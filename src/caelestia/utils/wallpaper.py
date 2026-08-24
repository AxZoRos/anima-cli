import json
import os
import random
import shutil
import subprocess
import threading
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

from materialyoucolor.hct import Hct
from materialyoucolor.utils.color_utils import argb_from_rgb
from PIL import Image, ImageStat

from caelestia.utils.colourfulness import get_variant
from caelestia.utils.material import get_colours_for_image
from caelestia.utils.paths import (
    atomic_write,
    c_cache_dir,
    compute_hash,
    get_config,
    thumb_queue_path,
    wallpaper_link_path,
    wallpaper_path_path,
    wallpaper_thumbnail_path,
    wallpapers_cache_dir,
)
from caelestia.utils.scheme import Scheme, get_scheme
from caelestia.utils.theme import apply_colours

# Cache utility paths once to avoid thousands of shutil.which() lookups in threads
_NICE_PATH = shutil.which("nice")
_IONICE_PATH = shutil.which("ionice")
_FFMPEG_PATH = shutil.which("ffmpeg")
_FFPROBE_PATH = shutil.which("ffprobe")


def _wrap_low_priority_cmd(cmd: list[str]) -> list[str]:
    prefix = []
    if _NICE_PATH:
        prefix.extend([_NICE_PATH, "-n", "10"])
    if _IONICE_PATH:
        prefix.extend([_IONICE_PATH, "-c", "2", "-n", "7"])
    return prefix + cmd


def is_valid_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".gif"]


def is_video(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in [".mp4", ".webm", ".mkv"]


def djb2_hash(s: str) -> str:
    hash_val = 5381
    for char in s:
        hash_val = ((hash_val << 5) + hash_val) + ord(char)
    return str(hash_val & 0xFFFFFFFF)


def probe_video_data(video_path: Path) -> dict[str, str] | None:
    if not _FFPROBE_PATH:
        return None
    try:
        cmd = _wrap_low_priority_cmd(
            [
                _FFPROBE_PATH,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-probesize",
                "1M",
                "-analyzeduration",
                "1M",
                "-show_entries",
                "stream=width,height,r_frame_rate,avg_frame_rate,bit_rate:format=bit_rate",
                "-of",
                "json",
                str(video_path),
            ]
        )
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=6.0)
        data = json.loads(res.stdout)
        streams = data.get("streams", [])
        stream = streams[0] if streams else {}
        format_info = data.get("format", {})

        width = stream.get("width")
        height = stream.get("height")
        if not width or not height:
            return None

        ext = video_path.suffix[1:].upper()
        if ext == "JPEG":
            ext = "JPG"

        fps_str = ""
        for rate in (stream.get("r_frame_rate"), stream.get("avg_frame_rate")):
            if rate and "/" in rate:
                num, den = rate.split("/")
                try:
                    d = float(den)
                    n = float(num)
                    if d > 0 and n > 0:
                        fps_val = round(n / d)
                        if fps_val > 0:
                            fps_str = f"{fps_val}fps"
                            break
                except (ValueError, TypeError):
                    pass

        bitrate_str = ""
        raw_bitrate = stream.get("bit_rate") or format_info.get("bit_rate")
        if raw_bitrate:
            try:
                br_val = float(raw_bitrate)
                mbps = br_val / 1_000_000
                if mbps >= 1.0:
                    bitrate_str = f"{mbps:.1f} Mbps".replace(".0 Mbps", " Mbps")
                else:
                    kbps = round(br_val / 1_000)
                    bitrate_str = f"{kbps} Kbps"
            except (ValueError, TypeError):
                pass

        return {
            "resolution": f"{width}x{height}",
            "format": ext,
            "fps": fps_str,
            "bitrate": bitrate_str,
        }
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _is_mostly_black(image_path: Path, threshold: float = 8.0) -> bool:
    try:
        with Image.open(image_path) as img:
            small = img.resize((64, 36), Image.Resampling.NEAREST)
            stat = ImageStat.Stat(small.convert("L"))
            return stat.mean[0] < threshold
    except (OSError, ValueError):
        return False


def extract_thumbnail(video_path: Path, output_path: Path) -> bool:
    if not _FFMPEG_PATH:
        return False

    def try_extract(s: float) -> bool:
        try:
            cmd = _wrap_low_priority_cmd(
                [
                    _FFMPEG_PATH,
                    "-y",
                    "-threads",
                    "1",
                    "-ss",
                    f"{s:.2f}",
                    "-i",
                    str(video_path),
                    "-an",
                    "-vframes",
                    "1",
                    "-vf",
                    "scale=-2:720",
                    "-q:v",
                    "2",
                    "-update",
                    "1",
                    str(output_path),
                ]
            )
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8.0)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
        return output_path.exists() and output_path.stat().st_size > 100

    candidate_timestamps = [0.5, 1.5, 3.0, 5.0, 0.1]

    for ts in candidate_timestamps:
        if try_extract(ts) and not _is_mostly_black(output_path):
            return True

    return output_path.exists() and output_path.stat().st_size > 100


