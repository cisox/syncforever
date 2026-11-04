"""A fake EverDrive-64 X7 for tests: the menu (boots a ROM over USB), then our app's file service
over a folder standing in for the SD card. Encodes our *beliefs* about the real thing (n64/src)."""

from __future__ import annotations

import errno
import struct
from pathlib import Path

from . import appserver, x7
from .protocol import FA_CREATE_ALWAYS, FA_CREATE_NEW, FA_READ, FA_WRITE

FR_OK, FR_DISK_ERR, FR_NO_FILE, FR_NO_PATH, FR_INVALID_NAME, FR_DENIED, FR_EXIST = 0, 1, 4, 5, 6, 7, 8
FR_INVALID_OBJECT, FR_INVALID_PARAMETER = 9, 19
AM_DIR = 0x10


def _fr(e: OSError) -> int:
    if isinstance(e, FileNotFoundError):
        return FR_NO_FILE
    if isinstance(e, FileExistsError):
        return FR_EXIST
    if isinstance(e, (IsADirectoryError, NotADirectoryError, PermissionError)) or e.errno == errno.ENOTEMPTY:
        return FR_DENIED
    return FR_DISK_ERR


class FakeX7:
    """A Transport: what the box writes goes to the menu or the app, replies wait in `out`."""

    def __init__(self, root, *, state="menu", version="0.1"):
        self.root = Path(root)
        self.state = state  # "menu", "app", "game" (answers nothing)
        self.version = version
        self.inbox = bytearray()
        self.out = bytearray()
        self.booted: bytes | None = None
        self._loading = 0  # ROM bytes still expected after `W`
        self._rom = bytearray()
        self.file = None
        self.requests: list[int] = []
        self.paks: dict[int, bytes] = {}  # port -> Controller Pak image
        self.ui_rx = appserver.Deframer()
        self.ui_got: list[tuple[int, bytes]] = []  # app protocol frames from the box
        if state == "app":
            self._start_app()

    # -- Transport ---------------------------------------------------------------

    def write(self, data: bytes) -> None:
        self.inbox += data
        while self._step():
            pass

    def read(self, n: int) -> bytes:
        got = bytes(self.out[:n])
        del self.out[:n]
        return got

    def reset_input(self) -> None:
        self.out.clear()

    def close(self) -> None:
        pass

    # -- the menu ----------------------------------------------------------------

    def _step(self) -> bool:
        if self.state == "game":
            self.inbox.clear()
            return False
        if self.state == "menu":
            return self._menu_step()
        return self._app_step()

    def _menu_step(self) -> bool:
        if self._loading:
            take = min(self._loading, len(self.inbox))
            if not take:
                return False
            self._rom += self.inbox[:take]
            del self.inbox[:take]
            self._loading -= take
            return True
        if len(self.inbox) < 16:
            return False
        cmd = bytes(self.inbox[:16])
        del self.inbox[:16]
        if cmd[:3] != b"cmd":
            return True
        letter, (_addr, blocks, _arg) = chr(cmd[3]), struct.unpack(">III", cmd[4:])
        if letter == "t":
            self.out += b"cmdr" + bytes(4) + b"\xff" * 4 + bytes.fromhex("80090000")
        elif letter == "W":
            self._rom = bytearray()
            self._loading = blocks * 512
        elif letter == "s":
            self.booted = bytes(self._rom)
            self._start_app()
        return True

    # -- the app -----------------------------------------------------------------

    def _start_app(self) -> None:
        self.state = "app"
        self._send(bytes([x7.OP_HELLO | x7.OP_REPLY, 0, FR_OK, x7.PROTO]) + x7.text(self.version))

    def _send(self, body: bytes, kind: int = x7.TYPE_SFE) -> None:
        raw = b"DMA@" + struct.pack(">I", kind << 24 | len(body)) + body + b"CMPH"
        self.out += raw + bytes(-len(raw) % 512)  # the app pads to whole blocks

    def app_says(self, kind: int, payload: bytes = b"") -> None:
        """The app's screen sends an app protocol message (docs/app-protocol.md)."""
        self._send(appserver.frame(kind, payload), x7.TYPE_UI)

    def _app_step(self) -> bool:
        start = self.inbox.find(b"DMA@")
        if start < 0:
            return False
        del self.inbox[:start]
        if len(self.inbox) < 8:
            return False
        (header,) = struct.unpack(">I", self.inbox[4:8])
        size = header & 0xFFFFFF
        end = 8 + size + (-size % 4)
        if len(self.inbox) < end + 4:
            return False
        data = bytes(self.inbox[8 : 8 + size])
        ok = self.inbox[end : end + 4] == b"CMPH"
        del self.inbox[: end + 4]
        if ok and header >> 24 == x7.TYPE_SFE and len(data) >= 2:
            self.requests.append(data[0])
            self._send(self._handle(data[0], data[1], data[2:]))
        elif ok and header >> 24 == x7.TYPE_UI:
            self.ui_got += self.ui_rx.feed(data)
        return True

    def _path(self, raw: bytes) -> Path | None:
        (n,) = struct.unpack_from(">H", raw)
        rel = raw[2 : 2 + n].decode()
        parts = [p for p in rel.split("/") if p]
        if any(p in (".", "..") for p in parts):
            return None
        return self.root.joinpath(*parts)

    @staticmethod
    def _entry(path: Path) -> bytes:
        st = path.stat()
        name = path.name.encode()
        attr = AM_DIR if path.is_dir() else 0
        size = 0 if path.is_dir() else st.st_size
        return struct.pack(">BIHHH", attr, size, 0x5B42, 0x6000, len(name)) + name  # 2025-10-02 12:00

    def _handle(self, op: int, tag: int, a: bytes) -> bytes:
        head = bytes([op | x7.OP_REPLY, tag])
        try:
            status, body = self._op(op, a)
        except OSError as e:
            status, body = _fr(e), b""
        return head + bytes([status]) + body

    def _op(self, op: int, a: bytes) -> tuple[int, bytes]:
        if op == x7.OP_HELLO:
            return FR_OK, bytes([x7.PROTO]) + x7.text(self.version)
        if op == x7.OP_STAT:
            path = self._path(a)
            if path is None or path == self.root:
                return FR_INVALID_NAME, b""  # FatFs can't stat the root
            if not path.parent.exists():
                return FR_NO_PATH, b""
            return FR_OK, self._entry(path)
        if op == x7.OP_LIST:
            (start,) = struct.unpack_from(">H", a)
            path = self._path(a[2:])
            if path is None or not path.is_dir():
                return FR_NO_PATH, b""
            items = sorted(path.iterdir(), key=lambda p: p.name)[start:]
            entries, more, size = [], 0, 3
            for item in items:
                e = self._entry(item)
                if size + len(e) > x7.MAX_DATA:
                    more = 1
                    break
                entries.append(e)
                size += len(e)
            return FR_OK, struct.pack(">HB", len(entries), more) + b"".join(entries)
        if op == x7.OP_OPEN:
            mode, path = a[0], self._path(a[1:])
            if path is None:
                return FR_INVALID_NAME, b""
            if self.file:
                self.file.close()
                self.file = None
            if not path.parent.is_dir():
                return FR_NO_PATH, b""
            if mode & FA_CREATE_NEW:
                how = "xb"
            elif mode & FA_CREATE_ALWAYS:
                how = "wb"
            elif mode & FA_WRITE:
                how = "r+b"
            elif mode & FA_READ:
                how = "rb"
            else:
                return FR_INVALID_PARAMETER, b""
            self.file = open(path, how)  # noqa: SIM115 - stays open across requests, like the app's FIL
            return FR_OK, b""
        if op == x7.OP_READ:
            if not self.file:
                return FR_INVALID_OBJECT, b""
            (n,) = struct.unpack_from(">I", a)
            return FR_OK, self.file.read(min(n, x7.MAX_DATA))
        if op == x7.OP_SEEK:
            if not self.file:
                return FR_INVALID_OBJECT, b""
            self.file.seek(struct.unpack_from(">I", a)[0])
            return FR_OK, b""
        if op == x7.OP_WRITE:
            if not self.file:
                return FR_INVALID_OBJECT, b""
            self.file.write(a)
            return FR_OK, struct.pack(">I", len(a))
        if op == x7.OP_CLOSE:
            if self.file:
                self.file.close()
                self.file = None
            return FR_OK, b""
        if op == x7.OP_DELETE:
            path = self._path(a)
            if path is None:
                return FR_INVALID_NAME, b""
            if not path.exists():
                return (FR_NO_FILE if path.parent.is_dir() else FR_NO_PATH), b""
            path.rmdir() if path.is_dir() else path.unlink()
            return FR_OK, b""
        if op == x7.OP_RENAME:
            n = struct.unpack(">H", a[:2])[0]
            src, dst = self._path(a[: 2 + n]), self._path(a[2 + n :])
            if src is None or dst is None:
                return FR_INVALID_NAME, b""
            if not src.exists():
                return FR_NO_FILE, b""
            if not dst.parent.is_dir():
                return FR_NO_PATH, b""
            if dst.exists():
                return FR_EXIST, b""
            src.rename(dst)
            return FR_OK, b""
        if op == x7.OP_MKDIR:
            path = self._path(a)
            if path is None:
                return FR_INVALID_NAME, b""
            if not path.parent.is_dir():
                return FR_NO_PATH, b""
            path.mkdir()
            return FR_OK, b""
        if op == x7.OP_CPAK:
            pak = self.paks.get(a[0])
            return (FR_OK, pak) if pak else (FR_NO_FILE, b"")
        if op == x7.OP_CPAK:
            pak = self.paks.get(a[0])
            return (FR_OK, pak) if pak else (FR_NO_FILE, b"")
        return FR_INVALID_PARAMETER, b""
