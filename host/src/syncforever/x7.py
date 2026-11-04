"""The EverDrive-64 X7: its menu boots our app over USB, and the app serves the SD card's files.

The X7's menu has no file commands (docs/n64-plan.md), so the box talks to our N64 app instead
(docs/x7-protocol.md). `X7App` offers the methods `ops`, `saves` and `romsync` call on an `N8Pro`
(stat, list_dir, open/read/write/close, delete, mkdir), so they work on the X7 unchanged.
"""

from __future__ import annotations

import dataclasses
import logging
import struct
import time
from dataclasses import dataclass

from .carts import X7
from .n8pro import DirEntry, fat_datetime
from .protocol import AT_DIR, FR_EXIST, FR_NO_FILE, FR_NO_PATH, DeviceError, EverDriveError, LinkError

log = logging.getLogger(__name__)

FTDI_VID, FT245R_PID = 0x0403, 0x6001
FT245R_PRODUCT = "FT245R USB FIFO"

PROTO = 1
TYPE_SFE = 0x53  # the UNFLoader frame type the file service travels in
TYPE_UI = 0x55  # ... and the app protocol's frames (docs/app-protocol.md), unchanged
MAX_DATA = 128 * 1024  # largest payload either way (the app's buffer)
CHUNK = 64 * 1024  # READ/WRITE size per request

OP_HELLO, OP_STAT, OP_LIST, OP_OPEN, OP_READ, OP_WRITE, OP_CLOSE, OP_DELETE, OP_MKDIR, OP_CPAK, OP_RENAME, OP_SEEK = (
    range(1, 13)
)
CPAK_SIZE = 0x8000  # an official Controller Pak (third-party ones may have more banks)
OP_REPLY = 0x80

# The menu (UNFLoader's EverDrive commands): "cmd" + letter, u32 address, u32 length/512, u32 arg.
ROM_BASE = 0x10000000
CRC_AREA = 0x100000 + 4096  # ROMs smaller than this get the rest filled (`c`)
BOOT_SETTLE = 0.5  # between the last data and `s` (UNFLoader: "the delay is needed")
HELLO_WAIT = 5.0
FRAME_IDLE = 5.0  # seconds of silence inside a frame before it's given up (the app waits 2 s a block)
READ_RETRIES = 2  # a READ whose reply was lost is asked again from the same place