def get_wallpaper() -> str | None:
    try:
        return wallpaper_path_path.read_text()
    except OSError:
        return None


def get_thumb(wall: Path, cache: Path) -> Path:
    thumb = cache / "thumbnail.jpg"

    if not thumb.exists():
        with Image.open(wall) as img:
            img = img.convert("RGB")
            img.thumbnail((128, 128), Image.Resampling.NEAREST)
            thumb.parent.mkdir(parents=True, exist_ok=True)
            img.save(thumb, "JPEG")

    return thumb


def get_smart_opts(wall: Path, cache: Path) -> dict:
    opts_cache = cache / "smart.json"

    try:
        return json.loads(opts_cache.read_text())
    except (OSError, json.JSONDecodeError):
        pass

    opts = {}

    with Image.open(get_thumb(wall, cache)) as img:
        opts["variant"] = get_variant(img)
        img.thumbnail((1, 1), Image.Resampling.LANCZOS)

        # Cast the pixel to a tuple of 3 integers to safely unpack it
        pixel = cast(tuple[int, int, int], img.getpixel((0, 0)))
        hct = Hct.from_int(argb_from_rgb(*pixel))

        opts["mode"] = "light" if hct.tone > 60 else "dark"

    opts_cache.parent.mkdir(parents=True, exist_ok=True)
    with opts_cache.open("w") as f:
        json.dump(opts, f)

    return opts


def convert_gif(wall: Path) -> Path:
    cache = wallpapers_cache_dir / compute_hash(wall)
    output_path = cache / "first_frame.jpg"

    if not output_path.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with Image.open(wall) as img:
            try:
                img.seek(0)
            except EOFError:
                pass

            img = img.convert("RGB")
            img.save(output_path, "JPEG", quality=90)

    return output_path


def convert_video(wall: Path) -> Path:
    videothumbs_dir = c_cache_dir / "videothumbs"
    videothumbs_dir.mkdir(parents=True, exist_ok=True)
    fast_thumb = videothumbs_dir / f"{djb2_hash(wall.name)}.jpg"
    if fast_thumb.exists() and fast_thumb.is_file() and fast_thumb.stat().st_size > 100:
        return fast_thumb

    if extract_thumbnail(wall, fast_thumb):
        return fast_thumb

    cache = wallpapers_cache_dir / djb2_hash(wall.name)
    output_path = cache / "first_frame.jpg"

    if not output_path.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        extract_thumbnail(wall, output_path)

    return output_path


def get_colours_for_wall(wall: Path | str, no_smart: bool) -> dict:
    wall = Path(wall)
    scheme = get_scheme()

    if wall.suffix.lower() == ".gif":
        wall_cache = convert_gif(wall)
    elif is_video(wall):
        wall_cache = convert_video(wall)
    else:
        wall_cache = wall

    cache = wallpapers_cache_dir / compute_hash(wall_cache)

    name = "dynamic"

    if not no_smart:
        smart_opts = get_smart_opts(wall_cache, cache)
        scheme = Scheme(
            {
                "name": name,
                "flavour": scheme.flavour,
                "mode": smart_opts["mode"],
                "variant": smart_opts["variant"],
                "colours": scheme.colours,
            }
        )

    return {
        "name": name,
        "flavour": scheme.flavour,
        "mode": scheme.mode,
        "variant": scheme.variant,
        "colours": get_colours_for_image(get_thumb(wall_cache, cache), scheme),
    }


