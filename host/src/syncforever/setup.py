"""`sudo sfe setup`: the box's settings by question and answer (deploy/install.sh runs it on a
fresh install; run it again any time, your current values are the defaults).

It asks for each console's ROM library and save folder, the default sync mode and which user the
box runs as, then writes them into the config (in place: every other line and comment stays),
puts the user in a systemd drop-in (kept by install.sh and package updates), gives that user
the state folder, restarts the service and runs `sfe doctor`.
"""

from __future__ import annotations

import grp
import os
import pwd
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from . import config, romsync
from .carts import FXPAK, PROFILES, X7

SERVICE = "syncforever"
DEFAULT_USER = "syncforever"
DROPIN = Path(f"/etc/systemd/system/{SERVICE}.service.d/10-user.conf")

CONSOLE_NAMES = {
    "nes": "NES (EverDrive-N8 PRO)",
    "genesis": "Genesis / Mega Drive (Mega EverDrive PRO)",
    "n64": "N64 (EverDrive-64 X7)",
    "snes": "SNES (FXPAK PRO)",
}
PROFILE_BY_KEY = {p.key: p for p in (*PROFILES.values(), X7, FXPAK)}
assert set(CONSOLE_NAMES) == set(PROFILE_BY_KEY)
WITH_SAVES, OFF_WORD = "with the saves", "off"


