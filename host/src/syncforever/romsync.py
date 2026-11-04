"""ROM sync, library folder -> cart. Runs inside an app session.

Modes (docs/design.md, decided 2026-11-01):
  add     copy ROMs missing on the cart; never replace or delete
  update  add, plus replace ROMs whose size or SHA-256 differ
  mirror  update, plus delete ROMs that sync itself copied earlier and that left the library
  strict  update, plus delete every ROM on the cart that isn't in the library
Mirror also removes the folders its removals leave empty; strict, every empty folder under dest.
Never: the Sync Forever app (make_plan's keep), hidden entries, or anything at or under a system
folder (EDN8/, MEGA/, ED64/, sd2snes/): the walk doesn't enter them and guard() refuses to touch them.

Only ROM files (the cart profile's rom_suffixes: carts.py) are compared or touched, never a
cart's system folder (EDN8/, MEGA/: saves, settings, the menu), hidden files, or anything else. Cart timestamps are wrong, so "changed" means size or
SHA-256. The manifest (a JSON file on the box) remembers which cart files sync copied
("owned", for mirror) and the hashes of cart files already read, so Update doesn't re-read
the whole card every time. On the FXPAK, whose listings have no sizes (a size costs reading the
file), the manifest's size is trusted for every file it knows; the rest are read once.
"""

from __future__ import annotations

import json
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import msu as msu_mod
from . import ops
from .carts import FXPAK, PROFILES, X7

MODES = ("add", "update", "mirror", "strict")
BIG = 16 << 20  # copies this big show their size (an MSU-1 track can take many minutes)
LOG_EVERY = 30.0  # seconds between a big copy's progress lines in the log
DEFAULT_UP, DEFAULT_READ = 500.0, 700.0  # KiB/s, until a cart says (upload_kib_s) or a copy measures
REMOVE_SECS = 1.0  # a removal, for the time left
STATUS_EVERY = 2.0  # seconds between progress lines in slow work (the apps give up after 30 s of silence)
HASHES_EVERY = 10.0  # seconds between saves of the library's checksums during a plan
COPY_WAIT, CART_COPY_RATE = 30.0, 256 * 1024  # seconds, bytes/s: how long a copy on the cart may take
# A file on the card, same name and size as the library's, nothing on record (a card copied by
# hand): how a plan takes it. check: read or checksum every one. trust: take it as the library's.
# auto: check, unless that would take over FIRST_LOOK_LIMIT seconds and a spot check of
# SPOT_CHECK_SECS seconds' worth finds every file the same.
FIRST_LOOKS = ("auto", "check", "trust")
FIRST_LOOK_LIMIT = 600.0
SPOT_CHECK_SECS = 120.0
CART_CRC_RATE = 3 << 20  # bytes/s of a checksum on the cart, for the app's estimate (seen: 4 MB/s)
EXPECT_SLACK = 5  # seconds around a copy until measured: the FXPAK's autosave wait, open, close
SPEED_MEMORY = 512 << 20  # bytes of measurements a speed keeps at full weight
AROUND_MEMORY = 50  # copies the time around one keeps at full weight
COPYING, CHECKING = 0, 1  # run_plan's copying() stages
MODE_NAMES = {"add": "Add only", "update": "Update", "mirror": "Mirror", "strict": "Mirror STRICT"}
ROM_SUFFIXES = (".nes",)  # the N8's; other carts pass their profile's (carts.py)
# Every cart's menu folder (EDN8, MEGA, ED64, sd2snes): never written, on any cart.
SYSTEM_DIRS = frozenset(p.system_dir.casefold() for p in (*PROFILES.values(), X7, FXPAK))


def is_rom(name: str, suffixes=ROM_SUFFIXES) -> bool:
    return not name.startswith(".") and name.lower().endswith(tuple(suffixes))


def key(path: str) -> str:
    """Paths compare as FAT matches them: case-insensitive, trailing dots and spaces dropped
    (a library folder "Super Smash T.V." is "Super Smash T.V" on the card)."""
    return ops.fat_key(path)


def cart_join(dest: str, rel: str) -> str:
    return ops.norm(f"{dest}/{rel}")


def in_system_dir(path: str) -> bool:
    """Compared as FAT matches names: a folder "sd2snes." on the card IS sd2snes/."""
    return ops.fat_key(path).split("/", 1)[0] in SYSTEM_DIRS


def guard(path: str) -> str:
    p = ops.norm(path)
    if in_system_dir(p):
        raise ops.WriteNotAllowed(f"never writes in the cart's system folder (/{p})")
    return p


