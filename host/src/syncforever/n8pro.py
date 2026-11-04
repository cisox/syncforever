"""EverDrive PRO client: one method per cart command, no policy.

The cart's profile (carts.py: byte order, STATUS order, SYS_INF layout) comes from the IDs it
reports at connect. Write gating, verification and hashing live in ops.py.
"""

import signal
import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from .carts import N8_PRO, PROFILES
from .protocol import (
    ACK_BLOCK,
    ADDR_CFG,
    ADDR_FIFO,
    AT_DIR,
    CFG_SIZE,
    DIR_GET_MAX,
    DIR_OPT_SORTED,
    FA_CREATE_ALWAYS,
    FA_CREATE_NEW,
    FA_WRITE,
    FR_EXIST,
    FR_NO_FILE,
    FR_NO_PATH,
    INSTALL_TIMEOUT,
    MENU_DIR,
    MENU_DIR_OPTS,
    MENU_PING,
    MENU_PONG,
    MENU_START,
    NAME_MAX,
    READ_BLOCK,
    STATUS2_KEY,
    SYS_INF_SIZE,
    WAKE_ZEROS,
    Cmd,
    DeviceError,
    EverDriveError,
    LinkError,
    UnsafeStateError,
    WedgedError,
    frame,
    u8,
)
from .transport import read_exact

MOVE_MODE = FA_WRITE | FA_CREATE_NEW  # F_FMV's mode byte (EverDrive.move)


@dataclass(frozen=True)
class DirEntry:
    name: str
    size: int
    is_dir: bool
    mtime: datetime | None


@dataclass(frozen=True)
class SysInfo:
    serial: str
    boot_ctr: int
    game_ctr: int
    built: datetime | None
    sw_ver: int
    hw_ver: int
    boot_ver: int
    device_id: int
    cart_form: str
    pwr_sys: bool
    pwr_usb: bool
    raw: bytes


def fat_datetime(date, time_):
    try:
        return datetime(
            1980 + (date >> 9),
            (date >> 5) & 0x0F,
            date & 0x1F,
            time_ >> 11,
            (time_ >> 5) & 0x3F,
            (time_ & 0x1F) * 2,
        )
    except ValueError:
        return None


STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)

# Payloads being sent, process-wide. Signals only reach the main thread, so a cart served by
# another thread can't defer them itself: whoever stops the process calls stop_writes() first.
_writes = threading.Condition()
_writes_in_flight = 0
_stopping = False


def stop_writes(timeout: float | None = None) -> bool:
    """Refuse new writes, then wait for those in flight. False if they didn't finish in time."""
    global _stopping
    with _writes:
        _stopping = True
        return _writes.wait_for(lambda: _writes_in_flight == 0, timeout)


def allow_writes() -> None:
    """Undo stop_writes() (a new serve in the same process, e.g. tests)."""
    global _stopping
    with _writes:
        _stopping = False


def writes_in_flight() -> int:
    return _writes_in_flight


@contextmanager
def _payload():
    """Count a declared payload as in flight; refuse to start one once the process is stopping."""
    global _writes_in_flight
    with _writes:
        if _stopping:
            raise KeyboardInterrupt
        _writes_in_flight += 1
    try:
        yield
    finally:
        with _writes:
            _writes_in_flight -= 1
            _writes.notify_all()


