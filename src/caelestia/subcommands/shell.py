import json
import os
import subprocess
from argparse import Namespace
from pathlib import Path

from caelestia.utils.paths import c_cache_dir, c_state_dir


class Command:
    args: Namespace

    def __init__(self, args: Namespace) -> None:
        self.args = args

    def _resolve_hw_decoder(self) -> str | None:
        decoder = ""

        state_file = c_state_dir / "wallpaper/ui_state.json"
        if state_file.exists():
            try:
                data = json.loads(state_file.read_text(encoding="utf-8"))
                decoder = str(data.get("hwDecoder", "")).strip()
            except Exception:
                pass

        if not decoder or decoder.lower() == "auto":
            return None

        if decoder.lower() in ["none", "cpu", "disabled", "software"]:
            return ","

        return decoder

    def run(self) -> None:
        if self.args.show:
            # Print the ipc
            self.print_ipc()
        elif self.args.log:
            # Print the log
            self.print_log()
        elif self.args.kill:
            # Kill the shell
            self.shell("kill")
        elif self.args.message:
            # Send a message
            self.message(*self.args.message)
        else:
            # Hardware Decoder Injection
            env = os.environ.copy()
            hw_decoder = self._resolve_hw_decoder()
            if hw_decoder is not None:
                env["QT_FFMPEG_DECODING_HW_DEVICE_TYPES"] = hw_decoder
            else:
                env.pop("QT_FFMPEG_DECODING_HW_DEVICE_TYPES", None)
                
            # Start the shell
            args = ["qs", "-c", "caelestia", "-n"]
            if self.args.log_rules:
                args.extend(["--log-rules", self.args.log_rules])
            if self.args.daemon:
                args.append("-d")
                subprocess.run(args, env=env)
            else:
                shell = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    universal_newlines=True,
                    env=env,
                )

                # Ensure stdout is not None for the type checker
                if shell.stdout:
                    for line in shell.stdout:
                        if self.filter_log(line):
                            print(line, end="")

    def shell(self, *args: str) -> str:
        return subprocess.check_output(["qs", "-c", "caelestia", *args], text=True)

    def filter_log(self, line: str) -> bool:
        return f"Cannot open: file://{c_cache_dir}/imagecache/" not in line

    def print_ipc(self) -> None:
        print(self.shell("ipc", "show"), end="")

    def print_log(self) -> None:
        if self.args.log_rules:
            log = self.shell("log", "-r", self.args.log_rules)
        else:
            log = self.shell("log")
        # FIXME: remove when logging rules are added/warning is removed
        for line in log.splitlines():
            if self.filter_log(line):
                print(line)

    def message(self, *args: list[str]) -> None:
        print(self.shell("ipc", "call", *args), end="")