class LibraryHashes:
    """SHA-256 and CRC-32 of library files, kept while a file's size and modification time stay
    the same: a plan compares every ROM the cart has with the library's, and hashing them all
    again took ~10 s over a network share each time (2026-11-04). Saved beside the cart's manifest."""

    def __init__(self, path):
        self.path = Path(path)
        self.dirty = False
        try:
            self.known: dict[str, list] = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self.known = {}

    def has(self, src: Path) -> bool:
        st = src.stat()
        hit = self.known.get(str(src))
        return bool(hit) and hit[0] == st.st_size and hit[1] == st.st_mtime_ns

    def sha(self, src: Path) -> str:
        return self.known[str(src)][2] if self.has(src) else self.both(src)[0]

    def both(self, src: Path) -> tuple[str, int]:
        """(SHA-256, CRC-32). A record from before CRCs has only the SHA: read again."""
        k = str(src)
        if self.has(src) and len(self.known[k]) > 3:
            return self.known[k][2], self.known[k][3]
        st = src.stat()
        sha, crc = ops.file_hashes(src)
        self.known[k] = [st.st_size, st.st_mtime_ns, sha, crc]
        self.dirty = True
        return sha, crc

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.known))
        os.replace(tmp, self.path)
        self.dirty = False


class Manifest:
    """{cart path key: {"path", "size", "sha", "owned"}}, saved atomically after each change; and
    the speeds this cart has shown (Speeds), so estimates fit this cart, cable, card and box."""

    def __init__(self, path):
        self.path = Path(path)
        self.speeds: dict = {}
        try:
            data = json.loads(self.path.read_text())
            self.files: dict[str, dict] = data.get("files", {})
            self.speeds = data.get("speeds", {})
        except FileNotFoundError:
            self.files = {}

    def get(self, cart_path: str) -> dict | None:
        return self.files.get(key(cart_path))

    def record(self, cart_path: str, size: int, sha: str, *, owned: bool | None = None) -> None:
        old = self.files.get(key(cart_path), {})
        self.files[key(cart_path)] = {
            "path": ops.norm(cart_path),
            "size": size,
            "sha": sha,
            "owned": old.get("owned", False) if owned is None else owned,
        }
        self.save()

    def forget(self, cart_path: str) -> None:
        if self.files.pop(key(cart_path), None) is not None:
            self.save()

    def owned(self) -> list[str]:
        return [v["path"] for v in self.files.values() if v.get("owned")]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"version": 1, "files": self.files}
        if self.speeds:
            data["speeds"] = self.speeds
        tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
        os.replace(tmp, self.path)


@dataclass
class Plan:
    mode: str
    add: list[str] = field(default_factory=list)  # library-relative paths
    replace: list[str] = field(default_factory=list)  # library-relative paths
    remove: list[str] = field(default_factory=list)  # cart paths
    remove_dirs: list[str] = field(default_factory=list)  # cart folders left empty, deepest first
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)  # library-relative: names the cart can't store
    extras: frozenset[str] = frozenset()  # keys of MSU files (msu.py): big, never read back
    left_out: list[str] = field(default_factory=list)  # library-relative: MSU games, the setting off
    stopped: int | None = None  # run_plan stopped early (the app's B): how many were done
    done: int = 0  # run_plan: how many are finished
    doing: str = ""  # and the name of the one under way (what a failure was working on)
    msu_games: int = 0  # MSU games in the plan (the setting on), their files counted with the rest
    tree: tuple | None = None  # the cart listing it was made from (cart_tree), to plan again without the cart
    # (cart path, library-relative path): a file the cart has, under a name the library no longer
    # uses, whose contents are a file it would add: renamed on the card instead of copied again.
    moves: list[tuple[str, str]] = field(default_factory=list)
    # Of those, cart path -> size: same name and size but no checksum on record (files on the card
    # before the box kept records): run_plan reads each to confirm, and uploads it if it differs.
    unconfirmed: dict[str, int] = field(default_factory=dict)
    trust_moves: bool = False  # first_look = "trust": those aren't confirmed
    trusted: int = 0  # files taken as the library's by name and size (first_look)
    spot_checked: int = 0  # files a spot check found the same before trusting the rest
    spot_failed: str = ""  # library-relative: the file that stopped first_look "auto" trusting

    def is_extra(self, rel: str) -> bool:
        return key(rel) in self.extras

    @property
    def empty(self) -> bool:
        return not (self.add or self.replace or self.remove or self.remove_dirs or self.moves)

    @property
    def removals(self) -> int:
        """The app's Remove count: files and the folders they leave empty."""
        return len(self.remove) + len(self.remove_dirs)

    def summary(self) -> str:
        """Remove counts what the app shows: files and folders (split out when there are folders)."""
        split = f" ({_n(len(self.remove), 'file')}, {_n(len(self.remove_dirs), 'folder')})" if self.remove_dirs else ""
        skip = f", skipped {len(self.skipped)} (names the cart can't store)" if self.skipped else ""
        if self.left_out:
            skip += f", left out {_n(len(self.left_out), 'MSU game')} (msu is off)"
        moved = f", move {len(self.moves)}" if self.moves else ""
        return f"add {len(self.add)}{moved}, replace {len(self.replace)}, remove {self.removals}{split}{skip}"


class Eta:
    """Seconds left in a counted step, from how long the part done has taken."""

    def __init__(self, total: int, clock=time.monotonic):
        self.total = total
        self.clock = clock
        self.start = clock()

    def left(self, done: float) -> float | None:
        """None until something is done: nothing to go on yet."""
        return (self.clock() - self.start) / done * (self.total - done) if done > 0 else None


