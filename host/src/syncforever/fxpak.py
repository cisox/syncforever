"""FXPAK PRO (SNES): the cart's own USB file service (docs/snes-plan.md), behind the same file
interface as N8Pro (stat, list_dir, open/read/write/close, mkdir, delete), so ops.py and the
sync and save code work unchanged.

Every request and every data block is 512 bytes. The cart's MCU does all SD card access itself
(the SNES menu asks it for files the same way), so there's no menu to keep clear of.

Firmware 1.11.2 (the owner's, 2026-11-03):
- LS gives names and folder/file only, no sizes. A file's size comes from GET's response.
- **Never cut a GET short.** The MCU's read position survives an aborted transfer (dropping
  DTR): the next GET starts that far into its file and sends that much less (measured: +512
  bytes), and once it's past a file's end the MCU sends nothing, stays busy, and stops taking
  USB data (only a power cycle recovers; replugging USB doesn't). So every GET is read to its
  end, sizes included (`_size` reads the whole file).
- **GET of a path that doesn't exist hangs the MCU the same way** (its size is the previous
  file's). So nothing here sends GET without first finding the name in an LS of its folder
  (`_find`).
- Names on the wire are Windows-1252, not UTF-8: the firmware's FatFs is built with code page
  1252 and no Unicode (src/ffconf.h). LS sends a card's "é" as one byte 0xE9, and a path must
  name it the same way. A name with anything 1252 hasn't got (Japanese, say) can't be stored.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from .n8pro import DirEntry
from .ops import fat_key
from .protocol import FA_CREATE_NEW, FA_READ, FA_WRITE, FR_EXIST, FR_NO_FILE, DeviceError, LinkError, WedgedError

VID, PID = 0x1209, 0x5A22
BLOCK = 512
MAGIC = b"USBA"

GET, PUT, LS, MKDIR, RM, MV, INFO = 0, 1, 4, 5, 6, 7, 11
BOOT, MENU_RESET = 9, 12  # only for our app: exit to the menu, restart after an update
RESPONSE = 0x0F
FILE, SNES = 0, 1
SRAM_BASE = 0xE00000  # SNES space: the running game's save RAM (ROM is from 0)

LS_DIR, LS_FILE, LS_MORE, LS_END = 0, 1, 2, 0xFF
PATH_MAX = 255
EMPTY_GET_DTR = 0.05  # seconds DTR stays low to idle the MCU after an empty file's GET
BLOCK_GAP = 0.001  # seconds between a PUT's data blocks (see FxPak.block_gap)
# Seconds a write to the cart may wait. The first new file after power-on can take far longer
# than 10 s to start: FatFs looks for free space from the card's FSInfo hint, inside the USB
# interrupt, and after a computer filled the card (46 GB of MSU-1, 2026-11-04) that meant walking
# millions of FAT entries; giving up left the file unclosed and the hint stale, so every try did it
# again. A real stall is still caught, just later.
WRITE_TIMEOUT = 120
CODEPAGE = "cp1252"


def name_bytes(name: str) -> bytes:
    """A name or path as the cart spells it. surrogateescape: the 5 bytes 1252 leaves undefined
    round-trip as they came from an LS. ValueError: the cart can't store this name."""
    try:
        return name.encode(CODEPAGE, "surrogateescape")
    except UnicodeEncodeError:
        raise ValueError(f"the FXPAK can't store this name (code page 1252 only): {name!r}") from None


def name_str(raw: bytes) -> str:
    return raw.decode(CODEPAGE, "surrogateescape")


def name_ok(path: str) -> bool:
    """Whether the cart can store this name (romsync skips library files it can't)."""
    try:
        name_bytes(path)
    except ValueError:
        return False
    return True


def is_fxpak(port) -> bool:
    return (port.vid, port.pid) == (VID, PID)


def find_ports():
    from serial.tools import list_ports

    return [p for p in list_ports.comports() if is_fxpak(p)]


def request(op: int, space: int = FILE, *, path: str = "", size: int = 0, flags: int = 0) -> bytes:
    b = bytearray(BLOCK)
    b[0:4] = MAGIC
    b[4], b[5], b[6] = op, space, flags
    struct.pack_into(">I", b, 252, size)
    raw = name_bytes("/" + path.strip("/"))
    if len(raw) > PATH_MAX:
        raise ValueError(f"path too long for the FXPAK ({len(raw)} bytes): {path!r}")
    b[256 : 256 + len(raw)] = raw
    return bytes(b)


def mem_request(op: int, addr: int, size: int) -> bytes:
    """GET/PUT in SNES space: the cart's memory, by the FPGA's address (ROM from 0, SRAM from
    SRAM_BASE). The address goes where a path would."""
    b = bytearray(request(op, SNES, size=size))
    b[256:512] = bytes(256)
    struct.pack_into(">I", b, 256, addr)
    return bytes(b)


