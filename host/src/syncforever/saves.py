"""Save backup: copy every file a game keeps on the cart into versioned files, never overwritten.

Cart layout (N8 verified 2026-10-31, Mega 2026-11-01; docs/test-results.md), under the cart's
system folder (carts.py: EDN8, MEGA):
- sysdata/recent.bin (N8) or recent.dat (Mega): 512-byte slots, newest first, each a
  NUL-terminated ROM path ("/Zelda.nes").
- gamedata/<ROM filename>/: the game's files, written by the time the menu is back:
  battery saves (bram.srm; the Sega CD's cd-bram.brm), save states (Mega: 00.SAV ...).

Store layout: <store>/<ROM filename>/<file>/<NNNN>_<YYYYmmdd-HHMMSS>_<sha256[:8]><ext>, one
file per change seen. NNNN only goes up, so order never depends on the clock. A file identical
to its newest copy is not stored again. Versions are stamped with the box's clock and
compared by SHA-256. Save states (is_state) can have a store of their own, laid out the same
(the console's `states` setting). The store only ever gains versions, and only of what a cart
held: nothing else writes to it.

Two ways to find a game's files: list its folder (every file, any name; needs F_DIR_LD, so
only while the menu isn't running or with the menu's folder restored), or stat the names the
cart profile knows (CartProfile.save_files; F_FINFO, safe anywhere).
"""

from __future__ import annotations

import hashlib
import io
import re
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import ops

SLOT = 512
STATE_NAME = re.compile(r"(\d\d\.sav|.*\.state)", re.IGNORECASE)  # the Mega's slots; the FXPAK's
WITH_SAVES = object()  # SaveStore's states: in the save folder


def is_state(name: str) -> bool:
    """A save state, not a battery save: the Mega's 00.SAV ... 09.SAV, the FXPAK's *.state."""
    return STATE_NAME.fullmatch(name) is not None


def gamedata(dev) -> str:
    return f"{dev.profile.system_dir}/gamedata"


def recent_path(dev) -> str:
    return f"{dev.profile.system_dir}/{dev.profile.recent_file}"


def parse_recent(data: bytes) -> list[str]:
    """ROM paths in the recent list, newest first, without the leading slash."""
    out = []
    for off in range(0, len(data) - SLOT + 1, SLOT):
        raw = data[off : off + SLOT].split(b"\0", 1)[0]
        if raw:
            out.append(ops.norm(raw.decode("latin-1")))
    return out


def read_file(dev, path: str) -> bytes | None:
    entry = dev.stat(path)
    if entry is None or entry.is_dir:
        return None
    buf = io.BytesIO()
    ops.download(dev, path, buf)
    return buf.getvalue()


def _part(name: str) -> str:
    """A game or file name from the cart, as one path component of the store. The names come
    from the SD card (listings, the recent list), so refuse anything that could leave it."""
    if not name or name in (".", "..") or any(c in name for c in "/\\\0") or ":" in name:
        raise ValueError(f"unsafe name from the cart: {name!r}")
    return name