def _n(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


def _walk_failed(e: OSError) -> None:
    raise e


def library_roms(root: Path, suffixes=ROM_SUFFIXES, tick=lambda found: None) -> dict[str, str]:
    """{key: library-relative path} of every ROM under root (hidden files and folders skipped,
    and symlinks that lead outside root, so sync can only copy what is in the library).
    tick(ROMs found so far) after each folder: a big library on a network share takes a while."""
    out = {}
    top = Path(root).resolve()
    # A folder that can't be read stops the plan: skipped, its ROMs would look removed from the
    # library, and strict would delete them from the cart.
    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_failed):  # doesn't follow folder symlinks
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in sorted(filenames):
            if is_rom(name, suffixes) and top in Path(dirpath, name).resolve().parents:  # no links out of the library
                rel = Path(dirpath, name).relative_to(root).as_posix()
                out[key(rel)] = rel
        tick(len(out))
    return out


def library_stamp(root: Path) -> tuple:
    """A cheap fingerprint of the library: each folder's path and modification time (adding,
    removing or renaming a file changes its folder's). A network share sends no change events, so
    AppServer compares this every LIBRARY_CHECK seconds while the app shows a plan."""
    out = []
    for dirpath, dirnames, _ in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        try:
            out.append((dirpath, os.stat(dirpath).st_mtime_ns))
        except OSError:
            out.append((dirpath, None))
    return tuple(out)


def cart_tree(
    dev, dest: str, suffixes=None, tick=lambda folders: None
) -> tuple[dict[str, tuple[str, int]], dict[str, list[tuple[str, bool]]]]:
    """Every ROM under dest, never inside a system folder: {key of path below dest: (cart
    path, size)}; and every folder under dest (dest itself too) with its entries: {folder:
    [(cart path, is_dir)]}, hidden entries included (they keep a folder from being empty).

    Listings skip the menu-folder restore: this runs while the app is up, and the menu
    reloads its folder when it comes back."""
    roms, folders = {}, {}
    if dest and dev.stat(dest) is None:
        return roms, folders  # nothing synced here yet
    pending = [ops.norm(dest)]
    no_sizes = {"sizes": False} if getattr(dev, "stat_reads_file", False) else {}  # the FXPAK: size None
    while pending:
        tick(len(folders))
        folder = pending.pop()
        children = folders[folder] = []
        for e in dev.list_dir(folder, restore=None, **no_sizes):
            path = cart_join(folder, e.name)
            children.append((path, e.is_dir))
            if e.name.startswith(".") or in_system_dir(path) and "/" not in path:
                children[-1] = (path, None)  # never removed, never looked into
                continue
            if e.is_dir:
                pending.append(path)
            elif is_rom(e.name, suffixes or dev.profile.rom_suffixes):
                rel = path[len(ops.norm(dest)) :].lstrip("/")
                roms[key(rel)] = (path, e.size)
    return roms, folders


def cart_roms(dev, dest: str) -> dict[str, tuple[str, int]]:
    """{key of path below dest: (cart path, size)} of every ROM under dest (cart_tree)."""
    return cart_tree(dev, dest)[0]


def empty_after(folders: dict[str, list[tuple[str, bool]]], removed: set[str], root: str, *, emptied_only: bool):
    """Folders under root (never root) that hold nothing once `removed` (cart paths) are gone,
    deepest first. emptied_only: just the ones those removals empty, never an empty folder
    the owner made (mirror); otherwise every empty one (strict)."""
    gone = {key(p) for p in removed}
    out = []

    def visit(folder: str) -> bool:  # True: the folder is removed
        empty, hit = True, False
        for path, is_dir in folders.get(folder, ()):
            if is_dir:
                sub = visit(path)
                empty &= sub
                hit |= sub
            elif is_dir is None or key(path) not in gone:
                empty = False
            else:
                hit = True
        ok = empty and folder != root and (hit or not emptied_only)
        if ok:
            out.append(folder)
        return ok

    if root in folders:
        visit(root)
    return out  # children are appended before their parents


def known(manifest: Manifest, cart_path: str, size: int | None) -> dict | None:
    """The manifest's record of a cart file, if it still fits: same size, or any size when the
    listing had none (the FXPAK)."""
    k = manifest.get(cart_path)
    return k if k and (size is None or k["size"] == size) else None


def cart_sha(dev, manifest: Manifest, cart_path: str, size: int | None, on_chunk=None) -> tuple[int, str]:
    """(size, SHA-256) of a cart file: from the manifest, else by reading it (and recording).
    on_chunk(done, total), if given, follows the read."""
    k = known(manifest, cart_path, size)
    if k:
        return k["size"], k["sha"]
    got = ops.download(dev, cart_path, ops.NullSink(), progress=on_chunk)
    manifest.record(cart_path, got.size, got.sha256)
    return got.size, got.sha256


