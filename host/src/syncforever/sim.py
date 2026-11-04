"""A fake EverDrive PRO (N8 by default; profile=MEGA_PRO for the big-endian Mega) that speaks
the USB byte protocol over a local folder standing in for the SD card.

It is a Transport, so N8Pro drives it exactly as it drives the real port. Behavior
the references do not pin down is marked SIM ASSUMPTION; the bench test in
docs/bench-test.md checks each one on the real cart.
"""

import os
import struct
import zlib
from datetime import datetime
from pathlib import Path, PurePosixPath

from .carts import N8_PRO
from .protocol import (
    ACK_BLOCK,
    ADDR_FIFO,
    AT_DIR,
    DIR_GET_MAX,
    DIR_OPT_HIDESYS,
    ERR_OUT_OF_DIR,
    ERR_OUT_OF_PAGE,
    FA_CREATE_ALWAYS,
    FA_CREATE_NEW,
    FA_READ,
    FA_WRITE,
    FR_EXIST,
    FR_NO_FILE,
    FR_NO_PATH,
    MENU_DIR,
    MENU_DIR_OPTS,
    MENU_PING,
    MENU_PONG,
    MENU_START,
    READ_BLOCK,
    STATUS2_KEY,
    SYS_INF_SIZE,
    Cmd,
)

FR_INT_ERR = 0x02
FR_INVALID_NAME = 0x06
FR_DENIED = 0x07
FR_INVALID_OBJECT = 0x09

# What STATUS reads after the menu launched a game: the menu's last file lookup
# (0x05 FR_NO_PATH for a game with no gamedata folder; seen 2026-10-31).
STATUS_AFTER_LAUNCH = 0x05