def process_files_queue(files_to_process: list[Path], workers: int = 4) -> None:
    videothumbs_dir = c_cache_dir / "videothumbs"
    videothumbs_dir.mkdir(parents=True, exist_ok=True)

    props_file = c_cache_dir / "wallpaper_properties.json"
    props_data: dict[str, dict[str, str]] = {}
    if props_file.exists():
        try:
            props_data = json.loads(props_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            props_data = {}

    write_lock = threading.Lock()

    def run_garbage_collection():
        with write_lock:
            keys_snapshot = list(props_data.keys())
        dead_keys = []
        for p_str in keys_snapshot:
            p = Path(p_str)
            if not p.exists():
                dead_keys.append(p_str)
        if dead_keys:
            with write_lock:
                for p_str in dead_keys:
                    props_data.pop(p_str, None)
                active_hashes = {djb2_hash(Path(k).name) for k in props_data}
            for p_str in dead_keys:
                h = djb2_hash(Path(p_str).name)
                if h not in active_hashes:
                    thumb_path = videothumbs_dir / f"{h}.jpg"
                    thumb_path.unlink(missing_ok=True)

    def process_file(file_path: Path):
        try:
            str_path = str(file_path)
            if not is_video(file_path):
                return

            if str_path not in props_data:
                prop_val = probe_video_data(file_path)
                if prop_val:
                    with write_lock:
                        props_data[str_path] = prop_val

            h = djb2_hash(file_path.name)
            thumb_path = videothumbs_dir / f"{h}.jpg"

            should_extract = True
            if thumb_path.exists() and thumb_path.is_file():
                try:
                    if thumb_path.stat().st_size > 100:
                        should_extract = False
                except OSError:
                    pass

            success = not should_extract
            if should_extract:
                success = extract_thumbnail(file_path, thumb_path)

            if success:
                with write_lock:
                    print(f"READY:{str_path}", flush=True)
            else:
                with write_lock:
                    print(f"FAILED:{str_path}", flush=True)
        except (OSError, ValueError, TypeError):
            pass

    with ThreadPoolExecutor(max_workers=workers) as executor:
        list(executor.map(process_file, files_to_process))

    run_garbage_collection()

    try:
        atomic_write(props_file, json.dumps(props_data, indent=2))
    except OSError:
        pass


def run_worker_daemon() -> None:
    cpu_cores = os.cpu_count() or 2
    workers = min(8, max(1, cpu_cores // 2))

    if thumb_queue_path.exists():
        try:
            targets_raw = json.loads(thumb_queue_path.read_text(encoding="utf-8"))
            thumb_queue_path.unlink(missing_ok=True)
            if targets_raw and isinstance(targets_raw, list):
                targets = [Path(p) for p in targets_raw if Path(p).exists()]
                if targets:
                    process_files_queue(targets, workers=workers)
        except (OSError, json.JSONDecodeError):
            thumb_queue_path.unlink(missing_ok=True)


def set_wallpaper(wall: Path, no_smart: bool) -> None:
    # Make path absolute
    wall = Path(wall).resolve()

    if not is_valid_image(wall) and not is_video(wall):
        raise ValueError(f'"{wall}" is not a valid image or video')

    # Use gif/video 1st frame for thumb only
    if wall.suffix.lower() == ".gif":
        wall_cache = convert_gif(wall)
    elif is_video(wall):
        wall_cache = convert_video(wall)
    else:
        wall_cache = wall

    # Update files
    wallpaper_path_path.parent.mkdir(parents=True, exist_ok=True)
    wallpaper_path_path.write_text(str(wall))
    wallpaper_link_path.parent.mkdir(parents=True, exist_ok=True)
    wallpaper_link_path.unlink(missing_ok=True)
    wallpaper_link_path.symlink_to(wall)

    cache = wallpapers_cache_dir / compute_hash(wall_cache)

    # Generate thumbnail or get from cache
    thumb = get_thumb(wall_cache, cache)
    wallpaper_thumbnail_path.parent.mkdir(parents=True, exist_ok=True)
    wallpaper_thumbnail_path.unlink(missing_ok=True)
    wallpaper_thumbnail_path.symlink_to(thumb)

    scheme = get_scheme()

    # Change mode and variant based on wallpaper colour
    if scheme.name == "dynamic" and not no_smart:
        smart_opts = get_smart_opts(wall_cache, cache)
        scheme.mode = smart_opts["mode"]
        scheme.variant = smart_opts["variant"]

    # Update colours
    scheme.update_colours()
    apply_colours(scheme.colours, scheme.mode)

    # Run custom post-hook if configured
    cfg = get_config().get("wallpaper", {})
    if post_hook := cfg.get("postHook"):
        subprocess.run(
            post_hook,
            shell=True,
            check=False,
            env={
                **os.environ,
                "WALLPAPER_PATH": str(wall),
                "SCHEME_NAME": scheme.name,
                "SCHEME_FLAVOUR": scheme.flavour,
                "SCHEME_MODE": scheme.mode,
                "SCHEME_VARIANT": scheme.variant,
                "SCHEME_COLOURS": json.dumps(scheme.colours),
                "THUMBNAIL_PATH": str(thumb),
            },
            stderr=subprocess.DEVNULL,
        )


def set_random(args: Namespace) -> None:
    valid_exts = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".gif", ".mp4", ".webm", ".mkv"}
    walls = [f for f in Path(args.random).rglob("*") if f.is_file() and f.suffix.lower() in valid_exts]
    if walls:
        set_wallpaper(random.choice(walls), args.no_smart)