def confirm(dev, cart_path: str, size: int, local: Path) -> str | None:
    """local's SHA-256 if the cart file has the same contents, else None."""
    if dev.profile.crc:
        sha, crc = ops.file_hashes(local)
        return sha if ops.cart_crc(dev, cart_path, size) == crc else None
    sha = ops.sha256_file(local)
    return sha if ops.download(dev, cart_path, ops.NullSink()).sha256 == sha else None


def _first_look(dev, manifest: Manifest, hashes: LibraryHashes, cart_path, cart_size, src: Path, on_chunk) -> bool:
    """Is this cart file, nothing on record, the library's? Saved either way it's learned."""
    size = src.stat().st_size
    if dev.profile.crc:
        sha, crc = hashes.both(src)
        same = ops.cart_crc(dev, cart_path, size, on_chunk) == crc
        if same:
            manifest.record(cart_path, size, sha)
    else:
        got, sha = cart_sha(dev, manifest, cart_path, cart_size, on_chunk)
        same = got == size and sha == hashes.sha(src)
    hashes.save()  # as the manifest: a check cut short keeps what it did
    return same


def _spot_picks(unknown, sizes: dict, rate: float) -> list:
    """The files a spot check reads: a random sample, SPOT_CHECK_SECS' worth, at least one."""
    order = sorted(unknown)
    random.Random(len(order)).shuffle(order)
    picks, secs = [], 0.0
    for k in order:
        cost = sizes[k] / rate
        if not picks or secs + cost <= SPOT_CHECK_SECS:
            picks.append(k)
            secs += cost
    return picks