def _cstr(b: bytes) -> str:
    return name_str(b.split(b"\0", 1)[0])


@dataclass(frozen=True)
class Info:
    version: str  # "1.11.2"
    device: str  # "FXPAK PRO STM32"
    rom: str  # the running ROM ("/sd2snes/m3nu.bin" at the menu)
    features: int
    config: int

    @property
    def at_menu(self) -> bool:
        return self.rom.lower() == "/sd2snes/m3nu.bin"


def parse_ls(block: bytes) -> tuple[list[tuple[bool, str]], bool]:
    """One LS data block: [(is_dir, name)], and whether the listing ended in it."""
    out, i = [], 0
    while i < len(block):
        kind = block[i]
        if kind == LS_END:
            return out, True
        if kind == LS_MORE:
            return out, False
        if kind not in (LS_DIR, LS_FILE):
            raise LinkError(f"FXPAK listing: unexpected entry type 0x{kind:02X}")
        end = block.index(b"\0", i + 1)
        out.append((kind == LS_DIR, name_str(block[i + 1 : end])))
        i = end + 1
    return out, False


def _split(path: str) -> tuple[str, str]:
    p = path.strip("/")
    if "/" in p:
        folder, name = p.rsplit("/", 1)
        return folder, name
    return "", p


class FxPak:
    """The cart, over a pyserial port (or anything with write/read/reset_input_buffer and a dtr
    attribute: the fake in tests)."""

    open_takes_size = True  # ops.upload passes the file's size to open(): PUT declares it first
    # A size costs reading the whole file (LS has none on 1.11.2): list_dir(sizes=False) gives
    # None, ops.download takes the size from the GET it opens, and sync trusts its manifest.
    stat_reads_file = True
    upload_kib_s, read_kib_s = 220.0, 670.0  # paced uploads (big files ~210) and reads, on the Pi (2026-11-03)
    name_ok = staticmethod(name_ok)  # names are code page 1252 (module docstring)

    def __init__(self, port, timeout: float = 5.0):
        from .carts import FXPAK

        self.port = port
        self.t = port  # what the CLI closes
        self.timeout = timeout
        self.profile = FXPAK
        self._read: tuple[int, bytearray, int] | None = None  # an open GET (read())
        # Upload pacing: after each 512-byte block of a PUT's data, wait until the host has sent
        # it, then `block_gap` seconds. Sent back to back, the blocks stall the cart's USB for
        # good now and then (power cycle only); paced like this they didn't (docs/snes-plan.md).
        self.block_gap = BLOCK_GAP
        self._write: dict | None = None

    # -- transport -------------------------------------------------------------------------

    def _recv(self, n: int = BLOCK) -> bytes:
        out = bytearray()
        end = time.monotonic() + self.timeout
        while len(out) < n:
            got = self.port.read(n - len(out))
            if got:
                out += got
                end = time.monotonic() + self.timeout
            elif time.monotonic() > end:
                raise LinkError(f"FXPAK: no reply ({len(out)}/{n} bytes)")
        return bytes(out)

    def _call(self, req: bytes) -> tuple[int, int, bytes]:
        """Send a request; (error, size, the response block)."""
        if self._read is not None or self._write is not None:
            raise LinkError("FXPAK: a transfer is still open")
        self.port.reset_input_buffer()  # nothing is owed between requests: drop any stray block
        self.port.write(req)
        r = self._recv()
        if r[:4] != MAGIC or r[4] != RESPONSE:
            raise LinkError(f"FXPAK: bad response {r[:8].hex()}")
        return r[5], struct.unpack_from(">I", r, 252)[0], r

    def _drain(self, size: int) -> None:
        """Take the rest of a GET's data blocks (never abort one: see the module docstring)."""
        for _ in range(-(-size // BLOCK)):
            self._recv()

    def _end_empty_get(self) -> None:
        """A GET of an empty file sends no data block and leaves the MCU busy (measured): drop
        DTR to idle it. Safe here only because nothing was sent, so no read position is left."""
        self.port.dtr = False
        time.sleep(EMPTY_GET_DTR)
        self.port.dtr = True
        time.sleep(EMPTY_GET_DTR)
        self.port.reset_input_buffer()

    # -- cart ------------------------------------------------------------------------------

    def info(self) -> Info:
        err, _size, r = self._call(request(INFO, SNES))
        if err:
            raise DeviceError("info", err)
        return Info(_cstr(r[260:324]), _cstr(r[324:388]), _cstr(r[16:256]), r[6] | r[7] << 8, r[10] | r[11] << 8)

    def menu_ping(self) -> bool:
        """The menu needn't be up for file work: the MCU serialises SD access itself."""
        return True

    # -- files -----------------------------------------------------------------------------

    def _ls(self, path: str) -> list[tuple[bool, str]] | None:
        """Names in a folder, or None if it isn't one (LS of a missing folder answers an error
        and still sends its end block)."""
        err, _size, _r = self._call(request(LS, path=path))
        entries, done = [], False
        while not done:
            got, done = parse_ls(self._recv())
            entries += got
        if err:
            return None
        return [(d, n) for d, n in entries if n not in (".", "..")]

    def _find(self, path: str) -> tuple[bool, str] | None:
        """(is_dir, the name as the card spells it), or None: never GET what this didn't find."""
        folder, name = _split(path)
        if not name:
            return (True, "")
        entries = self._ls(folder)
        if entries is None:
            return None
        key = fat_key(name)  # FAT: no case, no trailing dots or spaces
        return next(((d, n) for d, n in entries if fat_key(n) == key), None)

    def _size(self, path: str) -> int:
        """A file's size, from GET's response; its data is read and dropped (a GET is never cut
        short). Only for paths _find has just seen (GET of a missing file hangs the cart)."""
        err, size, _r = self._call(request(GET, path=path))
        if err:
            raise DeviceError(f"size {path!r}", err)  # unreachable for a file LS listed
        if size:
            self._drain(size)
        else:
            self._end_empty_get()
        return size

    def stat(self, path: str) -> DirEntry | None:
        found = self._find(path)
        if found is None:
            return None
        is_dir, name = found
        size = 0 if is_dir else self._size(path)
        return DirEntry(name or "/", size, is_dir, None)

    def read_whole(self, path: str) -> bytes | None:
        """A file's bytes in one GET (no separate size check); None if it isn't there."""
        found = self._find(path)
        if found is None or found[0]:
            return None
        self.open(path, FA_READ)
        try:
            return self.read(self._read[0])
        finally:
            self.close()

    def list_dir(self, path: str, *, restore=None, sizes: bool = True) -> list[DirEntry]:
        """Every entry in path; a file's size costs reading the whole file, so sizes=False
        skips them (size None)."""
        entries = self._ls(path)
        if entries is None:
            raise DeviceError(f"list {path!r}", FR_NO_FILE)
        folder = path.strip("/")
        out = []
        for is_dir, name in entries:
            full = f"{folder}/{name}" if folder else name
            size = 0 if is_dir else self._size(full) if sizes else None
            out.append(DirEntry(name, size, is_dir, None))
        return out

    def open(self, path: str, mode: int, size: int | None = None) -> None:
        if mode & FA_WRITE:
            if mode & FA_CREATE_NEW and self._find(path) is not None:
                raise DeviceError(f"open {path!r}", FR_EXIST)
            self._write = {"path": path, "size": size, "buf": bytearray(), "sent": 0, "started": False}
            if size is not None:
                self._start_put()
            return
        if not mode & FA_READ:
            raise ValueError(f"unsupported open mode 0x{mode:02X}")
        found = self._find(path)
        if found is None or found[0]:
            raise DeviceError(f"open {path!r}", FR_NO_FILE)
        err, total, _r = self._call(request(GET, path=path))
        if err:
            raise DeviceError(f"open {path!r}", err)
        if not total:
            self._end_empty_get()
        self._read = (total, bytearray(), -(-total // BLOCK))  # file bytes left, buffer, blocks owed

    @property
    def open_size(self) -> int:
        """The size of the file open for reading (from its GET)."""
        return self._read[0] + len(self._read[1])  # bytes left to hand out, before any read

    def read(self, n: int) -> bytes:
        left, buf, owed = self._read
        while len(buf) < min(n, left):
            buf += self._recv()  # whole blocks; the last is padded past the file's end
            owed -= 1
        take = bytes(buf[: min(n, left)])
        del buf[: len(take)]
        self._read = (left - len(take), buf, owed)
        return take

    def _start_put(self) -> None:
        w = self._write
        self._write = None  # _call refuses while a transfer is open
        err, _size, _r = self._call(request(PUT, path=w["path"], size=w["size"]))
        self._write = w
        if err:
            self._write = None
            raise DeviceError(f"put {w['path']!r}", err)
        w["started"] = True

    def _send(self, final: bool = False) -> None:
        w = self._write
        buf = w["buf"]
        while len(buf) >= BLOCK or (final and buf):
            block = bytes(buf[:BLOCK]).ljust(BLOCK, b"\0")
            n = min(len(buf), BLOCK)
            try:
                self.port.write(block)
                if self.block_gap:
                    self.port.flush()  # pyserial: until the OS has sent it
                    time.sleep(self.block_gap)
            except OSError as e:  # pyserial's write timeout is one: the upload stall (docs/snes-plan.md)
                self._write = None  # nothing more can go to this transfer
                raise WedgedError(f"FXPAK: the cart stopped taking upload data ({e}); power cycle the console") from e
            del buf[:n]
            w["sent"] += n

    def write(self, data: bytes) -> None:
        w = self._write
        if w["size"] is not None and w["sent"] + len(w["buf"]) + len(data) > w["size"]:
            raise ValueError(f"write past the declared size of {w['path']!r}")
        w["buf"] += data
        if w["started"]:
            self._send()

    def close(self) -> None:
        if self._read is not None:
            _left, _buf, owed = self._read
            self._read = None
            self._drain(owed * BLOCK)  # the rest, read and dropped: never cut short
            return
        w = self._write
        if w is None:
            return
        if not w["started"]:  # no size given: everything was buffered
            w["size"] = len(w["buf"])
            self._start_put()
        short = w["size"] - w["sent"] - len(w["buf"])
        if short > 0:  # finish the declared length, or the cart reads our next request as data
            w["buf"] += bytes(short)
        if w["size"] == 0:  # the cart still waits for one data block before it closes the file
            self.port.write(bytes(BLOCK))
        self._send(final=True)
        self._write = None
        if short > 0:
            raise LinkError(f"{w['path']!r}: {short} bytes short of the declared size (padded with zeros)")

    # -- the console (owner's go-ahead 2026-11-03: the SNES app's exit and restart) --------

    def menu_reset(self) -> None:
        """Reset the console into the menu (answered at once; the menu runs within 1 s)."""
        err, _size, _r = self._call(request(MENU_RESET))
        if err:
            raise DeviceError("menu reset", err)

    def boot(self, path: str) -> None:
        """Load and start the ROM at path (answered once it's loaded: ~1 s for 32 KiB). The
        menu also lists it as last played. Only for paths LS has shown."""
        if self._find(path) is None:
            raise DeviceError(f"boot {path!r}", FR_NO_FILE)
        err, _size, _r = self._call(request(BOOT, path=path))
        if err:
            raise DeviceError(f"boot {path!r}", err)

    # -- cart memory (SNES space): the running ROM's own ROM and save RAM ------------------

    def read_mem(self, addr: int, size: int) -> bytes:
        err, _total, _r = self._call(mem_request(GET, addr, size))
        if err:
            raise DeviceError(f"read 0x{addr:06X}", err)
        data = b"".join(self._recv() for _ in range(-(-size // BLOCK)))  # never cut short
        return data[:size]

    def write_mem(self, addr: int, data: bytes) -> None:
        err, _total, _r = self._call(mem_request(PUT, addr, len(data)))
        if err:
            raise DeviceError(f"write 0x{addr:06X}", err)
        padded = data + bytes(-len(data) % BLOCK)
        for off in range(0, len(padded), BLOCK):
            self.port.write(padded[off : off + BLOCK])
            if self.block_gap:
                self.port.flush()
                time.sleep(self.block_gap)

    def mkdir(self, path: str) -> bool:
        if self._find(path) is not None:
            return False
        err, _size, _r = self._call(request(MKDIR, path=path))
        if err:
            raise DeviceError(f"mkdir {path!r}", err)
        return True

    def move(self, src: str, dst: str) -> None:
        """Rename a file within its folder (MV: the new name at byte 8, usbinterface.c). Not yet
        tried on hardware: the box uses it only where the profile says `moves`."""
        (folder, _), (dst_folder, name) = _split(src), _split(dst)
        if fat_key(folder) != fat_key(dst_folder):
            raise ValueError(f"the FXPAK renames within a folder only: {src!r} -> {dst!r}")
        if self._find(src) is None:
            raise DeviceError(f"move {src!r}", FR_NO_FILE)
        if self._find(dst) is not None:
            raise DeviceError(f"move to {dst!r}", FR_EXIST)
        req = bytearray(request(MV, path=src))
        raw = name_bytes(name)
        if len(raw) > 240:
            raise ValueError(f"name too long for MV: {name!r}")
        req[8 : 8 + len(raw) + 1] = raw + b"\0"
        err, _size, _r = self._call(bytes(req))
        if err:
            raise DeviceError(f"move {src!r} to {dst!r}", err)

    def delete(self, path: str) -> bool:
        """A file or an empty folder."""
        if self._find(path) is None:
            return False
        err, _size, _r = self._call(request(RM, path=path))
        if err:
            raise DeviceError(f"delete {path!r}", err)
        return True