def menu_cmd(letter: str, addr: int = 0, size: int = 0, arg: int = 0) -> bytes:
    return b"cmd" + letter.encode() + struct.pack(">III", addr, size // 512, arg)


def frame(kind: int, data: bytes) -> bytes:
    """A frame to the app: padded to 4 bytes (the app reads in 4-byte units)."""
    return b"DMA@" + struct.pack(">I", kind << 24 | len(data)) + data + bytes(-len(data) % 4) + b"CMPH"


def text(s: str) -> bytes:
    raw = s.encode()
    return struct.pack(">H", len(raw)) + raw


def is_x7(port) -> bool:
    return (port.vid, port.pid) == (FTDI_VID, FT245R_PID) and FT245R_PRODUCT in (
        f"{port.product or ''} {port.description or ''}"
    )


def find_ports():
    from serial.tools import list_ports

    return [p for p in list_ports.comports() if is_x7(p)]


class FrameReader:
    """UNFLoader frames from the cart. Anything between frames (the app's zero padding, noise
    after a reconnect) is skipped by looking for the next "DMA@"."""

    def __init__(self, transport):
        self.t = transport

    def _read(self, n: int) -> bytes:
        buf = bytearray()
        deadline = time.monotonic() + FRAME_IDLE
        while len(buf) < n:
            chunk = self.t.read(n - len(buf))
            if chunk:
                buf += chunk
                deadline = time.monotonic() + FRAME_IDLE
            elif time.monotonic() > deadline:
                raise LinkError(f"X7 frame cut short ({len(buf)}/{n} bytes)")
        return bytes(buf)

    def next(self, timeout: float) -> tuple[int, bytes] | None:
        deadline = time.monotonic() + timeout
        window = b""
        while window != b"DMA@":
            b = self.t.read(1)
            if not b:
                if time.monotonic() > deadline:
                    return None
                continue
            window = (window + b)[-4:]
        (header,) = struct.unpack(">I", self._read(4))
        size = header & 0xFFFFFF
        data = self._read(size)
        if self._read(4) != b"CMPH":
            raise LinkError("X7 frame without its CMPH end")
        return header >> 24, data


@dataclass(frozen=True)
class Hello:
    proto: int
    version: str


def parse_hello(body: bytes) -> Hello:
    (n,) = struct.unpack_from(">H", body, 1)
    return Hello(body[0], body[3 : 3 + n].decode(errors="replace"))


def _entry(body: bytes, off: int) -> tuple[DirEntry, int]:
    attr, size, fdate, ftime, n = struct.unpack_from(">BIHHH", body, off)
    name = body[off + 11 : off + 11 + n].decode(errors="replace")
    return DirEntry(name, size, bool(attr & AT_DIR), fat_datetime(fdate, ftime)), off + 11 + n


EMU_DIR = "ED64/emu"
PATCHES = (".ips", ".aps")  # the menu applies these to the ROM of the same name


def emulated(names) -> tuple[str, ...]:
    """The extensions the card's emulators run: ED64/emu/<ext>.v64 runs *.<ext> (the OS readme)."""
    out = []
    for name in names:
        stem, dot, ext = name.rpartition(".")
        if dot and ext.lower() == "v64" and stem and not stem.startswith("."):
            out.append("." + stem.lower())
    return tuple(sorted(set(out)))


class X7App:
    """Our app on the X7, answering file requests. Same file methods as N8Pro, plus the app
    protocol's link (app_read, fifo_write) for AppServer."""

    def __init__(self, transport, timeout: float = 10.0):
        self.t = transport
        self.frames = FrameReader(transport)
        self.timeout = timeout
        self._tag = 0
        self._position = 0  # of the open file: where the next READ starts
        self.hello: Hello | None = None
        self.profile = X7
        self.ui = bytearray()  # app protocol bytes not yet handed to app_read

    def scan_emulators(self) -> tuple[str, ...]:
        """Sync N64 ROMs plus whatever the card's emulators run, and patches."""
        try:
            names = [e.name for e in self.list_dir(EMU_DIR) if not e.is_dir]
        except EverDriveError:
            names = []
        found = emulated(names)
        self.profile = dataclasses.replace(X7, rom_suffixes=(*X7.rom_suffixes, *found, *PATCHES))
        return found

    # -- the app protocol (AppServer's link) -------------------------------------

    def app_read(self, timeout: float) -> bytes:
        if not self.ui:
            self._take(self.frames.next(timeout))
        out = bytes(self.ui)
        self.ui.clear()
        return out

    def fifo_write(self, data: bytes) -> None:
        self.t.write(frame(TYPE_UI, data))

    def _take(self, got) -> None:
        if got is not None and got[0] == TYPE_UI:
            self.ui += got[1]

    # -- the link --------------------------------------------------------------

    def call(self, op: int, payload: bytes = b"", timeout: float | None = None) -> tuple[int, bytes]:
        """Send one request; (status, body) of its reply. Other frames (an unprompted HELLO,
        libdragon's heartbeat, stale replies) are skipped."""
        self._tag = self._tag % 255 + 1
        self.t.write(frame(TYPE_SFE, bytes([op, self._tag]) + payload))
        deadline = time.monotonic() + (timeout or self.timeout)
        while True:
            got = self.frames.next(max(deadline - time.monotonic(), 0.05))
            if got is None:
                raise LinkError(f"the X7 app didn't answer (op {op})")
            kind, data = got
            if kind == TYPE_SFE and len(data) >= 3 and data[0] == op | OP_REPLY and data[1] == self._tag:
                return data[2], data[3:]
            self._take(got)  # an app protocol frame meanwhile: AppServer reads it next

    def say_hello(self, timeout: float | None = None) -> Hello:
        _status, body = self.call(OP_HELLO, timeout=timeout)
        self.hello = parse_hello(body)
        return self.hello

    def menu_ping(self) -> bool:
        """Writes are safe while our app answers: it owns the card (the menu isn't running)."""
        try:
            self.say_hello(timeout=3.0)
        except EverDriveError:
            return False
        return True

    def _path(self, path: str) -> bytes:
        return text(path.strip("/"))

    def _check(self, what: str, status: int) -> None:
        if status:
            raise DeviceError(what, status)

    # -- files (N8Pro's interface) ----------------------------------------------

    def stat(self, path: str) -> DirEntry | None:
        status, body = self.call(OP_STAT, self._path(path))
        if status in (FR_NO_FILE, FR_NO_PATH):
            return None
        self._check(f"stat {path!r}", status)
        entry, _ = _entry(body, 0)
        return entry

    def list_dir(self, path: str, *, restore=True) -> list[DirEntry]:
        """Every entry in path (restore is the PRO carts' concern: the X7's menu isn't running)."""
        entries: list[DirEntry] = []
        while True:
            status, body = self.call(OP_LIST, struct.pack(">H", len(entries)) + self._path(path))
            self._check(f"list {path!r}", status)
            count, more = struct.unpack_from(">HB", body)
            off = 3
            for _ in range(count):
                entry, off = _entry(body, off)
                entries.append(entry)
            if not more:
                return entries

    def open(self, path: str, mode: int) -> None:
        status, _ = self.call(OP_OPEN, bytes([mode]) + self._path(path))
        self._check(f"open {path!r}", status)
        self._position = 0

    def _read_chunk(self, n: int) -> tuple[int, bytes]:
        """One READ. Its reply can be lost on the way (2026-11-05: a pause on the USB side, 64,088
        of 65,539 bytes): the file is where that READ left it, so go back and ask again."""
        for left in range(READ_RETRIES, -1, -1):
            try:
                if left < READ_RETRIES:
                    status, _ = self.call(OP_SEEK, struct.pack(">I", self._position))
                    self._check("seek", status)
                got = self.call(OP_READ, struct.pack(">I", n))
                self._position += len(got[1])
                return got
            except LinkError as e:
                if not left:
                    raise
                log.warning("read at %d: %s; asking again", self._position, e)
        raise AssertionError("unreachable")

    def read(self, n: int) -> bytes:
        out = bytearray()
        while len(out) < n:
            status, body = self._read_chunk(min(CHUNK, n - len(out)))
            self._check("read", status)
            if not body:
                break  # end of file
            out += body
        return bytes(out)

    def write(self, data: bytes) -> None:
        for off in range(0, len(data), CHUNK):
            part = data[off : off + CHUNK]
            status, _written = self.call(OP_WRITE, part)
            self._check("write", status)

    def close(self) -> None:
        status, _ = self.call(OP_CLOSE)
        self._check("close", status)

    def mkdir(self, path: str) -> bool:
        status, _ = self.call(OP_MKDIR, self._path(path))
        if status == FR_EXIST:
            return False
        self._check(f"mkdir {path!r}", status)
        return True

    def delete(self, path: str) -> bool:
        status, _ = self.call(OP_DELETE, self._path(path))
        if status in (FR_NO_FILE, FR_NO_PATH):
            return False
        self._check(f"delete {path!r}", status)
        return True

    def move(self, src: str, dst: str) -> None:
        """Rename (f_rename in our app; apps before it answer FR_INVALID_PARAMETER)."""
        status, _ = self.call(OP_RENAME, self._path(src) + self._path(dst))
        self._check(f"move {src!r} to {dst!r}", status)

    def controller_pak(self, port: int) -> bytes | None:
        """The Controller Pak in controller port+1 (32 KB), or None: no pak there, or an app
        from before CPAK (it answers FR_INVALID_PARAMETER)."""
        status, body = self.call(OP_CPAK, bytes([port]))
        if status or len(body) != CPAK_SIZE:
            return None
        return body

    def sys_info(self):
        raise EverDriveError("the X7 has no SYS_INF; `sfe --x7` reports the app's HELLO instead")


def menu_answers(transport, timeout: float = 1.0) -> bool:
    """True if the X7's menu answers its test command (`t` -> "cmdr")."""
    transport.reset_input()
    transport.write(menu_cmd("t"))
    deadline = time.monotonic() + timeout
    reply = b""
    while len(reply) < 16 and time.monotonic() < deadline:
        reply += transport.read(16 - len(reply))
    return reply[:4] == b"cmdr"


def boot(transport, rom: bytes) -> None:
    """Load rom into the cart over USB and start it. The menu must be up (menu_answers)."""
    rom = rom + bytes(-len(rom) % 512)
    if len(rom) < CRC_AREA:
        transport.write(menu_cmd("c", ROM_BASE, CRC_AREA))
    transport.write(menu_cmd("W", ROM_BASE, len(rom)))
    for off in range(0, len(rom), 0x8000):
        transport.write(rom[off : off + 0x8000])
    time.sleep(BOOT_SETTLE)
    transport.write(menu_cmd("s"))


def connect(transport, rom: bytes | None = None, *, log=lambda msg: None) -> X7App:
    """Our app on this X7: already running, or booted from the menu with rom."""
    app = X7App(transport)
    try:
        app.say_hello(timeout=1.0)
        log(f"the app is running (v{app.hello.version})")
        return app
    except LinkError:
        pass
    if not menu_answers(transport):
        raise LinkError("neither our app nor the X7 menu answered (is a game running? power-cycle to the menu)")
    if rom is None:
        raise LinkError("the X7 is at its menu, and there's no app ROM to boot")
    log("booting the app from the menu")
    boot(transport, rom)
    got = app.frames.next(HELLO_WAIT)
    while got is not None and not (got[0] == TYPE_SFE and got[1][:1] == bytes([OP_HELLO | OP_REPLY])):
        got = app.frames.next(HELLO_WAIT)
    if got is None:
        raise LinkError("the app didn't start after booting")
    app.hello = parse_hello(got[1][3:])
    if app.hello.proto != PROTO:
        raise LinkError(f"the app speaks protocol {app.hello.proto}, the box {PROTO}")
    log(f"the app started (v{app.hello.version})")
    return app