def make_plan(
    dev,
    library: Path,
    dest: str,
    mode: str,
    manifest: Manifest,
    *,
    progress=lambda msg, detail=None, left=None: None,
    keep=(),
    msu: str = "",
    msu_on: bool = False,
    first_look: str = "auto",
    tree: tuple | None = None,
    clock=time.monotonic,
) -> Plan:
    """keep: cart paths sync never adds, replaces or removes (the Sync Forever app).
    msu, msu_on: the cart's kind of MSU game (msu.py; "" none) and the console's setting. On,
    a game's MSU files sync with it: before its ROM, so a sync cut short never leaves a ROM
    without them; a copy on the cart the manifest doesn't know is taken as the library's (never
    read); a known one is compared by size; run_plan doesn't read them back. Off, MSU games are
    left out of the plan, and their copies on the cart left alone.
    first_look: what to do with cart files that have nothing on record (FIRST_LOOKS)."""
    if first_look not in FIRST_LOOKS:
        raise ValueError(f"unknown first_look {first_look!r}")
    if mode not in MODES:
        raise ValueError(f"unknown sync mode {mode!r}")
    # Long checks keep the app informed: it gives up on a silent box after 30 s and says HELLO,
    # and on the N8 and Mega those bytes land in the middle of a read ("read: 0x53", the S of
    # its frame; a big file's "Verifying 1/1", 2026-11-04).
    said_at = [clock()]

    def say(msg: str, detail: str | None = None, left: float | None = None) -> None:
        """left: the seconds a counted step has to go: the app then shows its full-screen view."""
        if left is None:
            progress(msg, detail)
        else:
            progress(msg, detail, left=max(1, round(left)))

    def keep_alive(msg: str, detail: str | None = None, left: float | None = None) -> None:
        if clock() - said_at[0] >= STATUS_EVERY:
            said_at[0] = clock()
            say(msg, detail, left)

    rom_suffixes = tuple(dev.profile.rom_suffixes)
    suffixes = rom_suffixes + (msu_mod.CART_SUFFIXES[msu] if msu and msu_on else ())
    hashes = LibraryHashes(manifest.path.with_name(manifest.path.stem + "-library.json"))
    # progress only for slow steps (each one rewrites the app's status line)
    local = library_roms(library, suffixes, lambda found: keep_alive("Scanning library", _n(found, "ROM")))
    # tree: a listing from an earlier plan, when nothing can have changed the cart since (the app
    # is open: AppServer plans again from it when the library changes).
    listed = (
        tree
        if tree is not None
        else cart_tree(dev, dest, suffixes, lambda folders: keep_alive("Reading the card", _n(folders, "folder")))
    )
    cart, folders = dict(listed[0]), listed[1]
    msu_games = msu_mod.games(msu, library, library_roms(library, ("",)), rom_suffixes) if msu else {}
    msu_files = {key(f) for files in msu_games.values() for f in files}
    kept = {key(p) for p in keep if p}
    root = ops.norm(dest)
    for k in [k for k, rel in local.items() if key(cart_join(dest, rel)) in kept]:
        del local[k]
    for k in [k for k, (path, _) in cart.items() if key(path) in kept]:
        del cart[k]
    if mode in ("mirror", "strict") and not local and cart:
        # An unmounted disk or share is an empty folder: "the library has nothing" would delete the cart's ROMs.
        raise ValueError("Library empty/offline")
    plan = Plan(mode, extras=frozenset(msu_files) if msu_on else frozenset(), tree=listed)
    if msu_on:
        plan.msu_games = len(msu_games)
        for files in msu_games.values():
            local.update({key(f): f for f in files})
    else:  # left out, and left alone on the cart: the games and their files
        plan.left_out = sorted(local[k] for k in msu_games if k in local)
        for k in [*msu_games, *msu_files]:
            local.pop(k, None)
            cart.pop(k, None)
    dev_ok = getattr(dev, "name_ok", None)  # the FXPAK: code page 1252 names only

    def name_ok(path: str) -> bool:
        return ops.fat_ok(path) and not in_system_dir(path) and (dev_ok is None or dev_ok(path))

    for k in [k for k, rel in local.items() if not name_ok(cart_join(dest, rel))]:
        plan.skipped.append(local.pop(k))
    # The cart files with nothing on record to check (the first look at a cart, on the FXPAK):
    # read in full, or checksummed on the cart where it can (CartProfile.crc). Counted
    # first so the status can say "Verifying 3/40".
    unknown = set()
    # And the files on record whose library copy has no checksum kept: read here, maybe over a network.
    unread = set()
    if mode != "add":
        for k, rel in local.items():
            if k in cart and not plan.is_extra(rel):
                cart_path, cart_size = cart[k]
                same_size = cart_size is None or cart_size == (library / rel).stat().st_size
                if same_size and not known(manifest, cart_path, cart_size):
                    unknown.add(k)
                elif same_size and not hashes.has(library / rel):
                    unread.add(k)
    checked = read = 0
    saved_at = clock()

    def ticker(status: str, name: str, eta: Eta, before: int):
        """on_chunk for a cart read, the `before`th of eta's files. Not during an FXPAK read: one
        GET, nothing else may go to the cart until it ends."""
        if getattr(dev, "stat_reads_file", False):
            return None
        return lambda done, total: keep_alive(
            status, f"{name} {done * 100 // total if total else 0}%", eta.left(before + (done / total if total else 0))
        )

    # Files taken as the library's, unread. The FXPAK's listing has no sizes: only "trust" takes
    # its files, by name alone; "auto" always checks them.
    trusting = set()
    if first_look == "trust":
        trusting = set(unknown)
        plan.trust_moves = True
    elif first_look == "auto" and unknown and all(cart[k][1] is not None for k in unknown):
        sizes = {k: cart[k][1] for k in unknown}
        rate = CART_CRC_RATE if dev.profile.crc else Speeds(dev, manifest).rate("back") * 1024
        if sum(sizes.values()) / rate > FIRST_LOOK_LIMIT:
            picks = _spot_picks(unknown, sizes, rate)
            eta = Eta(len(picks), clock)
            for n, k in enumerate(picks, 1):
                status, rel = f"Spot check {n}/{len(picks)}", local[k]
                say(status, Path(rel).name, eta.left(n - 1))
                said_at[0] = clock()
                tick = ticker(status, Path(rel).name, eta, n - 1)
                if not _first_look(dev, manifest, hashes, *cart[k], library / rel, tick):
                    plan.spot_failed = rel
                    break
                unknown.discard(k)
                plan.spot_checked += 1
            if not plan.spot_failed:
                trusting = set(unknown)
    unknown -= trusting
    unread |= {k for k in trusting if not hashes.has(library / local[k])}
    reading, checking = Eta(len(unread), clock), Eta(len(unknown), clock)
    for index, (k, rel) in enumerate(local.items(), 1):
        keep_alive("Checking library", f"{index} of {len(local)}")
        if k not in cart:
            plan.add.append(rel)
            continue
        if mode == "add":
            plan.unchanged += 1
            continue
        cart_path, cart_size = cart[k]  # size None: the listing had none (FXPAK)
        src = library / rel
        src_size = src.stat().st_size
        if cart_size is not None and src_size != cart_size:
            plan.replace.append(rel)
            continue
        if plan.is_extra(rel):
            k_known = manifest.get(cart_path)
            if k_known is None:  # on the cart already, by whatever means: trusted by name
                manifest.record(cart_path, src_size, "")
                plan.unchanged += 1
            elif k_known["size"] != src_size:
                plan.replace.append(rel)
            else:
                plan.unchanged += 1
            continue
        if k in unread:
            read += 1
            keep_alive(f"Library {read}/{len(unread)}", Path(rel).name, reading.left(read - 1))
            if clock() - saved_at >= HASHES_EVERY:  # a plan cut short keeps what it read
                saved_at = clock()
                hashes.save()
        if k in trusting:
            manifest.record(cart_path, src_size, hashes.sha(src))
            plan.trusted += 1
            plan.unchanged += 1
            continue
        if k in unknown:  # this one means asking the cart
            checked += 1
            status, name = f"Verifying {checked}/{len(unknown)}", Path(rel).name
            say(status, name, checking.left(checked - 1))
            said_at[0] = clock()
            tick = ticker(status, name, checking, checked - 1)
            if _first_look(dev, manifest, hashes, cart_path, cart_size, src, tick):
                plan.unchanged += 1
            else:
                plan.replace.append(rel)
            continue
        size, sha = cart_sha(dev, manifest, cart_path, cart_size)
        if size != src_size or sha != hashes.sha(src):
            plan.replace.append(rel)
        else:
            plan.unchanged += 1
    # A game's extras before its ROM (folder by folder, as found).
    order = lambda rel: (str(Path(rel).parent).casefold(), not plan.is_extra(rel), rel.casefold())
    plan.add.sort(key=order)
    plan.replace.sort(key=order)
    if mode == "strict":
        plan.remove = sorted(path for k, (path, _) in cart.items() if k not in local)
    elif mode == "mirror":
        owned = {key(p) for p in manifest.owned()}
        plan.remove = sorted(path for k, (path, _) in cart.items() if k not in local and key(path) in owned)
    if plan.remove and plan.add:
        _find_moves(dev, plan, library, dest, manifest, hashes, keep_alive)
        _find_likely_moves(dev, plan, library, dest, manifest, cart, keep_alive)
    if mode in ("strict", "mirror"):
        moved_from = {src for src, _ in plan.moves}
        plan.remove_dirs = empty_after(folders, set(plan.remove) | moved_from, root, emptied_only=mode == "mirror")
    hashes.save()
    return plan


