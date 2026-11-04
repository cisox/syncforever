"""A fake FXPAK PRO on a folder (the SD card), for tests and `sfe --fxpak --sim DIR`: the port
side of fxpak.py, as measured on firmware 1.11.2 and read in its source (docs/snes-plan.md).

GET of a missing path raises CartHung: on the real cart it hangs the MCU until a power cycle,
so a test that gets there has found a bug. So does cutting a GET short (dropping DTR before its
blocks are read): the firmware keeps the read position, the next GET is shifted by it
(measured: 512 bytes), and one that starts past its file's end hangs USB.
"""

from __future__ import annotations

import os
import shutil
import struct
from pathlib import Path

from .fxpak import (
    BLOCK,
    BOOT,
    GET,
    INFO,
    LS,
    LS_DIR,
    LS_END,
    LS_FILE,
    LS_MORE,
    MAGIC,
    MENU_RESET,
    MKDIR,
    MV,
    PUT,
    RESPONSE,
    RM,
    SNES,
    SRAM_BASE,
    name_bytes,
    name_str,
)
from .ops import fat_name


class CartHung(AssertionError):
    """The request would hang a real FXPAK (power cycle only)."""


class FakeFxPak:
    def __init__(self, root, rom: str = "/sd2snes/m3nu.bin", version: str = "1.11.2"):
        self.root = Path(root)
        self.rom = rom
        self.version = version
        self.inbox = bytearray()
        self.out = bytearray()
        self.put: dict | None = None  # an open PUT: path, size, bytes taken
        self._dtr = True
        self.requests: list[tuple[int, str]] = []
        self.aborts = 0
        self.stale = 0  # the firmware's read position left by a GET cut short
        self.busy = False  # after an empty file's GET: takes no more requests until DTR drops
        # SNES space (the running ROM's memory): its ROM image from 0 and its SRAM. A write
        # lands at once and the running ROM sees it (SIM ASSUMPTION: fine on hardware so far).
        self.rom_mem = bytearray(0x8000)
        self.sram = bytearray(0x800)

    # -- pyserial's surface --------------------------------------------------------------

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        if self._dtr and not value:  # the host "disconnected": the MCU goes idle
            self.busy = False
            if self.out:  # a GET's blocks still unread: its position stays (one block sent)
                self.stale = BLOCK
            self.out.clear()
            self.put = None
            self.aborts += 1
        self._dtr = value

    def reset_input_buffer(self):
        self.out.clear()

    def read(self, n):
        got = bytes(self.out[:n])
        del self.out[:n]
        return got

    def write(self, data):
        self.inbox += data
        while len(self.inbox) >= BLOCK:
            block = bytes(self.inbox[:BLOCK])
            del self.inbox[:BLOCK]
            self._block(block)

    def flush(self):
        pass

    def close(self):
        pass

    # -- the MCU ---------------------------------------------------------------------------

    def _local(self, path: str) -> Path:
        # FatFs drops trailing dots and spaces from each name, on create and on lookup.
        path = "/".join(fat_name(c) for c in path.split("/"))
        p = (self.root / path.lstrip("/")).resolve()
        if p != self.root.resolve() and self.root.resolve() not in p.parents:
            raise CartHung(f"path outside the card: {path!r}")
        return p

    def _respond(self, error: int, size: int, extra: dict[int, bytes] | None = None):
        r = bytearray(BLOCK)
        r[0:4] = MAGIC
        r[4] = RESPONSE
        r[5] = error
        struct.pack_into(">I", r, 252, size)
        for off, data in (extra or {}).items():
            r[off : off + len(data)] = data
        self.out += r

    def _mem(self, addr: int, size: int) -> tuple[bytearray, int]:
        """The memory at a SNES-space address, and the offset in it."""
        for base, mem in ((0, self.rom_mem), (SRAM_BASE, self.sram)):
            if base <= addr and addr + size <= base + len(mem):
                return mem, addr - base
        raise AssertionError(f"SNES space 0x{addr:06X}+{size} isn't modelled")

    def _block(self, b: bytes):
        if self.put is not None and "mem" in self.put:  # SNES space
            take = min(BLOCK, self.put["size"] - self.put["done"])
            mem, off = self._mem(self.put["addr"] + self.put["done"], take)
            mem[off : off + take] = b[:take]
            self.put["done"] += take
            if self.put["done"] >= self.put["size"]:
                self.put = None
            return
        if self.put is not None:  # a PUT's data phase: every block is data
            take = min(BLOCK, self.put["size"] - self.put["done"])
            with open(self.put["file"], "ab") as f:
                f.write(b[:take])
            self.put["done"] += take
            if self.put["done"] >= self.put["size"]:
                self.put = None
            return
        if self.busy:
            return  # stuck after an empty file's GET: nothing is read, nothing answered
        if b[:4] != MAGIC:
            return  # not a request: ignored, like the firmware
        op = b[4]
        size = struct.unpack_from(">I", b, 252)[0]
        if b[5] == SNES and op in (GET, PUT):
            addr = struct.unpack_from(">I", b, 256)[0]
            self.requests.append((op, f"snes:{addr:06X}"))
            mem, off = self._mem(addr, size)
            self._respond(0, size)
            if op == GET:
                data = bytes(mem[off : off + size])
                for o in range(0, len(data), BLOCK):
                    self.out += data[o : o + BLOCK].ljust(BLOCK, b"\0")
            else:
                self.put = {"mem": True, "addr": addr, "size": size, "done": 0}
            return
        path = name_str(b[256:].split(b"\0", 1)[0])
        self.requests.append((op, path))
        if op == MENU_RESET:
            self.rom = "/sd2snes/m3nu.bin"
            self._respond(0, 0)
            return
        if op == BOOT:
            p = self._local(path)
            if not p.is_file():
                self._respond(1, 0)
                return
            self.rom = "/" + path.strip("/")
            data = p.read_bytes()[: len(self.rom_mem)]
            self.rom_mem[:] = data.ljust(len(self.rom_mem), b"\0")
            self._respond(0, 0)
            return
        if op == INFO:
            self._respond(0, 0, {16: name_bytes(self.rom), 260: self.version.encode(), 324: b"FXPAK PRO STM32"})
        elif op == LS:
            self._ls(self._local(path))
        elif op == GET:
            p = self._local(path)
            if not p.is_file():
                raise CartHung(f"GET of a missing file hangs the cart: {path!r}")
            data = p.read_bytes()
            self._respond(0, len(data))
            self.busy = not data  # an empty file: no data block, the MCU stays busy until DTR drops
            if self.stale:
                if self.stale >= len(data):
                    raise CartHung(f"GET after a GET cut short, past {path!r}'s end: USB stops")
                data, self.stale = data[self.stale :], 0  # shifted, and that much shorter
            for off in range(0, len(data), BLOCK):
                self.out += data[off : off + BLOCK].ljust(BLOCK, b"\0")
        elif op == PUT:
            p = self._local(path)
            ok = p.parent.is_dir() and not p.is_dir()
            if ok:
                p.write_bytes(b"")  # FA_CREATE_ALWAYS
            self._respond(0 if ok else 5, size)
            # The cart takes the data blocks even after a failed open (SIM ASSUMPTION), and
            # waits for one block even for an empty file.
            self.put = {"file": p if ok else os.devnull, "size": size, "done": 0}
        elif op == MKDIR:
            p = self._local(path)
            ok = p.parent.is_dir() and not p.exists()
            if ok:
                p.mkdir()
            self._respond(0 if ok else 1, 0)
        elif op == MV:  # SIM ASSUMPTION (firmware source): the new name at byte 8, same folder
            p = self._local(path)
            new = p.parent / fat_name(name_str(b[8:].split(b"\0", 1)[0]))
            ok = p.is_file() and not new.exists()
            if ok:
                p.rename(new)
            self._respond(0 if ok else 1, 0)
        elif op == RM:
            p = self._local(path)
            if p.is_file():
                p.unlink()
                self._respond(0, 0)
            elif p.is_dir() and not any(p.iterdir()):
                shutil.rmtree(p)
                self._respond(0, 0)
            else:
                self._respond(1, 0)
        else:
            self._respond(1, 0)

    def _ls(self, folder: Path):
        if not folder.is_dir():
            self._respond(1, 1)
            self.out += bytes([LS_END]).ljust(BLOCK, b"\0")
            return
        self._respond(0, 1)
        block = bytearray()
        for p in sorted(folder.iterdir()):
            entry = bytes([LS_DIR if p.is_dir() else LS_FILE]) + name_bytes(p.name) + b"\0"
            if len(block) + len(entry) + 1 > BLOCK:  # SIM ASSUMPTION: where blocks break
                self.out += (block + bytes([LS_MORE])).ljust(BLOCK, b"\0")
                block = bytearray()
            block += entry
        self.out += (block + bytes([LS_END])).ljust(BLOCK, b"\0")


