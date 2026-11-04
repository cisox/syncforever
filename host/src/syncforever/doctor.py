"""`sfe doctor`: check the box's setup as the user the service runs as, and say how to fix what
isn't right. Read-only: it touches no cart, and in each save folder only a test file it removes.

Run as root (sudo sfe doctor) it checks the service, then runs its folder checks again as the
service's user (runuser), since what matters is what that user can read and write.
"""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from . import config
from .carts import FXPAK, PROFILES, X7
from .setup import CONSOLE_NAMES, SERVICE

PROFILE_BY_KEY = {p.key: p for p in (*PROFILES.values(), X7, FXPAK)}
OK, WARN, FAIL = "ok  ", "warn", "FAIL"


class Report:
    def __init__(self, out: Callable[[str], None] = print):
        self.out = out
        self.failed = False

    def line(self, level: str, text: str, fix: str = "") -> None:
        self.failed |= level == FAIL
        self.out(f"  [{level}] {text}")
        if fix:
            self.out(f"         fix: {fix}")


def whoami() -> str:
    return pwd.getpwuid(os.geteuid()).pw_name


def check_library(r: Report, lib: Path, suffixes) -> None:
    if not lib.is_dir():
        r.line(FAIL, f"library {lib} is missing", "mount it, or fix the path (sudo sfe setup)")
        return
    if not os.access(lib, os.R_OK | os.X_OK):
        r.line(
            FAIL,
            f"library {lib}: {whoami()} can't read it",
            f"give {whoami()} read access (or run the box as the files' owner: sudo sfe setup)",
        )
        return
    roms, outside, unreadable, denied = 0, 0, 0, []
    top = lib.resolve()
    for dirpath, dirnames, filenames in os.walk(lib, onerror=lambda e: denied.append(e.filename)):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or not name.lower().endswith(tuple(suffixes)):
                continue
            p = Path(dirpath, name)
            if top not in p.resolve().parents:
                outside += 1
            elif not os.access(p, os.R_OK):
                unreadable += 1
            else:
                roms += 1
    level = OK if roms else WARN
    r.line(
        level,
        f"library {lib}: {roms} ROMs",
        "" if roms else f"no {', '.join(suffixes)} files here: is it the right folder?",
    )
    if outside:
        r.line(
            WARN,
            f"{outside} ROMs are links to files outside the library: sync skips them",
            "replace them with hard links, or point the library at the folder they're in",
        )
    if unreadable or denied:
        r.line(
            WARN,
            f"{unreadable} ROMs and {len(denied)} folders {whoami()} can't read: a plan with such a folder stops with an error",
            f"give {whoami()} read access",
        )


def check_saves(r: Report, saves: Path, what: str = "save folder") -> None:
    if not saves.is_dir():
        r.line(
            FAIL,
            f"{what} {saves} is missing",
            "mount it, or create the folder (the box never creates it)",
        )
        return
    try:
        with tempfile.NamedTemporaryFile(dir=saves, prefix=".sfe-doctor-"):
            pass
        r.line(OK, f"{what} {saves}: writable")
    except OSError as e:
        r.line(
            FAIL,
            f"{what} {saves}: {whoami()} can't write it ({e.strerror})",
            f"give {whoami()} write access (for SMB, mount with uid=/gid= of that user), or sudo sfe setup",
        )


def check_folders(cfg_path: Path | None, out: Callable[[str], None] = print) -> bool:
    """The config, each console's folders, the state folder, as the current user. True if all good."""
    r = Report(out)
    out(f"As {whoami()}:")
    try:
        cfg = config.load(str(cfg_path) if cfg_path else None)
    except ValueError as e:
        r.line(FAIL, str(e), "fix the file, or rewrite it: sudo sfe setup")
        return False
    r.line(OK, f"config {cfg.source or '(none: defaults)'}")
    for key, name in CONSOLE_NAMES.items():
        c = cfg.console(key)
        if not c.library and not c.saves and not c.states:
            continue
        out(name)
        if c.library:
            check_library(r, c.library, PROFILE_BY_KEY[key].rom_suffixes)
        else:
            r.line(OK, "no library: this console doesn't sync")
        if c.saves:
            check_saves(r, c.saves)
        else:
            r.line(WARN, "no save folder: saves aren't backed up")
        if c.states and c.states != c.saves:
            check_saves(r, c.states, "save state folder")
    out("Box")
    state = cfg.state.expanduser()
    if state.is_dir() and os.access(state, os.W_OK):
        r.line(OK, f"state folder {state}: writable")
    else:
        r.line(
            FAIL,
            f"state folder {state}: {whoami()} can't write it",
            "sudo sfe setup (it gives the folder to the box's user)",
        )
    if not cfg.any_library():
        r.line(WARN, "no console has a library: nothing will sync", "sudo sfe setup")
    return not r.failed


def check_service(out: Callable[[str], None] = print) -> str | None:
    """The service's state and user (systemd); the user, or None without systemd."""
    if not shutil.which("systemctl"):
        return None
    r = Report(out)
    out("Service")
    active = subprocess.run(
        ["systemctl", "is-active", SERVICE], capture_output=True, text=True, check=False
    ).stdout.strip()
    user = subprocess.run(
        ["systemctl", "show", "-P", "User", SERVICE], capture_output=True, text=True, check=False
    ).stdout.strip()
    if active == "active":
        r.line(OK, f"{SERVICE} is running as {user or 'root'}")
    else:
        r.line(
            FAIL,
            f"{SERVICE} is {active or 'not installed'}",
            f"sudo systemctl start {SERVICE}; journalctl -u {SERVICE} -e",
        )
    from .box import serial_devices

    carts = serial_devices()
    r.line(OK if carts else WARN, f"carts on USB: {', '.join(carts) if carts else 'none'}")
    return user or "root"


def main(cfg_path: str | None, *, as_is: bool = False) -> int:
    path = Path(cfg_path) if cfg_path else config.find()
    if as_is or os.geteuid() != 0:
        if not as_is:
            check_service()
        return 0 if check_folders(path) else 1
    user = check_service()
    if user and user != "root" and shutil.which("runuser"):
        # What the box can do is what its user can do: the folder checks again, as that user.
        cmd = ["runuser", "-u", user, "--", sys.argv[0], "doctor", "--as-is"]
        if path:
            cmd += ["--config", str(path)]
        return subprocess.run(cmd, check=False).returncode
    return 0 if check_folders(path) else 1