def _make_folders(dev, policy, path: str) -> None:
    """The folders above path, as an upload makes them."""
    parts = path.split("/")[:-1]
    for i in range(1, len(parts) + 1):
        folder = "/".join(parts[:i])
        if dev.stat(folder) is None:
            dev.mkdir(policy.check(folder))


def can_move(dev, src: str, dst: str) -> bool:
    """Whether a sync may rename src to dst on this cart (the profile's moves and move_folders)."""
    profile = dev.profile
    if not profile.move:
        return False
    return profile.move != "rename" or profile.move_folders or key(str(Path(src).parent)) == key(str(Path(dst).parent))


def _find_likely_moves(dev, plan: Plan, library: Path, dest: str, manifest: Manifest, cart: dict, keep_alive) -> None:
    """Files to add with the name and size of a file to remove that has no checksum on record, as
    when a library is reorganised into folders: likely moves. run_plan reads each cart file to be
    sure; it's never trusted unread. A listing without sizes (the FXPAK) can't do this."""
    sizes_of = dict(cart.values())
    alike: dict[tuple[str, int], list[str]] = {}
    for path in plan.remove:
        k = manifest.get(path)
        if not (k and k.get("sha")) and sizes_of.get(path) is not None:
            alike.setdefault((Path(path).name.casefold(), sizes_of[path]), []).append(path)
    if not alike:
        return
    adding = list(plan.add)
    for index, rel in enumerate(adding, 1):
        keep_alive("Looking for moves", f"{index} of {len(adding)}")
        same = alike.get((Path(rel).name.casefold(), (library / rel).stat().st_size), [])
        match = next((c for c in same if can_move(dev, c, cart_join(dest, rel))), None)
        if match is not None:
            same.remove(match)
            plan.moves.append((match, rel))
            plan.unconfirmed[match] = sizes_of[match]
            plan.add.remove(rel)
            plan.remove.remove(match)


def _find_moves(
    dev, plan: Plan, library: Path, dest: str, manifest: Manifest, hashes: LibraryHashes, keep_alive
) -> None:
    """Files to add whose contents a file to remove already has: moved instead. Only cart files
    whose size and SHA-256 the manifest records (box-made copies and files it has read); the
    library side is hashed only for a size some removal has."""
    gone: dict[tuple[int, str], list[str]] = {}
    for path in plan.remove:
        k = manifest.get(path)
        if k and k.get("size", -1) >= 0 and k.get("sha"):
            gone.setdefault((k["size"], k["sha"]), []).append(path)
    if not gone:
        return
    sizes = {size for size, _ in gone}
    adding = list(plan.add)
    for index, rel in enumerate(adding, 1):
        keep_alive("Looking for moves", f"{index} of {len(adding)}")
        src = library / rel
        size = src.stat().st_size
        if size not in sizes:
            continue
        candidates = gone.get((size, hashes.sha(src)), [])
        dst = cart_join(dest, rel)
        match = next((c for c in candidates if can_move(dev, c, dst)), None)
        if match is None:
            continue
        candidates.remove(match)
        plan.moves.append((match, rel))
        plan.add.remove(rel)
        plan.remove.remove(match)


def _mb(size: int) -> str:
    return f"{size / (1 << 20):.1f} MB" if size >= 1 << 20 else f"{size >> 10} KB"


def _logging(cb, log, name, size, clock):
    """cb (or nothing), plus a log line every LOG_EVERY seconds: bytes done, speed so far."""
    start = clock()
    next_at = start + LOG_EVERY

    def watch(done, total):
        nonlocal next_at
        if cb:
            cb(done, total)
        now = clock()
        if now >= next_at:
            next_at = now + LOG_EVERY
            rate = done / 1024 / (now - start)
            log(f"  {name}: {done / (1 << 20):.1f} of {_mb(size)}, {rate:.0f} KiB/s")

    return watch