class SaveStore:
    """root: the save folder. states: where save states go: WITH_SAVES, another folder, or None
    (not backed up). root None: only the states are backed up."""

    def __init__(self, root, clock=time.time, *, states=WITH_SAVES):
        self.root = Path(root) if root else None
        self.states = self.root if states is WITH_SAVES else Path(states) if states else None
        self.clock = clock

    def _root(self, file: str) -> Path | None:
        return self.states if is_state(file) else self.root

    def wants(self, file: str) -> bool:
        """False: this kind of file isn't backed up, so don't read it from the cart."""
        return self._root(file) is not None

    def missing(self) -> Path | None:
        """A folder of this store that isn't there (not mounted?). The box never creates
        them: a backup would land on the mount point."""
        return next((p for p in (self.root, self.states) if p is not None and not p.is_dir()), None)

    def _folder(self, root: Path, game: str, file: str | None = None) -> Path:
        folder = root / _part(game)
        if file is not None:
            folder = folder / _part(file)
        if not folder.resolve().is_relative_to(root.resolve()):
            raise ValueError(f"outside the save store: {folder}")
        return folder

    def versions(self, game: str, file: str) -> list[Path]:
        """Oldest first."""
        root = self._root(file)
        if root is None:
            return []
        folder = self._folder(root, game, file)
        if not folder.is_dir():
            return []
        return sorted((p for p in folder.iterdir() if _seq(p) is not None), key=_seq)

    def files(self, game: str) -> list[str]:
        out = set()
        for root in {self.root, self.states} - {None}:
            folder = self._folder(root, game)
            if folder.is_dir():
                out |= {p.name for p in folder.iterdir() if p.is_dir()}
        return sorted(out)

    def add(self, game: str, file: str, data: bytes) -> Path | None:
        """Store data as a new version of game's file; None if it equals the newest version
        (or this kind of file isn't backed up)."""
        root = self._root(file)
        if root is None:
            return None
        sha = hashlib.sha256(data).hexdigest()
        versions = self.versions(game, file)
        if versions and versions[-1].read_bytes() == data:
            return None
        folder = self._folder(root, game, file)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.clock()))
        ext = PurePosixPath(file).suffix
        seq = _seq(versions[-1]) + 1 if versions else 1
        while True:
            path = folder / f"{seq:04d}_{stamp}_{sha[:8]}{ext}"
            try:
                with open(path, "xb") as f:  # never overwrite
                    f.write(data)
                return path
            except FileExistsError:  # another writer took this number
                seq += 1


def _seq(path: Path) -> int | None:
    head = path.name.split("_", 1)[0]
    return int(head) if head.isdigit() else None


@dataclass(frozen=True)
class BackupResult:
    game: str
    file: str
    status: str  # "saved", "unchanged"
    path: Path | None = None
    size: int = 0


def _result(store, game: str, file: str, data: bytes) -> BackupResult:
    path = store.add(game, file, data)
    return BackupResult(game, file, "saved" if path else "unchanged", path, len(data))


def backup_files(dev, store: SaveStore, game: str, names) -> list[BackupResult]:
    out = []
    for name in names:
        try:
            _part(game), _part(name)
        except ValueError:
            continue  # a name the store can't hold safely: skip it, never write outside
        if not store.wants(name):
            continue
        cart_path = f"{gamedata(dev)}/{game}/{name}"
        data = read_file(dev, cart_path)
        if data is None:
            continue
        out.append(_result(store, game, name, data))
    return out


def flat_game(name: str) -> str:
    """The game a flat save file belongs to: its name without the type, and for an FXPAK save
    state without the slot ("Secret of Mana (USA)01.state" -> "Secret of Mana (USA)")."""
    stem = name.rsplit(".", 1)[0] or name
    if name.lower().endswith(".state") and len(stem) > 2 and stem[-2:].isdigit():
        stem = stem[:-2]
    return stem


APP_SRAM = b"SFE1"  # the start of the SNES app's SRAM (snes/src/link_io.c), so of its .srm


def backup_flat(dev, store: SaveStore, progress=lambda done, total, name: None) -> list[BackupResult]:
    """Carts whose save folders hold <ROM name>.<type> files directly (the X7's gamedata, the
    FXPAK's saves, states and cheats: CartProfile.save_dirs): each file is stored under its
    game, e.g. "Paper Mario (USA)/Paper Mario (USA).fla". A missing folder is skipped.
    progress(done, total, game) before each file."""
    whole = getattr(dev, "read_whole", None)  # the FXPAK: one GET, no separate size check
    todo = []
    for sub in dev.profile.save_dirs:
        folder = f"{dev.profile.system_dir}/{sub}"
        if len(dev.profile.save_dirs) > 1 and dev.stat(folder) is None:
            continue
        for e in dev.list_dir(folder, **({"sizes": False} if whole else {})):
            if e.is_dir:
                continue
            game = flat_game(e.name)
            try:
                _part(game), _part(e.name)
            except ValueError:
                continue
            if store.wants(e.name):
                todo.append((folder, game, e.name))
    out = []
    for done, (folder, game, name) in enumerate(todo, 1):
        progress(done, len(todo), game)
        data = whole(f"{folder}/{name}") if whole else read_file(dev, f"{folder}/{name}")
        if data is None or (name.lower().endswith(".srm") and data[:4] == APP_SRAM):
            continue  # gone, or the SNES app's own SRAM: its link to the box, not a save
        out.append(_result(store, game, name, data))
    return out


