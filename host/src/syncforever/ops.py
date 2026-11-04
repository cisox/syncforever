"""Safe file operations on top of N8Pro: write gating, hashing, readback, benchmarks."""

import hashlib
import io
import random
import time
import zlib
from contextlib import suppress
from dataclasses import dataclass

from .protocol import (
    FA_CREATE_ALWAYS,
    FA_CREATE_NEW,
    FA_READ,
    FA_WRITE,
    FR_NO_FILE,
    DeviceError,
    EverDriveError,
    UnsafeStateError,
    WedgedError,
)

DEFAULT_WRITE_ROOT = "syncforever-test"

# Each F_FWR is all-or-wedge, so keep single commands short enough that a
# deferred Ctrl-C is honored quickly. Reads use the same size for symmetric benchmarks.
CHUNK = 64 * 1024


class WriteNotAllowed(EverDriveError):
    pass


class VerifyError(EverDriveError):
    pass


def norm(path):
    """Cart path without leading/trailing slashes; '' is the root."""
    parts = [p for p in path.split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise ValueError(f"relative path components are not allowed: {path!r}")
    return "/".join(parts)


FAT_BAD = set('"*:<>?\\|') | {chr(c) for c in range(32)}


def fat_name(name: str) -> str:
    """A name as FAT stores it: trailing dots and spaces dropped ("Super Smash T.V." is kept as
    "Super Smash T.V"; FatFs strips them on create and on lookup)."""
    return name.rstrip(" .")


def fat_key(path: str) -> str:
    """How FAT matches a path: each name as it stores it, case-insensitive."""
    return "/".join(fat_name(p) for p in norm(path).split("/")).casefold()


def fat_ok(path: str) -> bool:
    """Whether FAT can hold every name in the path: none of its forbidden characters, and
    something left once trailing dots and spaces go."""
    return all(fat_name(p) and not FAT_BAD.intersection(p) for p in norm(path).split("/"))


class WritePolicy:
    """Writes and deletes are allowed only under these roots unless anywhere=True."""

    def __init__(self, roots=(DEFAULT_WRITE_ROOT,), anywhere=False):
        self.roots = [norm(r) for r in roots]
        self.anywhere = anywhere

    def check(self, path):
        p = norm(path)
        if self.anywhere:
            return p
        for root in self.roots:
            if root and (p == root or p.startswith(root + "/")):
                return p
        allowed = ", ".join(f"/{r}" for r in self.roots)
        raise WriteNotAllowed(f"/{p} is outside the write roots ({allowed}); pass --allow-write-anywhere to override")


def require_menu(dev, doing="writing"):
    if not dev.menu_ping():
        raise UnsafeStateError(f"the cart's menu did not answer; return the console to the ROM menu before {doing}")


def launch(dev, path):
    """Start the ROM at path through the menu (install, then *s; carts.py). Returns the
    mapper index, or None on carts that don't report it."""
    path = norm(path)
    require_menu(dev, "launching")
    entry = dev.stat(path)
    if entry is None or entry.is_dir:
        raise ValueError(f"no ROM at /{path}")
    mapper = dev.menu_install(path)
    dev.menu_start()
    return mapper


@dataclass(frozen=True)
class TransferResult:
    path: str
    size: int
    sha256: str
    seconds: float

    @property
    def kib_per_s(self):
        return self.size / 1024 / self.seconds if self.seconds else 0.0


class NullSink:
    """download() target when only the SHA-256 matters."""

    def write(self, data):
        pass


_HashSink = NullSink


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def file_hashes(path) -> tuple[str, int]:
    """(SHA-256, CRC-32) of a local file, in one read."""
    sha, crc = hashlib.sha256(), 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            sha.update(chunk)
            crc = zlib.crc32(chunk, crc)
    return sha.hexdigest(), crc


# Bytes per F_FCRC: ~4 s at the Mega's 4 MB/s, so a big file doesn't silence the box past the
# app's wait (the box sends a STATUS between chunks, tick).
CRC_CHUNK = 16 << 20


def cart_crc(dev, path, size, tick=None) -> int:
    """CRC-32 of a cart file, computed on the cart. tick(done, size) after each chunk."""
    dev.open(path, FA_READ)
    try:
        crc = done = 0
        while done < size:
            n = min(CRC_CHUNK, size - done)
            crc = dev.file_crc(n, crc)
            done += n
            if tick:
                tick(done, size)
    finally:
        dev.close()
    return crc


def download(dev, path, dst, *, chunk=CHUNK, progress=None):
    t0 = time.monotonic()
    if getattr(dev, "stat_reads_file", False):  # the FXPAK: the GET that opens it gives the size
        dev.open(path, FA_READ)  # FR_NO_FILE if it isn't there (or is a folder)
        size = dev.open_size
    else:
        entry = dev.stat(path)
        if entry is None:
            raise DeviceError(f"get {path!r}", FR_NO_FILE)
        if entry.is_dir:
            raise EverDriveError(f"{path!r} is a folder")
        size = entry.size
        dev.open(path, FA_READ)
    h = hashlib.sha256()
    done = 0
    try:
        while done < size:
            data = dev.read(min(chunk, size - done))
            dst.write(data)
            h.update(data)
            done += len(data)
            if progress:
                progress(done, size)
    except BaseException:
        with suppress(EverDriveError):
            dev.close()
        raise
    dev.close()
    return TransferResult(path, done, h.hexdigest(), time.monotonic() - t0)


def make_parents(dev, path):
    parts = norm(path).split("/")[:-1]
    for i in range(1, len(parts) + 1):
        dev.mkdir("/".join(parts[:i]))


def upload(dev, src, path, *, policy, overwrite=False, chunk=CHUNK, progress=None, total=None, menu_check=True):
    """Stream src to path. Without overwrite an existing file is an error (FR_EXIST).

    menu_check=False is only for the app's own session: our app is running and idle
    (it asked for the copy), so the menu can't answer the ping and the ping bytes would
    land in the app's FIFO.
    """
    path = policy.check(path)
    if menu_check:
        require_menu(dev)
    make_parents(dev, path)
    mode = FA_WRITE | (FA_CREATE_ALWAYS if overwrite else FA_CREATE_NEW)
    if getattr(dev, "open_takes_size", False):  # the FXPAK: a PUT declares its length first
        if total is None:
            here = src.tell()
            total = src.seek(0, 2) - here
            src.seek(here)
        dev.open(path, mode, size=total)
    else:
        dev.open(path, mode)
    h = hashlib.sha256()
    t0 = time.monotonic()
    done = 0
    try:
        while data := src.read(chunk):
            dev.write(data)
            h.update(data)
            done += len(data)
            if progress:
                progress(done, total)
    except WedgedError:
        raise
    except BaseException:
        with suppress(EverDriveError):
            dev.close()
            if not overwrite:
                # We created this file, so a partial copy is ours to remove.
                dev.delete(path)
        raise
    dev.close()
    return TransferResult(path, done, h.hexdigest(), time.monotonic() - t0)


def verify(dev, path, expected_sha256, *, chunk=CHUNK, progress=None):
    result = download(dev, path, _HashSink(), chunk=chunk, progress=progress)
    if result.sha256 != expected_sha256:
        raise VerifyError(f"{path}: readback sha256 {result.sha256} != sent {expected_sha256}")
    return result


@dataclass(frozen=True)
class BenchResult:
    size: int
    upload: TransferResult
    download: TransferResult
    match: bool


def bench(dev, folder, sizes, *, policy, seed=0x4E38, keep=False, report=None):
    """Upload, read back and (unless keep) delete a file of each size; report(result) after
    each one, so a stall shows where it happened."""
    results = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for size in sizes:
        data = random.Random(seed + size).randbytes(size)
        path = f"{norm(folder)}/bench-{size}-{stamp}.bin"
        up = upload(dev, io.BytesIO(data), path, policy=policy)
        down = download(dev, path, _HashSink())
        results.append(BenchResult(size, up, down, up.sha256 == down.sha256))
        if report:
            report(results[-1])
        if not keep:
            dev.delete(path)
    return results