@contextmanager
def _defer_sigint():
    """Hold Ctrl-C and SIGTERM until a declared payload is fully sent: stopping short wedges the
    cart. Only the main thread can; other threads rely on stop_writes()."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending = []
    previous = {s: signal.signal(s, lambda *_: pending.append(True)) for s in STOP_SIGNALS}
    try:
        yield
    finally:
        for s, handler in previous.items():
            signal.signal(s, handler)
        if pending:
            raise KeyboardInterrupt


def _bcd(n):
    return ((n // 10) << 4) | (n % 10)


def _unbcd(b):
    return (b >> 4) * 10 + (b & 15)


def rtc_encode(when):
    return bytes(_bcd(v) for v in (when.year - 2000, when.month, when.day, when.hour, when.minute, when.second))


def rtc_decode(raw):
    y, mo, d, h, mi, s = (_unbcd(b) for b in raw)
    try:
        return datetime(2000 + y, mo, d, h, mi, s)
    except ValueError:
        return None


def _norm_dir(path):
    return "/".join(p for p in path.split("/") if p)


class EverDrive:
    def __init__(self, transport):
        self.t = transport
        self.protocol = None
        self.device = None
        self.profile = N8_PRO  # until connect() reads the cart's IDs
        self._wedged = False
        self._desynced = False
        self.menu_dir = MENU_DIR  # last folder the menu was found showing; tried first

    # -- link ----------------------------------------------------------------

    def connect(self, attempts=2):
        last = None
        for _ in range(attempts):
            self._wake()
            try:
                self._identify()
                return
            except LinkError as e:
                last = e
                time.sleep(0.1)
        raise last

    def _wake(self):
        self.t.write(bytes(WAKE_ZEROS))
        self.t.reset_input()
        self._desynced = False

    def _identify(self):
        # STATUS2 answers [0x5A, protocol, device, x]; then the STATUS sent behind it,
        # [status, 0xA5] on the N8, [0xA5, status] on the Mega (carts.py).
        self._tx(frame(Cmd.STATUS2) + frame(Cmd.STATUS))
        a = self._rx(4)
        if a[0] != STATUS2_KEY:
            raise LinkError(f"unexpected STATUS2 key 0x{a[0]:02X}")
        st = self._rx(2)
        self.protocol, self.device = a[1], a[2]
        profile = PROFILES.get((self.protocol, self.device))
        if profile is None:
            raise LinkError(
                f"unknown EverDrive (protocol 0x{self.protocol:02X}, device 0x{self.device:02X}, status reply {st.hex()})"
            )
        self.profile = profile
        self.status()

    # numbers and strings in the cart's byte order
    def _u16(self, v):
        return self.profile.pack("H", v)

    def _u32(self, v):
        return self.profile.pack("I", v)

    def _str(self, s):
        b = s.encode("ascii")
        return self._u16(len(b)) + b

    def _begin(self):
        if self._wedged:
            raise WedgedError(f"cart parser is wedged; power-cycle the {self.profile.console} before reconnecting")
        if self._desynced:
            self._wake()

    def _tx(self, data):
        self.t.write(data)

    def _tx_data(self, data):
        # edlink's Link.TxData sends at most 4096 bytes per write and never
        # exactly 512 (it sends 256 instead). Reason unknown; mirrored as-is.
        mv = memoryview(data)
        off = 0
        while off < len(mv):
            n = min(len(mv) - off, 4096)
            if n == 512:
                n = 256
            self.t.write(bytes(mv[off : off + n]))
            off += n

    def _rx(self, n):
        try:
            return read_exact(self.t, n)
        except LinkError:
            self._desynced = True
            raise

    def _rx8(self):
        return self._rx(1)[0]

    def _rx16(self):
        return self.profile.unpack("H", self._rx(2))[0]

    def _rx32(self):
        return self.profile.unpack("I", self._rx(4))[0]

    def _rx_string(self):
        return self._rx(self._rx16()).decode("latin-1")

    def _rx_file_info(self):
        size, date, time_, attr = self.profile.unpack("IHHB", self._rx(9))
        name = self._rx_string()
        return DirEntry(name, size, bool(attr & AT_DIR), fat_datetime(date, time_))

    def _check(self, op):
        code = self.status()
        if code:
            raise DeviceError(op, code)

    # -- commands ------------------------------------------------------------

    def status(self):
        self._begin()
        self._tx(frame(Cmd.STATUS))
        st = self._rx(2)
        code = self.profile.status_code(st)
        if code is None:
            self._desynced = True
            raise LinkError(f"unexpected STATUS reply {st.hex()}")
        return code

    def sys_info(self):
        self._begin()
        self._tx(frame(Cmd.SYS_INF))
        b = self._rx(SYS_INF_SIZE)
        p, at = self.profile, self.profile.sysinf
        serial_hi, serial_lo, boot_ctr, game_ctr = p.unpack("IIII", b, at.serial)
        date, time_ = p.unpack("HH", b, at.built)
        if at.cart_form is None:
            form = p.console.lower()
        else:
            form = "famicom" if b[at.cart_form] else "nes"
        return SysInfo(
            serial=f"{serial_hi:08X}.{serial_lo:08X}",
            boot_ctr=boot_ctr,
            game_ctr=game_ctr,
            built=fat_datetime(date, time_),
            sw_ver=p.unpack("H", b, at.sw_ver)[0],
            hw_ver=p.unpack("H", b, at.hw_ver)[0],
            boot_ver=p.unpack("H", b, at.boot_ver)[0],
            device_id=b[at.device],
            cart_form=form,
            pwr_sys=bool(b[at.pwr_sys]),
            pwr_usb=bool(b[at.pwr_usb]),
            raw=b,
        )

    def rtc_get(self):
        """The cart clock as a naive local datetime, or None if it holds an impossible date."""
        self._begin()
        self._tx(frame(Cmd.RTC_GET))
        return rtc_decode(self._rx(6))

    def rtc_set(self, when):
        """Set the cart clock (local time, whole seconds). Verified on the N8, 2026-11-01."""
        self._begin()
        self._tx(frame(Cmd.RTC_SET) + rtc_encode(when))

    def mem_rd(self, addr, length):
        """Read cart memory through the MCU; only the MapConfig area (ADDR_CFG) is allowed."""
        if not (ADDR_CFG <= addr and addr + length <= ADDR_CFG + CFG_SIZE and length > 0):
            raise ValueError(f"mem_rd outside the config area: 0x{addr:X}+{length}")
        self._begin()
        self._tx(frame(Cmd.MEM_RD) + self._u32(addr) + self._u32(length) + u8(0))
        return self._rx(length)

    def reset_to_menu(self, hard=False, timeout=None):
        """Reset the console into the cart's menu, as edlink's Mega `run` does: hold the
        console in reset (soft: its reset button; hard: a cold boot), clear the cart's config
        (256 zero bytes at ADDR_CFG), release; the menu sends `r` once it's up. True if it did.
        Nothing talks to the menu while it boots, so this is safe where pinging isn't."""
        self._require("menu_reset", "resetting to the menu")
        self._begin()
        self._tx(frame(Cmd.HOST_RST) + u8(2 if hard else 1))
        time.sleep(0.06)
        self._tx(frame(Cmd.MEM_WR) + self._u32(ADDR_CFG) + self._u32(256) + u8(0) + bytes(256))
        self._tx(frame(Cmd.HOST_RST) + u8(0))
        with self._timeout(timeout or (6.0 if hard else 2.0)):
            reply = self.t.read(1)
        if reply != b"r":
            self._desynced = True
            return False
        return True

    def _require(self, ability, what):
        if not getattr(self.profile, ability):
            raise UnsafeStateError(f"{what} isn't tested on the {self.profile.name} yet")

    def menu_ping(self, timeout=1.0):
        """True if the ROM-select menu is up. Unanswered pings sit harmlessly in the FIFO."""
        self._require("menu_ping", "the menu ping")
        self._fifo_wr(MENU_PING)
        with self._timeout(timeout):
            reply = self.t.read(1)
        if reply == MENU_PONG:
            return True
        # A reply we did not expect, or a late one, must not be read as the next answer.
        self._desynced = True
        if reply:
            raise LinkError(f"unexpected menu ping reply {reply.hex()}")
        return False

    def menu_install(self, path, timeout=INSTALL_TIMEOUT):
        """Load the ROM at path for `*s` (N8: `*n`, Mega: `*i`; carts.py). Returns the mapper
        index, or None on carts that don't report it. Only the ROM-select menu answers;
        check menu_ping first."""
        self._require("menu_launch", "launching through the menu")
        p = self.profile
        path = path.strip("/")
        if p.install_slash:
            path = "/" + path
        self._fifo_wr(p.install_cmd + self._str(path))
        with self._timeout(timeout):
            code = self._rx8()
        if code:
            raise DeviceError(f"install {path!r}", code)
        return self._rx16() if p.install_mapper else None

    def menu_start(self):
        """Menu `*s`: start the ROM loaded by menu_install. The menu stops answering."""
        self._require("menu_launch", "launching through the menu")
        self._fifo_wr(MENU_START)

    def _fifo_wr(self, data):
        self._begin()
        self._tx(frame(Cmd.MEM_WR) + self._u32(ADDR_FIFO) + self._u32(len(data)) + u8(0) + data)

    def fifo_write(self, data):
        """Put bytes in the cart FIFO for the running program to read (our app)."""
        self._fifo_wr(data)

    def app_read(self, timeout=0.05):
        """Bytes the running program sent with CMD_USB_WR; b"" if none arrived in time.
        Only meaningful while our own app runs: nothing else is on the wire then."""
        with self._timeout(timeout):
            first = self.t.read(1)
        if not first:
            return b""
        with self._timeout(0.02):
            return first + self.t.read(4096)

    @contextmanager
    def _timeout(self, seconds):
        previous = getattr(self.t, "timeout", None)
        if previous is not None:
            self.t.timeout = seconds
        try:
            yield
        finally:
            if previous is not None:
                self.t.timeout = previous

    def list_dir(self, path, *, restore=True):
        """Entries in path, as the cart sorts them (folders first), including the system folder.

        Loading a folder replaces the menu's (the MCU has one slot, shared), and the menu
        then shows Error:81 on its next scroll. So by default this first reads what the
        menu shows, then puts that folder back afterwards (see `_find_folder`).
        restore=<path> puts back that folder instead; restore=None leaves ours loaded.
        """
        seen = None
        if restore is True:
            try:
                seen = self.read_snapshot()
            except DeviceError:
                pass  # the menu's view is already stale (an entry was deleted); use the root
        try:
            return self._list_loaded(path)
        finally:
            if self._wedged or restore is None:
                pass
            elif restore is True:
                self.menu_dir = self._find_folder(seen, first=path)  # leaves it loaded
            else:
                self._load_dir(restore, MENU_DIR_OPTS)

    def read_snapshot(self):
        """What the menu shows: the entries in the cart's loaded-directory slot, read
        without loading anything. Only meaningful if every host listing restores the
        menu's folder afterwards, as list_dir does."""
        self._begin()
        return self._read_loaded("snapshot")

    def _find_folder(self, seen, *, first, max_loads=64):
        """The folder whose listing (with the menu's options) equals `seen`.

        Tries the folder just listed, the last one found, the root and the system folder, then the
        rest of the card breadth first. Falls back to the root (on a flat ROM layout the
        biggest folder, so the menu is least likely to page past the end). Leaves the
        returned folder loaded.
        """
        if seen is None:
            self._load_dir(MENU_DIR, MENU_DIR_OPTS)
            return MENU_DIR
        want = [(e.name, e.size, e.is_dir, e.mtime) for e in seen]
        queue = list(dict.fromkeys([_norm_dir(first), self.menu_dir, MENU_DIR, self.profile.system_dir]))
        tried = set()
        while queue and len(tried) < max_loads:
            folder = queue.pop(0)
            if folder in tried:
                continue
            tried.add(folder)
            try:
                self._load_dir(folder, MENU_DIR_OPTS)
            except DeviceError:
                continue
            got = self._read_loaded(f"list {folder!r}")
            if [(e.name, e.size, e.is_dir, e.mtime) for e in got] == want:
                return folder
            queue += [f"{folder}/{e.name}".lstrip("/") for e in got if e.is_dir]
        self._load_dir(MENU_DIR, MENU_DIR_OPTS)
        return MENU_DIR

    def _load_dir(self, path, opts):
        self._begin()
        self._tx(frame(Cmd.F_DIR_LD) + u8(opts) + self._str(path))
        self._check(f"list {path!r}")

    def _list_loaded(self, path):
        self._load_dir(path, DIR_OPT_SORTED)
        return self._read_loaded(f"list {path!r}")

    def _read_loaded(self, op):
        self._tx(frame(Cmd.F_DIR_SIZE))
        total = self._rx16()
        entries = []
        for first in range(0, total, DIR_GET_MAX):
            count = min(DIR_GET_MAX, total - first)
            self._tx(frame(Cmd.F_DIR_GET) + self._u16(first) + self._u16(count) + self._u16(NAME_MAX))
            for _ in range(count):
                code = self._rx8()
                if code:
                    # The cart stops answering this page after an error.
                    raise DeviceError(op, code)
                entries.append(self._rx_file_info())
        return entries

    def stat(self, path):
        """The entry at path, or None if it does not exist."""
        self._begin()
        self._tx(frame(Cmd.F_FINFO) + self._str(path))
        code = self._rx8()
        if code in (FR_NO_FILE, FR_NO_PATH):
            return None
        if code:
            raise DeviceError(f"stat {path!r}", code)
        entry = self._rx_file_info()
        if not entry.name:
            # The cart sends an empty name for F_FINFO (seen 2026-10-31).
            entry = DirEntry(path.rstrip("/").rsplit("/", 1)[-1], entry.size, entry.is_dir, entry.mtime)
        return entry

    def open(self, path, mode):
        if mode & (FA_WRITE | FA_CREATE_NEW | FA_CREATE_ALWAYS):
            self._require("writes", "writing files")
        self._begin()
        self._tx(frame(Cmd.F_FOPN) + u8(mode) + self._str(path))
        self._check(f"open {path!r}")

    def read(self, n):
        """n bytes from the open file. The cart prefixes every 4 KiB with a status byte."""
        self._begin()
        self._tx(frame(Cmd.F_FRD) + self._u32(n))
        out = bytearray()
        while len(out) < n:
            code = self._rx8()
            if code:
                raise DeviceError("read", code)
            out += self._rx(min(READ_BLOCK, n - len(out)))
        return bytes(out)

    def write(self, data):
        """Write data to the open file. Any failure after the length is sent wedges the cart."""
        if not data:
            return
        self._begin()
        mv = memoryview(data)
        try:
            with _payload(), _defer_sigint():
                self._tx(frame(Cmd.F_FWR) + self._u32(len(data)))
                for off in range(0, len(mv), ACK_BLOCK):
                    ack = self._rx8()
                    if ack:
                        # The cart abandoned the command and stopped reading: nothing is owed.
                        raise DeviceError("write", ack)
                    self._tx_data(mv[off : off + ACK_BLOCK])
        except DeviceError:
            raise
        except (EverDriveError, OSError) as e:
            self._wedged = True
            raise WedgedError(f"write stopped partway ({e}); power-cycle the {self.profile.console}") from e
        self._check("write")

    def close(self):
        self._begin()
        self._tx(frame(Cmd.F_FCLOSE))
        self._check("close")

    def mkdir(self, path):
        """Create one folder. False if it already existed."""
        self._require("writes", "writing files")
        self._begin()
        self._tx(frame(Cmd.F_DIR_MK) + self._str(path))
        code = self.status()
        if code == FR_EXIST:
            return False
        if code:
            raise DeviceError(f"mkdir {path!r}", code)
        return True

    def move(self, src, dst, mode=MOVE_MODE):
        """Rename src to dst (F_FMV, edio's ed_cmd_file_move). dst must not exist, its folder
        must. mode: the destination's open flags, as for F_FCP (NESPC, read for facts, sends
        FA_WRITE | FA_CREATE_NEW and records the cart refusing a copy without FA_WRITE). Mode 0
        answered success and moved nothing on the N8 (2026-11-04). The box uses moves only where
        the cart profile says `moves` (sfe mv tries it)."""
        self._require("writes", "writing files")
        self._begin()
        self._tx(frame(Cmd.F_FMV) + bytes([mode]) + self._str(src) + self._str(dst))
        code = self.status()
        if code:
            raise DeviceError(f"move {src!r} to {dst!r}", code)

    def _rx_wait(self, n, wait, what):
        """n bytes from a command that answers when it's done, which may be long after the
        transport's own timeout."""
        deadline = time.monotonic() + wait
        out = b""
        while len(out) < n:
            got = self.t.read(n - len(out))
            if got:
                out += got
            elif time.monotonic() > deadline:
                self._desynced = True
                raise LinkError(f"{what}: no answer in {wait:.0f} s")
        return out

    def file_crc(self, size, base=0, wait=60.0):
        """CRC-32 of the next `size` bytes of the open file, computed on the cart (F_FCRC, edio's
        ed_cmd_file_crc): no file data crosses USB. base: the CRC so far, to go on from.
        ops.cart_crc does a whole file."""
        self._begin()
        self._tx(frame(Cmd.F_FCRC) + self._u32(size) + self._u32(base))
        code = self._rx_wait(1, wait, "file crc")[0]
        if code:
            raise DeviceError("file crc", code)
        return struct.unpack(self.profile.endian + "I", self._rx_wait(4, 10.0, "file crc"))[0]

    def copy(self, src, dst, mode=MOVE_MODE, wait=60.0):
        """Copy src to dst on the cart itself (F_FCP, mode FA_WRITE | FA_CREATE_NEW as NESPC
        sends it; worked on the Mega 2026-11-04). The STATUS after it answers once the whole copy
        is done: wait up to `wait` seconds for it."""
        self._require("writes", "writing files")
        self._begin()
        self._tx(frame(Cmd.F_FCP) + bytes([mode]) + self._str(src) + self._str(dst))
        self._tx(frame(Cmd.STATUS))
        st = self._rx_wait(2, wait, f"copy {src!r}")
        code = self.profile.status_code(st)
        if code is None:
            self._desynced = True
            raise LinkError(f"unexpected STATUS reply {st.hex()}")
        if code:
            raise DeviceError(f"copy {src!r} to {dst!r}", code)

    def delete(self, path):
        """Delete a file or empty folder. False if it was not there."""
        self._require("writes", "writing files")
        self._begin()
        self._tx(frame(Cmd.F_DEL) + self._str(path))
        code = self.status()
        if code in (FR_NO_FILE, FR_NO_PATH):
            return False
        if code:
            raise DeviceError(f"delete {path!r}", code)
        return True


N8Pro = EverDrive  # the name most of the code and tests still use