PAKS = "Controller Pak"  # the store's folder for Controller Pak images


def pak_id(image: bytes) -> str:
    """A name for one Controller Pak: from its ID block (0x20-0x3F, written when it's formatted:
    a serial and the like), so two paks never share versions. A blank block: from the data."""
    block = image[0x20:0x40]
    if block.strip(b"\0") and block.strip(b"\xff"):
        return hashlib.sha256(block).hexdigest()[:8]
    return "blank-" + hashlib.sha256(image).hexdigest()[:8]


def backup_paks(dev, store: SaveStore, progress=lambda done, total, name: None) -> list[BackupResult]:
    """Every Controller Pak plugged into a controller (the X7: our app reads them), as
    "Controller Pak/<id>.mpk": the format the X7's CPak manager restores ("File to CPak").
    progress(port + 1, 4, PAKS) before each controller."""
    reader = getattr(dev, "controller_pak", None)
    out = []
    for port in range(4) if reader else ():
        progress(port + 1, 4, PAKS)
        image = reader(port)
        if image is None:
            continue
        name = f"{pak_id(image)}.mpk"
        path = store.add(PAKS, name, image)
        out.append(BackupResult(PAKS, name, "saved" if path else "unchanged", path, len(image)))
    return out


def backup_game(dev, store: SaveStore, game: str, *, listing=False, restore=True) -> list[BackupResult]:
    """Back up one game's files (game = its ROM filename, e.g. "Zelda.nes"): every file in its
    folder (listing=True; restore as for list_dir), or the profile's known names."""
    if listing:
        entries = dev.list_dir(f"{gamedata(dev)}/{game}", restore=restore)
        names = [e.name for e in entries if not e.is_dir]
    else:
        names = dev.profile.save_files
    return backup_files(dev, store, game, names)


def recent_games(dev, n: int = 1) -> list[str]:
    data = read_file(dev, recent_path(dev))
    if data is None:
        return []
    games = []
    for rom in parse_recent(data):
        name = PurePosixPath(rom).name
        if name not in games:
            games.append(name)
        if len(games) == n:
            break
    return games


def summary(results: list[BackupResult]) -> str:
    saved = [f"{r.game}/{r.file}" for r in results if r.status == "saved"]
    return f"checked {len(results)} files, backed up {len(saved)}" + (f": {', '.join(saved)}" if saved else "")


def backup_recent(dev, store: SaveStore, n: int = 1, *, check_menu=True) -> list[BackupResult]:
    """Back up the n most recently played games' files, by the profile's known names (stats
    and reads, no listing). check_menu=False when the caller knows it's safe (a game runs)."""
    if check_menu:
        ops.require_menu(dev, "backing up saves")
    return [r for game in recent_games(dev, n) for r in backup_game(dev, store, game)]


def backup_all(dev, store: SaveStore, *, in_app=False, progress=lambda done, total, name: None) -> list[BackupResult]:
    """Back up every file of every game in gamedata (reads only). At the menu each listing
    restores the menu's folder; in_app=True is for our app's session, where the menu isn't
    running (no ping: it would land in the app's FIFO; no restore: the menu reloads when the
    app exits). progress(done, total, name) before each game or file."""
    if not in_app:
        ops.require_menu(dev, "backing up saves")
    restore = None if in_app else True
    if dev.profile.flat_saves:
        return backup_flat(dev, store, progress) + backup_paks(dev, store, progress)
    games = [e.name for e in dev.list_dir(gamedata(dev), restore=restore) if e.is_dir]
    out = []
    for done, game in enumerate(games, 1):
        progress(done, len(games), game)
        out += backup_game(dev, store, game, listing=True, restore=restore)
    return out