def _stage(copying, n, total, name, stage, size):
    """An ops progress callback (done, total) that reports one stage of one copy, or None."""
    if copying is None:
        return None
    return lambda done, _total: copying(n, total, name, stage, done, size)


class Speeds:
    """What a sync expects of the cart, from what it has measured: KiB/s up and back (bytes over
    seconds, so big files weigh most: on the FXPAK a small ROM went up at ~315 KiB/s, a 44 MB
    track nearer 210), and the seconds around each copy (the FXPAK's autosave wait, opening,
    closing). Kept in the cart's manifest, so a sync starts from this setup's own figures; until
    a cart has any, its profile's (upload_kib_s) or the defaults. The totals are scaled down past
    SPEED_MEMORY, so the figures follow a new cable or card."""

    def __init__(self, dev, manifest: Manifest | None = None):
        self.saved = manifest.speeds if manifest is not None else {}
        self.defaults = {
            "up": getattr(dev, "upload_kib_s", DEFAULT_UP),
            "back": getattr(dev, "read_kib_s", DEFAULT_READ),
        }
        for k in ("up", "back", "around"):
            self.saved.setdefault(k, [0, 0.0])

    def rate(self, which: str) -> float:
        size, secs = self.saved[which]
        return size / 1024 / secs if size >= 1 << 20 and secs else self.defaults[which]

    def around(self) -> float:
        n, secs = self.saved["around"]
        return secs / n if n else EXPECT_SLACK

    def add(self, which: str, amount: float, seconds: float) -> None:
        t = self.saved[which]
        t[0] += amount
        t[1] += seconds
        limit = SPEED_MEMORY if which != "around" else AROUND_MEMORY
        if t[0] > limit:  # older measurements count for less
            t[0], t[1] = t[0] / 2, t[1] / 2

    def back_secs(self, size: int) -> float:
        return size / 1024 / self.rate("back")

    def copy_secs(self, size: int, checked: bool) -> float:
        return size / 1024 / self.rate("up") + (self.back_secs(size) if checked else 0) + self.around()