def fat_encode(ts):
    d = datetime.fromtimestamp(ts)
    date = ((d.year - 1980) << 9) | (d.month << 5) | d.day
    time_ = (d.hour << 11) | (d.minute << 5) | (d.second // 2)
    return date, time_


class FakeN8Pro:
    def __init__(self, root, *, in_game=False, sw_ver=0x0103, protocol=None, profile=N8_PRO):
        self.root = Path(root)
        self.profile = profile
        self.protocol = profile.protocol if protocol is None else protocol
        self.in_game = in_game
        self.sw_ver = sw_ver
        self.serial_words = (0x12345678, 0x9ABCDEF0)  # SYS_INF serial: which cart this is
        self.commands = []  # every command id parsed, in order
        self.write_sizes = []  # size of every host write() call
        self.sent = bytearray()  # every byte the host wrote
        self.fail_writes_after = None  # raise OSError once this many more bytes arrive
        self._in = bytearray()
        self._out = bytearray()
        self._result = STATUS_AFTER_LAUNCH if in_game else 0  # sticky: what STATUS reports
        self.fifo = bytearray()  # host -> game FIFO bytes nobody read
        self._file = None
        self._file_mode = 0
        self._listing = []
        # The one loaded-directory slot, shared with the menu: (path, F_DIR_LD options).
        # The menu starts with its root loaded.
        self.loaded_dir = None
        self.installed = None  # ROM loaded by *n
        self.running = None  # ROM started by *s
        # Test hook: on_start(rel_path, rom_bytes) -> app or None, called when *s starts a ROM.
        # An app gets the FIFO bytes the host sends (host_send), its USB output is read back
        # on this transport (take_usb), and when it sets `exited` the menu comes back.
        self.on_start = None
        self.resetting = False  # held in host reset (Cmd.HOST_RST)
        self.app = None
        self.rtc = bytes([0x26, 0x03, 0x09, 0x15, 0x13, 0x00])  # BCD; wrong, like the real cart's was
        self.menu_open(MENU_DIR)
        self._gen = self._parser()
        next(self._gen)

    # -- Transport -----------------------------------------------------------

    def write(self, data):
        self.write_sizes.append(len(data))
        self.sent += data
        if self.fail_writes_after is not None:
            if len(data) > self.fail_writes_after:
                self._in += data[: self.fail_writes_after]
                self.fail_writes_after = 0
                next(self._gen)
                raise OSError("simulated cable unplug")
            self.fail_writes_after -= len(data)
        self._in += data
        next(self._gen)

    def read(self, n):
        if self.app:
            self._out += self.app.take_usb()
            if self.app.exited:
                self._menu_returns()
        data = bytes(self._out[:n])
        del self._out[:n]
        return data

    def reset_input(self):
        self._out.clear()

    def close(self):
        if self._file:
            self._file.close()
            self._file = None

    # -- parser --------------------------------------------------------------

    def _take(self, n):
        while len(self._in) < n:
            yield
        data = bytes(self._in[:n])
        del self._in[:n]
        return data

    def _take_u16(self):
        return self.profile.unpack("H", (yield from self._take(2)))[0]

    def _take_u32(self):
        return self.profile.unpack("I", (yield from self._take(4)))[0]

    def _take_string(self):
        n = yield from self._take_u16()
        return (yield from self._take(n)).decode("ascii")

    def _parser(self):
        yield
        handlers = {
            Cmd.STATUS: self._status,
            Cmd.STATUS2: self._status2,
            Cmd.MEM_WR: self._mem_wr,
            Cmd.HOST_RST: self._host_rst,
            Cmd.SYS_INF: self._sys_inf,
            Cmd.RTC_GET: self._rtc_get,
            Cmd.RTC_SET: self._rtc_set,
            Cmd.F_DIR_LD: self._dir_load,
            Cmd.F_DIR_SIZE: self._dir_size,
            Cmd.F_DIR_GET: self._dir_get,
            Cmd.F_FINFO: self._finfo,
            Cmd.F_FOPN: self._fopen,
            Cmd.F_FRD: self._fread,
            Cmd.F_FWR: self._fwrite,
            Cmd.F_FCLOSE: self._fclose,
            Cmd.F_DIR_MK: self._mkdir,
            Cmd.F_DEL: self._delete,
            Cmd.F_FCRC: self._fcrc,  # SIM ASSUMPTION: zlib's CRC32, base 0, in the cart's byte order
            Cmd.F_FMV: self._move,
            Cmd.F_FCP: self._copy,  # SIM ASSUMPTION (NESPC's notes): a file into an existing folder  # SIM ASSUMPTION: mode 0 = plain rename, dst must not exist
        }
        while True:
            # The MCU discards anything that is not a valid header.
            if (yield from self._take(1)) != b"+":
                continue
            if (yield from self._take(1))[0] != ord("+") ^ 0xFF:
                continue
            cmd, inv = yield from self._take(2)
            if cmd ^ 0xFF != inv:
                continue
            self.commands.append(cmd)
            handler = handlers.get(cmd)
            if handler:
                yield from handler()

    # -- helpers -------------------------------------------------------------

    def _resolve(self, path):
        parts = PurePosixPath(path).parts
        parts = [p for p in parts if p != "/"]
        if any(p in ("..", ".") for p in parts):
            return None
        return self.root.joinpath(*parts)

    def _info(self, p, name_max=255):
        st = p.stat()
        date, time_ = fat_encode(st.st_mtime)
        attr = AT_DIR if p.is_dir() else 0x20
        size = 0 if p.is_dir() else st.st_size
        name = p.name.encode("latin-1")[:name_max] if p != self.root else b""
        return self.profile.pack("IHHBH", size, date, time_, attr, len(name)) + name

    def _emit(self, *chunks):
        for c in chunks:
            self._out += c

    def _done(self, code):
        self._result = code

    # -- handlers ------------------------------------------------------------

    def _status(self):
        self._emit(self.profile.status_reply(self._result))
        yield from ()

    def _mem_wr(self):
        addr = yield from self._take_u32()
        n = yield from self._take_u32()
        yield from self._take(1)  # exec flag
        data = yield from self._take(n)
        if addr != ADDR_FIFO:
            return
        if self.in_game:
            if self.app:
                self.app.host_send(data)
            else:
                self.fifo += data  # nobody reads it
        elif data == MENU_PING:
            self._emit(MENU_PONG)
        elif data.startswith(self.profile.install_cmd):  # the Mega ignores `*n` (hardware)
            self._menu_install(data[len(self.profile.install_cmd) :])
        elif data == MENU_START and self.installed:
            self.in_game, self.running = True, self.installed
            if self.on_start:
                self.app = self.on_start(self.running, self._resolve(self.running).read_bytes())

    def game_reset(self):
        """Test hook: the player does the in-game reset. The menu comes back and the FIFO
        bytes the game never read are gone (hardware: no backlog of `k`s, 2026-11-01)."""
        self.fifo.clear()
        self._menu_returns()

    def _menu_returns(self):
        """The running app exited to the menu: the menu boots fresh and loads its root."""
        self.app = None
        self.in_game, self.running = False, None
        self.menu_open(MENU_DIR)

    def _menu_install(self, payload):
        (n,) = self.profile.unpack("H", payload)
        rel = payload[2 : 2 + n].decode("ascii").lstrip("/")
        path = self._resolve(rel)
        if path is None or not path.is_file():
            # SIM ASSUMPTION: the menu's error code for a missing ROM.
            self.installed = None
            return self._emit(bytes([FR_NO_FILE]))
        head = path.read_bytes()[:16]
        # SIM ASSUMPTION: the reported index is the iNES mapper (true for MMC3 = 4).
        mapper = (head[6] >> 4) | (head[7] & 0xF0) if head[:4] == b"NES\x1a" else 0
        self.installed = rel
        self._emit(b"\x00", self.profile.pack("H", mapper) if self.profile.install_mapper else b"")

    def _rtc_get(self):
        self._emit(self.rtc)
        yield from ()

    def _host_rst(self):
        """Host reset (Mega): releasing it boots the menu, which sends `r`. The config-reset
        MEM_WR in between lands at ADDR_CFG and is ignored like any non-FIFO write."""
        (mode,) = yield from self._take(1)
        if mode == 0 and self.resetting:
            self.resetting = False
            self.fifo.clear()
            self._menu_returns()
            self._emit(b"r")
        elif mode:
            self.resetting = True

    def _rtc_set(self):
        # SIM ASSUMPTION: no reply, and STATUS keeps its last result.
        self.rtc = yield from self._take(6)

    def _status2(self):
        self._emit(bytes([STATUS2_KEY, self.protocol, self.profile.device, 0]))
        yield from ()

    def _sys_inf(self):
        # edio SysInfoIO, laid out and byte-ordered per profile (carts.py).
        b = bytearray(SYS_INF_SIZE)
        p, at = self.profile, self.profile.sysinf
        b[at.serial : at.serial + 16] = p.pack("IIII", *self.serial_words, 42, 7)
        b[at.built : at.built + 4] = p.pack("HH", 0x5B3E, 0x8487)
        b[at.sw_ver : at.sw_ver + 2] = p.pack("H", self.sw_ver)
        b[at.hw_ver : at.hw_ver + 2] = p.pack("H", 0x0001)
        b[at.boot_ver : at.boot_ver + 2] = p.pack("H", 0x0102)
        b[at.device] = p.device
        b[at.pwr_sys] = b[at.pwr_usb] = 1
        self._emit(bytes(b))
        yield from ()

    def _dir_load(self):
        opts = (yield from self._take(1))[0]
        raw = yield from self._take_string()
        self._done(self._load(raw, opts))

    def menu_open(self, path):
        """Test hook: the menu enters a folder, loading it into the shared slot."""
        assert self._load(path, MENU_DIR_OPTS) == 0

    def _load(self, raw, opts):
        # Always sorted, as the menu and NESPC load. The listing is a snapshot: later
        # changes on the card don't show until the next load (verified 2026-10-31).
        path = self._resolve(raw)
        self.loaded_dir = ("/".join(PurePosixPath(raw).parts).lstrip("/"), opts)
        if path is None or not path.is_dir():
            self._listing = []
            return FR_NO_PATH
        listing = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
        if opts & DIR_OPT_HIDESYS and path == self.root:
            # SIM ASSUMPTION: hides by name (EDN8 has plain 0x10 attributes); only EDN8 seen.
            listing = [p for p in listing if p.name != "EDN8"]
        self._listing = listing
        return 0

    def _dir_size(self):
        self._emit(self.profile.pack("H", len(self._listing)))
        yield from ()

    def _dir_get(self):
        start = yield from self._take_u16()
        count = yield from self._take_u16()
        name_max = yield from self._take_u16()
        if count > DIR_GET_MAX:
            return self._emit(bytes([ERR_OUT_OF_PAGE]))
        for i in range(start, start + count):
            if i >= len(self._listing) or not self._listing[i].exists():
                # SIM ASSUMPTION: an entry deleted since the load fails like one past the end.
                return self._emit(bytes([ERR_OUT_OF_DIR]))
            self._emit(b"\x00", self._info(self._listing[i], name_max))

    def _finfo(self):
        path = self._resolve((yield from self._take_string()))
        # SIM ASSUMPTION: F_FINFO's inline code also becomes the sticky STATUS.
        if path is None or not path.parent.is_dir():
            self._done(FR_NO_PATH)
            return self._emit(bytes([FR_NO_PATH]))
        if not path.exists():
            self._done(FR_NO_FILE)
            return self._emit(bytes([FR_NO_FILE]))
        self._done(0)
        self._emit(b"\x00", self._info(path, name_max=0))  # the real cart sends no name

    def _fopen(self):
        mode = (yield from self._take(1))[0]
        path = self._resolve((yield from self._take_string()))
        if self._file:
            self._file.close()
            self._file = None
        if path is None:
            return self._done(FR_INVALID_NAME)
        if not path.parent.is_dir():
            return self._done(FR_NO_PATH)
        if path.is_dir():
            return self._done(FR_DENIED)
        if mode & FA_CREATE_NEW and path.exists():
            return self._done(FR_EXIST)
        if mode & (FA_CREATE_NEW | FA_CREATE_ALWAYS):
            self._file = open(path, "w+b")  # noqa: SIM115 - held open across commands, like FatFs
        elif not path.exists():
            return self._done(FR_NO_FILE)
        else:
            self._file = open(path, "r+b")  # noqa: SIM115
        self._file_mode = mode
        self._done(0)

    def _fread(self):
        n = yield from self._take_u32()
        sent = 0
        while sent < n:
            if not self._file or not self._file_mode & FA_READ:
                return self._emit(bytes([FR_INVALID_OBJECT]))
            block = self._file.read(min(READ_BLOCK, n - sent))
            if len(block) < min(READ_BLOCK, n - sent):
                # SIM ASSUMPTION: reading past EOF fails rather than coming up short.
                return self._emit(bytes([FR_INT_ERR]))
            self._emit(b"\x00", block)
            sent += len(block)

    def _fwrite(self):
        n = yield from self._take_u32()
        got = 0
        while got < n:
            if not self._file or not self._file_mode & FA_WRITE:
                return self._emit(bytes([FR_DENIED]))
            self._emit(b"\x00")
            block = yield from self._take(min(ACK_BLOCK, n - got))
            self._file.write(block)
            got += len(block)
        self._done(0)

    def _fclose(self):
        if not self._file:
            return self._done(FR_INVALID_OBJECT)
        self._file.close()
        self._file = None
        self._done(0)
        yield from ()

    def _mkdir(self):
        path = self._resolve((yield from self._take_string()))
        if path is None:
            return self._done(FR_INVALID_NAME)
        if path.exists():
            return self._done(FR_EXIST)
        if not path.parent.is_dir():
            return self._done(FR_NO_PATH)
        path.mkdir()
        self._done(0)

    def _fcrc(self):
        e = self.profile.endian
        size = struct.unpack(e + "I", (yield from self._take(4)))[0]
        base = struct.unpack(e + "I", (yield from self._take(4)))[0]
        if not self._file:
            return self._emit(bytes([FR_INVALID_OBJECT]))
        self._emit(b"\0" + struct.pack(e + "I", zlib.crc32(self._file.read(size), base)))

    def _copy(self):
        yield from self._take(1)  # mode
        src = self._resolve((yield from self._take_string()))
        dst = self._resolve((yield from self._take_string()))
        if src is None or dst is None:
            return self._done(FR_INVALID_NAME)
        if not src.is_file():
            return self._done(FR_NO_FILE)
        if not dst.parent.is_dir():
            return self._done(FR_NO_PATH)
        if dst.exists():
            return self._done(FR_EXIST)
        dst.write_bytes(src.read_bytes())
        self._done(0)

    def _move(self):
        yield from self._take(1)  # mode
        src = self._resolve((yield from self._take_string()))
        dst = self._resolve((yield from self._take_string()))
        if src is None or dst is None:
            return self._done(FR_INVALID_NAME)
        if not src.exists():
            return self._done(FR_NO_FILE)
        if not dst.parent.is_dir():
            return self._done(FR_NO_PATH)
        if dst.exists():
            return self._done(FR_EXIST)
        src.rename(dst)
        self._done(0)

    def _delete(self):
        path = self._resolve((yield from self._take_string()))
        if path is None:
            return self._done(FR_INVALID_NAME)
        if not path.parent.is_dir():
            return self._done(FR_NO_PATH)
        if not path.exists():
            return self._done(FR_NO_FILE)
        if path.is_dir():
            if any(path.iterdir()):
                return self._done(FR_DENIED)
            path.rmdir()
        else:
            os.remove(path)
        self._done(0)