def toml_str(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def set_values(text: str, section: str, values: dict[str, str | None]) -> str:
    """The config text with `key = "value"` set in [section]: the line replaced where it is,
    else added under the header; the section added at the end if it's missing. None: no such
    line."""
    lines = text.splitlines()
    header = re.compile(r"^\s*\[([^\]]+)\]\s*(#.*)?$")
    start = next(
        (i for i, line in enumerate(lines) if (m := header.match(line)) and m.group(1).strip() == section), None
    )
    if start is None:
        lines += ["", f"[{section}]"] + [f"{k} = {toml_str(v)}" for k, v in values.items() if v is not None]
        return "\n".join(lines) + "\n"
    end = next((i for i in range(start + 1, len(lines)) if header.match(lines[i])), len(lines))
    for key, value in values.items():
        new = f"{key} = {toml_str(value)}" if value is not None else ""
        at = next((i for i in range(start + 1, end) if re.match(rf"^\s*{re.escape(key)}\s*=", lines[i])), None)
        if value is None:
            if at is not None:
                del lines[at]
                end -= 1
        elif at is None:
            lines.insert(start + 1, new)
            end += 1
        else:
            lines[at] = new
    return "\n".join(lines) + "\n"


class Setup:
    def __init__(
        self,
        config_path: Path,
        *,
        ask: Callable[[str], str] = input,
        out: Callable[[str], None] = print,
        dropin: Path = DROPIN,
        run: Callable[[list[str]], object] = lambda cmd: subprocess.run(cmd, check=False),
    ):
        self.path = Path(config_path)
        self.ask = ask
        self.out = out
        self.dropin = dropin
        self.run = run

    def question(self, prompt: str, current: str) -> str:
        """The answer; Enter keeps `current`, "-" clears it."""
        shown = f" [{current}]" if current else " [none]"
        answer = self.ask(f"{prompt}{shown}: ").strip()
        if answer == "-":
            return ""
        return answer or current

    def folder(self, prompt: str, current: str) -> str:
        while True:
            value = self.question(prompt, current)
            if not value or Path(value).is_dir():
                return value
            if self.ask(f"  {value} doesn't exist (not mounted?). Use it anyway? [y/N]: ").strip().lower() == "y":
                return value
            current = value

    def states_folder(self, own) -> str | None:
        """The console's `states` setting: a folder, "" (not backed up), or None (no setting:
        with the saves)."""
        now = getattr(own, "states", None)
        current = WITH_SAVES if now is None else OFF_WORD if now == config.OFF else str(now)
        while True:
            answer = self.ask(f"  Save state folder (- with the saves, {OFF_WORD} none) [{current}]: ").strip()
            value = current if not answer else WITH_SAVES if answer == "-" else answer
            if value == WITH_SAVES:
                return None
            if value == OFF_WORD:
                return ""
            if Path(value).is_dir():
                return value
            if self.ask(f"  {value} doesn't exist (not mounted?). Use it anyway? [y/N]: ").strip().lower() == "y":
                return value

    def run_setup(self) -> int:
        text = self.path.read_text() if self.path.exists() else ""
        cfg = config.parse(text, self.path)
        self.out("Sync Forever setup. Enter keeps the value in [brackets], - clears it.\n")
        for key, name in CONSOLE_NAMES.items():
            c = cfg.console(key)
            self.out(name)
            library = self.folder("  ROM library folder (empty: this console doesn't sync)", str(c.library or ""))
            saves = self.folder("  Save backup folder (empty: no save backup)", str(c.saves or ""))
            values = {"library": library, "saves": saves}
            if PROFILE_BY_KEY[key].save_states:
                values["states"] = self.states_folder(cfg.consoles.get(key))
            text = set_values(text, key, values)
        mode = cfg.sync_mode
        while True:
            mode = self.question(f"Default sync mode ({', '.join(romsync.MODES)})", mode)
            if mode in romsync.MODES:
                break
            self.out(f"  one of {', '.join(romsync.MODES)}")
        text = set_values(text, "cart", {"sync_mode": mode})
        user = self.service_user()
        config.parse(text, self.path)  # never write a config the box can't read
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text)
        tmp.replace(self.path)
        self.out(f"\nWrote {self.path}")
        self.write_dropin(user)
        state = cfg.state.expanduser()
        state.mkdir(parents=True, exist_ok=True)
        shutil.chown(state, user, pwd.getpwnam(user).pw_gid)
        self.run(["systemctl", "daemon-reload"])
        self.run(["systemctl", "restart", SERVICE])
        self.out(f"The box runs as {user}; service restarted. Checking it (sfe doctor):\n")
        return 0

    def effective_user(self) -> str:
        """Who the service runs as now (systemd, every drop-in included), else our drop-in's."""
        try:
            got = subprocess.run(
                ["systemctl", "show", "-P", "User", SERVICE], capture_output=True, text=True, check=False
            )
            if got.returncode == 0 and got.stdout.strip():
                return got.stdout.strip()
        except OSError:
            pass
        if self.dropin.exists():
            m = re.search(r"^User=(.+)$", self.dropin.read_text(), re.MULTILINE)
            if m:
                return m.group(1).strip()
        return DEFAULT_USER

    def service_user(self) -> str:
        current = self.effective_user()
        self.out(
            "\nThe box runs as a user that can read the ROM libraries and write the save folders.\n"
            "With a network share, that's usually the user that owns its files (e.g. the NFS share's uid)."
        )
        while True:
            user = self.question("Run the box as", current)
            try:
                pwd.getpwnam(user)
                return user
            except KeyError:
                self.out(f"  there's no user {user!r} on this computer (create it, or pick another)")

    def write_dropin(self, user: str) -> None:
        group = grp.getgrgid(pwd.getpwnam(user).pw_gid).gr_name
        self.dropin.parent.mkdir(parents=True, exist_ok=True)
        override = self.dropin.parent / "override.conf"  # systemctl edit's: read after ours, so it wins
        if override.exists() and re.search(r"^\s*User=", override.read_text(), re.MULTILINE):
            self.out(f"Note: {override} (from systemctl edit) sets User= too and wins over this; remove that line.")
        self.dropin.write_text(
            "# Written by sfe setup: the user the box runs as.\n[Service]\n"
            f"User={user}\nGroup={group}\nSupplementaryGroups=dialout\n"
        )
        self.out(f"Wrote {self.dropin}")


def main(config_path: str | None) -> int:
    if os.geteuid() != 0:
        raise PermissionError("setup writes /etc and restarts the service: run it with sudo")
    path = Path(config_path) if config_path else (config.find() or Path("/etc/syncforever/config.toml"))
    return Setup(path).run_setup()