def run_plan(
    dev,
    plan: Plan,
    library: Path,
    dest: str,
    manifest: Manifest,
    *,
    policy,
    progress=lambda msg, detail=None, expect=None, left=None: None,
    copying=None,
    log=None,
    clock=time.monotonic,
    stop=lambda: False,
):
    """Carry out a plan; every copy is read back and checked (but extras: make_plan). Returns
    the plan's summary.
    copying(n, total, name, stage, done, size), if given, follows each copy byte by byte
    (stage COPYING, then CHECKING for the readback). progress(status, detail, expect, left):
    the status line, each phase counted on its own ("Copying 3 of 61", "Replacing 1 of 2",
    "Removing 4 of 18"), the line under it (the file's name), and for copies
    how many seconds this one and everything left (this one included) should take, at the
    speeds this sync has seen (the apps count it up: the box can't speak to the SNES app during
    an upload). log(line), if given, gets a line per copy and removal, and for a big copy one as
    it starts and every LOG_EVERY seconds: in the box's journal, a long copy that's working
    shows its bytes going up. stop(), asked before each copy and removal, ends the run there
    (the app's B): plan.stopped then says how many were done. A read-back long enough to matter
    (STATUS_EVERY) gets its own status, "Checking n of N". The apps send STOP as a STATUS arrives,
    so stop() is asked right after that one too: a STOP landing in the read would break it."""
    copies = [(r, False) for r in plan.add] + [(r, True) for r in plan.replace]
    sizes = {rel: (library / rel).stat().st_size for rel, _ in copies}
    total = len(plan.moves) + len(copies) + plan.removals
    speeds = Speeds(dev, manifest)

    def move_secs(src: str) -> int:
        """A move's seconds (the app waits by this): confirming it, and a cart's own copy."""
        size = plan.unconfirmed.get(src)
        secs = 0
        if size is not None and not plan.trust_moves:
            secs = size / CART_CRC_RATE if dev.profile.crc else size / 1024 / speeds.rate("back")
        if dev.profile.move == "copy":
            secs += (size if size is not None else manifest.get(src)["size"]) / CART_COPY_RATE + EXPECT_SLACK
        return round(secs)

    def left_secs(from_index: int, moves_from: int | None = None) -> int:
        rest = copies[from_index:]
        secs = sum(speeds.copy_secs(sizes[r], not plan.is_extra(r)) for r, _ in rest)
        if moves_from is not None:
            secs += sum(move_secs(src) for src, _ in plan.moves[moves_from:])
        return round(secs + plan.removals * REMOVE_SECS)

    def stopped(n: int) -> bool:
        if stop():
            plan.stopped = n - 1
            if log:
                log(f"stopped by the app after {n - 1} of {total}")
            return True
        return False

    n = 0
    for index, (src, rel) in enumerate(plan.moves):  # first: instant, and they free their names
        n += 1
        plan.done, plan.doing = n - 1, Path(rel).name
        dst = guard(cart_join(dest, rel))
        progress(
            f"Moving {index + 1} of {len(plan.moves)}", Path(rel).name, expect=move_secs(src), left=left_secs(0, index)
        )
        if stopped(n):
            return plan.summary()
        if src in plan.unconfirmed:  # the same name and size: check it to be sure
            sha = (
                ops.sha256_file(library / rel)
                if plan.trust_moves
                else confirm(dev, src, plan.unconfirmed[src], library / rel)
            )
            if sha is None:  # another file after all: uploaded, this one goes
                if log:
                    log(f"not the same file: {src} (uploading {rel})")
                plan.remove.append(src)
                sizes[rel] = (library / rel).stat().st_size
                copies.insert(sum(not o for _, o in copies), (rel, False))
                continue
            manifest.record(src, plan.unconfirmed[src], sha)
        _make_folders(dev, policy, dst)
        record = manifest.get(src)
        if dev.profile.move == "copy":  # the cart copies it (no data over USB), then the old goes
            dev.copy(policy.check(guard(src)), policy.check(dst), wait=COPY_WAIT + record["size"] / CART_COPY_RATE)
            manifest.record(dst, record["size"], record["sha"], owned=True)
            dev.delete(policy.check(guard(src)))
        else:
            dev.move(policy.check(guard(src)), policy.check(dst))
            manifest.record(dst, record["size"], record["sha"], owned=True)
        manifest.forget(src)
        if log:
            log(f"moved {src} -> {dst}")
    n_add = sum(not o for _, o in copies)
    for index, (rel, overwrite) in enumerate(copies):
        n += 1
        plan.done, plan.doing = n - 1, Path(rel).name
        phase, k, of = (
            ("Copying", index + 1, n_add) if not overwrite else ("Replacing", index + 1 - n_add, len(plan.replace))
        )
        cart_path = guard(cart_join(dest, rel))
        src = library / rel
        size = sizes[rel]
        name = Path(rel).name
        checked = not plan.is_extra(rel)  # extras: too big to read back (make_plan)
        shown = name if size < BIG else f"{name} ({size >> 20} MB)"
        says_checking = checked and speeds.back_secs(size) >= STATUS_EVERY
        expect = round(speeds.copy_secs(size, checked and not says_checking))
        started = clock()
        progress(f"{phase} {k} of {of}", shown, expect=expect, left=left_secs(index))
        if stopped(n):
            return plan.summary()
        bytes_cb = _stage(copying, n, total, name, COPYING, size)
        check_cb = _stage(copying, n, total, name, CHECKING, size)
        if log and size >= BIG:
            log(f"copying {rel} ({_mb(size)})")
            bytes_cb = _logging(bytes_cb, log, name, size, clock)
        # A copy cut short leaves a file the next plan must not trust (an MSU file is taken by
        # name: make_plan): recorded as unfinished until it's done.
        manifest.record(cart_path, -1, "", owned=True)
        with open(src, "rb") as f:
            up = ops.upload(
                dev, f, cart_path, policy=policy, overwrite=overwrite, menu_check=False, total=size, progress=bytes_cb
            )
        speeds.add("up", size, up.seconds)
        moved = up.seconds
        stopping = False
        if says_checking:
            progress(
                f"Checking {k} of {of}",
                shown,
                expect=round(speeds.back_secs(size)),
                left=round(speeds.back_secs(size)) + left_secs(index + 1),
            )
            stopping = stop()
        if checked:
            back = ops.verify(dev, cart_path, up.sha256, progress=check_cb)
            speeds.add("back", size, back.seconds)
            moved += back.seconds
        speeds.add("around", 1, max(0.0, clock() - started - moved))
        manifest.record(cart_path, up.size, up.sha256, owned=True)  # saves the speeds too
        if index == 0 and getattr(dev, "after_first_copy", None):  # the FXPAK app (fxapp.py)
            dev.after_first_copy()
        if log:
            how = "checked" if checked else "not read back"
            log(f"copied {rel} ({_mb(size)}) in {up.seconds:.0f} s, {up.kib_per_s:.0f} KiB/s, {how}")
        if stopping:
            plan.stopped = n
            if log:
                log(f"stopped by the app after {n} of {total}")
            return plan.summary()
    removals = [(p, p) for p in plan.remove] + [(f, f + "/") for f in plan.remove_dirs]  # folders deepest first
    if removals and not library_roms(library, dev.profile.rom_suffixes):
        raise ValueError("Library offline: none removed")
    for index, (cart_path, label) in enumerate(removals):
        n += 1
        plan.done, plan.doing = n - 1, Path(label.rstrip("/")).name
        progress(
            f"Removing {index + 1} of {len(removals)}",
            Path(label.rstrip("/")).name + ("/" if label.endswith("/") else ""),
            left=round((len(removals) - index) * REMOVE_SECS),
        )
        if stopped(n):
            return plan.summary()
        dev.delete(policy.check(guard(cart_path)))
        if not label.endswith("/"):
            manifest.forget(cart_path)
        if log:
            log(f"removed {label}")
    return plan.summary()
