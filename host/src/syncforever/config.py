"""The box's settings: a TOML file (deploy/config.example.toml), overridden by command-line flags.

Search order when no file is named: $SFE_CONFIG, ~/.config/syncforever/config.toml,
/etc/syncforever/config.toml. None found means all defaults.

Folders are per console: [nes], [genesis] and [n64] (carts.CONSOLES) each take library, saves, dest
and sync_mode. [library], [saves] and [cart] give the defaults for consoles that don't set
their own. A console's saves = "" turns its save backup off.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import romsync
from .carts import CONSOLES

SEARCH = (Path("~/.config/syncforever/config.toml"), Path("/etc/syncforever/config.toml"))


OFF = Path("")  # an explicit "" in the config: off (where None means "not set")


@dataclass(frozen=True)
class ConsoleSettings:
    """One console's folders. Unset fields (None) fall back to the defaults (Config)."""

    library: Path | None = None
    saves: Path | None = None
    dest: str | None = None
    sync_mode: str | None = None
    msu: bool | None = None  # sync MSU games with their audio (msu.py); default off
    states: Path | None = None  # save states' folder; unset: with the saves, "": not backed up
    first_look: str | None = None  # files on the card with nothing on record (romsync.FIRST_LOOKS)


@dataclass(frozen=True)
class Config:
    # [library]
    library: Path | None = None  # folder of ROMs
    # [saves]
    saves: Path | None = None  # backup folder; None turns save backup off
    # [cart]
    dest: str = "/"  # where library ROMs go on the cart
    app_path: str = "/Sync Forever.nes"  # "" never installs; the cart's extension replaces .nes
    sync_mode: str = "add"
    set_clock: bool = True
    # [box]
    state: Path = Path("~/.syncforever")  # sync records
    port: str | None = None  # serial device; None finds every EverDrive
    firmware: bool = False  # check krikzz.com daily, deliver new firmware (firmware.py)
    consoles: dict = field(default_factory=dict)  # key -> ConsoleSettings ([nes], [genesis])
    source: Path | None = field(default=None, compare=False)  # the file these came from

    def __post_init__(self):
        modes = [self.sync_mode] + [c.sync_mode for c in self.consoles.values() if c.sync_mode]
        for mode in modes:
            if mode not in romsync.MODES:
                raise ValueError(f"sync mode must be one of {', '.join(romsync.MODES)}, not {mode!r}")
        for c in self.consoles.values():
            if c.first_look and c.first_look not in romsync.FIRST_LOOKS:
                raise ValueError(f"first_look must be one of {', '.join(romsync.FIRST_LOOKS)}, not {c.first_look!r}")

    def console(self, key: str) -> ConsoleSettings:
        """This console's folders, with the defaults filled in (OFF becomes None)."""
        c = self.consoles.get(key, ConsoleSettings())

        def pick(own, default):
            value = default if own is None else own
            return None if value == OFF else value

        return ConsoleSettings(
            library=pick(c.library, self.library),
            saves=pick(c.saves, self.saves),
            dest=c.dest if c.dest is not None else self.dest,
            sync_mode=c.sync_mode or self.sync_mode,
            msu=c.msu is True,
            states=pick(c.states, pick(c.saves, self.saves)),
            first_look=c.first_look or "auto",
        )

    def any_library(self) -> bool:
        return any(self.console(k).library for k in CONSOLES)

    def override(self, **changes) -> Config:
        """A copy with the given settings replaced; None values (flags not given) are ignored."""
        return replace(self, **{k: v for k, v in changes.items() if v is not None})


# TOML table -> {key: (Config field, type)}
SCHEMA = {
    "library": {"path": ("library", Path)},
    "saves": {"path": ("saves", Path)},
    "cart": {
        "dest": ("dest", str),
        "app_path": ("app_path", str),
        "sync_mode": ("sync_mode", str),
        "set_clock": ("set_clock", bool),
    },
    "box": {"state": ("state", Path), "port": ("port", str), "firmware": ("firmware", bool)},
}
CONSOLE_KEYS = {
    "library": Path,
    "saves": Path,
    "dest": str,
    "sync_mode": str,
    "msu": bool,
    "states": Path,
    "first_look": str,
}


def parse(text: str, source: Path | None = None) -> Config:
    where = f" in {source}" if source else ""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"config{where}: {e}") from None
    values = {}
    consoles = {}
    for table, body in data.items():
        if table in CONSOLES and isinstance(body, dict):
            consoles[table] = _console(table, body, where)
            continue
        if table not in SCHEMA or not isinstance(body, dict):
            raise ValueError(f"config{where}: unknown section [{table}]")
        for key, value in body.items():
            if key not in SCHEMA[table]:
                raise ValueError(f"config{where}: unknown setting {table}.{key}")
            name, kind = SCHEMA[table][key]
            if kind is Path:
                if not isinstance(value, str):
                    raise ValueError(f"config{where}: {table}.{key} must be a path string")
                value = Path(value).expanduser() if value else None
            elif not isinstance(value, kind):
                raise ValueError(f"config{where}: {table}.{key} must be {kind.__name__}")
            if name == "port" and not value:
                value = None
            values[name] = value
    return Config(**values, consoles=consoles, source=source)


def _console(table: str, body: dict, where: str) -> ConsoleSettings:
    values = {}
    for key, value in body.items():
        kind = CONSOLE_KEYS.get(key)
        if kind is None:
            raise ValueError(f"config{where}: unknown setting {table}.{key}")
        if kind is bool:
            if not isinstance(value, bool):
                raise ValueError(f"config{where}: {table}.{key} must be true or false")
            values[key] = value
            continue
        if not isinstance(value, str):
            raise ValueError(f"config{where}: {table}.{key} must be a string")  # noqa: TRY004
        values[key] = (Path(value).expanduser() if value else OFF) if kind is Path else value
    return ConsoleSettings(**values)


def find(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("SFE_CONFIG")
    if env:
        return Path(env).expanduser()
    for p in SEARCH:
        if p.expanduser().is_file():
            return p.expanduser()
    return None


def load(explicit: str | None = None) -> Config:
    path = find(explicit)
    if path is None:
        return Config()
    try:
        text = path.read_text()
    except OSError as e:
        raise ValueError(f"can't read config {path}: {e.strerror}") from None
    return parse(text, path)