class FakeSnesApp:
    """The SNES app's side of the rings (snes/src/link_io.c) on a FakeFxPak's memory, for tests
    of fxapp.py: start() as the app does at boot, send() a frame, received() what the box sent."""

    INBOX, RING = 0x7000, 256

    def __init__(self, cart: FakeFxPak):
        self.cart = cart

    def start(self):
        rom, s = self.cart.rom_mem, self.cart.sram
        self.tail = rom[self.INBOX + self.RING]  # skip what the box sent before
        self.head = rom[self.INBOX + self.RING + 1]  # our ring starts where the box has read
        s[4], s[5] = self.tail, self.head
        s[6] = (s[6] + 1) & 0xFF
        s[0:4] = b"SFE1"

    def send(self, data: bytes):
        s = self.cart.sram
        ack = self.cart.rom_mem[self.INBOX + self.RING + 1]
        assert (self.head - ack) % self.RING + len(data) <= self.RING - 1, "the app's ring is full"
        for b in data:
            s[8 + self.head] = b
            self.head = (self.head + 1) % self.RING
        s[5] = self.head

    def received(self) -> bytes:
        rom = self.cart.rom_mem
        head = rom[self.INBOX + self.RING]
        out = bytes(rom[self.INBOX + (self.tail + i) % self.RING] for i in range((head - self.tail) % self.RING))
        self.tail = head
        self.cart.sram[4] = self.tail
        return out
