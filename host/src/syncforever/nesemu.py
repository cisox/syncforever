"""Just enough NES to test the Sync Forever app (nes/) without a console.

A py65 6502 runs the ROM against a minimal PPU (nametables, palette, the vblank flag), a
controller, and optionally a fake EverDrive (FIFO + MCU command parser). It is a test
oracle, not an emulator: no rendering pipeline, no APU, no sprites, mapper 0 only.

Timing model, kept deliberately simple:
- A frame is FRAME_CYCLES CPU cycles; frame k's vblank is the first VBLANK_CYCLES of it (k >= 1).
- Reading $2002 outside a not-yet-seen vblank fast-forwards the clock to the next vblank
  start (returning 0), so the app's polled waits cost nothing to emulate. This is only
  valid because the app reads $2002 solely to wait for vblank.
- A $2006/$2007 write while rendering is on and outside vblank is recorded in
  `violations`: on hardware it corrupts the screen (the "garbage tiles" bug).
"""

from __future__ import annotations

import struct
import zlib
from collections import deque
from dataclasses import dataclass

from py65.devices.mpu6502 import MPU

FRAME_CYCLES = 29781  # NTSC: 262 scanlines x 341 dots / 3
VBLANK_CYCLES = 2273  # 20 scanlines
# On hardware, palette writes in the first part of vblank (rendering on) didn't stick: entry 2
# written ~70 cycles in kept its old colour, entries 14-15 at ~250 changed (N8 PRO on an NTSC
# NES, 2026-11-02; the cause is unknown). Modelled as: such writes before PAL_SETTLE are lost.
PAL_SETTLE = 150

BUTTONS = {"A": 0x80, "B": 0x40, "SELECT": 0x20, "START": 0x10, "UP": 0x08, "DOWN": 0x04, "LEFT": 0x02, "RIGHT": 0x01}

# EverDrive N8 PRO (edio/everdrive.h)
REG_FIFO_DATA, REG_FIFO_STAT, REG_MSTAT = 0x40F0, 0x40F1, 0x40FF
ADDR_CFG = 0x1800000
CFG_MAP_IDX = 32  # MapConfig: 8 cheat slots x 4 bytes, then map_idx
CMD_STATUS, CMD_MEM_WR, CMD_USB_WR = 0x10, 0x1A, 0x22

HL = 0x80  # the app: tile | HL = the same glyph on the highlight bar


class ExitedToMenu(Exception):
    """The app wrote a MapConfig with map_idx 255: on hardware the menu takes over."""


class CartError(Exception):
    """The app sent the fake EverDrive something it doesn't understand."""


@dataclass
class Violation:
    frame: int
    cycle_in_frame: int
    register: int
    pc: int


class FakeEverDrive:
    """The cart side of edio's FIFO: parses the commands the app sends, answers some.

    `usb_out` collects what the app sends to the box (CMD_USB_WR); `to_cpu` is what the
    app will read next, so a test (or the box) feeds it with `host_send`.
    """

    def __init__(self):
        self.to_cpu: deque[int] = deque()
        self.rx = bytearray()
        self.usb_out = bytearray()
        self.config: bytes | None = None
        self.mem_writes: list[tuple[int, bytes]] = []

    # registers
    def read(self, addr):
        if addr == REG_FIFO_DATA:
            return self.to_cpu.popleft() if self.to_cpu else 0
        if addr == REG_FIFO_STAT:
            return 0x00 if self.to_cpu else 0x80  # bit 7 set: nothing for the CPU
        if addr == REG_MSTAT:
            return 0xA1  # $Ax + STATUS_UNLOCK
        return addr >> 8  # open bus

    def write(self, addr, value):
        if addr == REG_FIFO_DATA:
            self.rx.append(value)
            self._parse()

    def host_send(self, data: bytes):
        """The box writes into the cart FIFO (MEM_WR to ADDR_FIFO on hardware)."""
        self.to_cpu.extend(data)

    # MCU
    def _parse(self):
        rx = self.rx
        while rx:
            if rx[0] == 0:  # ed_fifo_flush / halt "exec" bytes between commands
                del rx[0]
                continue
            if len(rx) < 4:
                return
            if rx[0] != ord("+") or rx[1] != ord("+") ^ 0xFF or rx[2] ^ rx[3] != 0xFF:
                raise CartError(f"bad command header {bytes(rx[:4]).hex()}")
            cmd, body = rx[2], rx[4:]
            used = self._command(cmd, body)
            if used is None:
                return  # wait for more bytes
            del rx[: 4 + used]

    def _command(self, cmd, body) -> int | None:
        if cmd == CMD_STATUS:
            self.to_cpu.extend((0x00, 0xA5))
            return 0
        if cmd == CMD_USB_WR:
            if len(body) < 2:
                return None
            (n,) = struct.unpack_from("<H", body)
            if len(body) < 2 + n:
                return None
            self.usb_out += body[2 : 2 + n]
            return 2 + n
        if cmd == CMD_MEM_WR:
            if len(body) < 9:
                return None
            addr, n = struct.unpack_from("<II", body)
            if len(body) < 9 + n:
                return None
            data = bytes(body[9 : 9 + n])
            self.mem_writes.append((addr, data))
            if addr == ADDR_CFG:
                self.config = data
                if len(data) > CFG_MAP_IDX and data[CFG_MAP_IDX] == 255:
                    raise ExitedToMenu()
            return 9 + n
        raise CartError(f"unsupported command 0x{cmd:02X}")


class Nes:
    def __init__(self, rom: bytes, *, cart: FakeEverDrive | None = None):
        if rom[:4] != b"NES\x1a":
            raise ValueError("not an iNES file")
        prg_banks, chr_banks = rom[4], rom[5]
        mapper = (rom[6] >> 4) | (rom[7] & 0xF0)
        if mapper != 0 or prg_banks not in (1, 2) or chr_banks != 1:
            raise ValueError(f"only NROM with 8 KB CHR ROM (mapper {mapper}, prg {prg_banks}, chr {chr_banks})")
        prg = rom[16 : 16 + prg_banks * 0x4000]
        self.prg = prg * (2 // prg_banks)
        self.chr = rom[16 + len(prg) : 16 + len(prg) + 0x2000]
        self.vertical = bool(rom[6] & 1)
        self.cart = cart

        self.ram = bytearray(0x800)
        self.nt = bytearray(0x800)
        self.pal = bytearray(32)
        self.ctrl = self.mask = 0
        self.lost_pal_writes = 0  # palette writes too early in vblank (PAL_SETTLE)
        self.vaddr = 0
        self.latch = False
        self.read_buf = 0
        self.seen_vblank = 0  # frame whose vblank flag was already read
        self.buttons = 0
        self.shift = 0
        self.violations: list[Violation] = []
        self.blanks = 0  # times rendering was switched off (a visible flash on a TV)
        self.exited = False

        self.mpu = MPU(memory=self)
        self.mpu.pc = self.prg[0x7FFC] | (self.prg[0x7FFD] << 8)

    # --- memory map (py65 indexes this object) ---------------------------------------
    def __getitem__(self, addr):
        if addr < 0x2000:
            return self.ram[addr & 0x7FF]
        if addr >= 0x8000:
            return self.prg[addr - 0x8000]
        if addr < 0x4000:
            return self._ppu_read(0x2000 + (addr & 7))
        if addr == 0x4016:
            bit = (self.shift >> 7) & 1
            self.shift = ((self.shift << 1) & 0xFF) | 1
            return 0x40 | bit
        if self.cart and 0x40F0 <= addr <= 0x41FF:
            return self.cart.read(addr)
        return addr >> 8  # open bus

    def __setitem__(self, addr, value):
        if addr < 0x2000:
            self.ram[addr & 0x7FF] = value
        elif addr < 0x4000:
            self._ppu_write(0x2000 + (addr & 7), value)
        elif addr == 0x4016:
            if value & 1:
                self.shift = self.buttons
        elif self.cart and 0x40F0 <= addr <= 0x41FF:
            try:
                self.cart.write(addr, value)
            except ExitedToMenu:
                self.exited = True
                raise

    # --- PPU --------------------------------------------------------------------------
    @property
    def cycles(self):
        return self.mpu.processorCycles

    @property
    def frame(self):
        return self.cycles // FRAME_CYCLES

    def _in_vblank(self):
        k, off = divmod(self.cycles, FRAME_CYCLES)
        return k >= 1 and off < VBLANK_CYCLES

    def _ppu_read(self, reg):
        if reg == 0x2002:
            self.latch = False
            k = self.frame
            if self._in_vblank() and self.seen_vblank != k:
                self.seen_vblank = k
                return 0x80
            # Nothing to see until the next vblank: skip ahead to it (see module docstring).
            self.mpu.processorCycles = (k + 1) * FRAME_CYCLES
            return 0
        if reg == 0x2007:
            value, self.read_buf = self.read_buf, self._vram_read(self.vaddr)
            self._advance()
            return value
        return 0

    def _ppu_write(self, reg, value):
        if reg in (0x2006, 0x2007) and (self.mask & 0x18) and not self._in_vblank():
            k, off = divmod(self.cycles, FRAME_CYCLES)
            self.violations.append(Violation(k, off, reg, self.mpu.pc))
        if reg == 0x2000:
            self.ctrl = value
        elif reg == 0x2001:
            if (self.mask & 0x18) and not (value & 0x18):
                self.blanks += 1
            self.mask = value
        elif reg == 0x2005:
            self.latch = not self.latch
        elif reg == 0x2006:
            if not self.latch:
                self.vaddr = ((value & 0x3F) << 8) | (self.vaddr & 0xFF)
            else:
                self.vaddr = (self.vaddr & 0x3F00) | value
            self.latch = not self.latch
        elif reg == 0x2007:
            self._vram_write(self.vaddr, value)
            self._advance()

    def _advance(self):
        self.vaddr = (self.vaddr + (32 if self.ctrl & 4 else 1)) & 0x3FFF

    def _nt_index(self, a):
        off = (a - 0x2000) & 0xFFF
        table = (off >> 10) & 1 if self.vertical else (off >> 11) & 1
        return table * 0x400 + (off & 0x3FF)

    def _vram_read(self, a):
        a &= 0x3FFF
        if a < 0x2000:
            return self.chr[a]
        if a < 0x3F00:
            return self.nt[self._nt_index(a)]
        return self.pal[self._pal_index(a)]

    def _vram_write(self, a, value):
        a &= 0x3FFF
        if a < 0x2000:
            return  # CHR ROM
        if a < 0x3F00:
            self.nt[self._nt_index(a)] = value
        else:
            off = self.cycles % FRAME_CYCLES
            if (self.mask & 0x18) and self._in_vblank() and off < PAL_SETTLE:
                self.lost_pal_writes += 1  # see PAL_SETTLE
                return
            self.pal[self._pal_index(a)] = value & 0x3F

    @staticmethod
    def _pal_index(a):
        i = a & 0x1F
        return i - 0x10 if i in (0x10, 0x14, 0x18, 0x1C) else i

    # --- the cart's USB side (sim.FakeN8Pro.on_start apps) ------------------------------
    def host_send(self, data: bytes) -> None:
        if self.cart:
            self.cart.host_send(data)

    def take_usb(self) -> bytes:
        if not self.cart:
            return b""
        out = bytes(self.cart.usb_out)
        self.cart.usb_out.clear()
        return out

    # --- driving it -------------------------------------------------------------------
    def run_frames(self, n: int, buttons: int | str = 0) -> None:
        """Run n frames holding `buttons` (mask or names like "A+DOWN"). Stops early on exit."""
        if isinstance(buttons, str):
            buttons = sum(BUTTONS[b] for b in buttons.upper().split("+")) if buttons else 0
        self.buttons = buttons
        end = (self.frame + n) * FRAME_CYCLES
        try:
            while self.cycles < end and not self.exited:
                self.mpu.step()
        except ExitedToMenu:
            pass
        self.buttons = 0

    def press(self, buttons: int | str, frames: int = 2, after: int = 4) -> None:
        """Tap: hold for `frames`, then release for `after` frames so the app redraws."""
        self.run_frames(frames, buttons)
        self.run_frames(after)

    # --- looking at it ----------------------------------------------------------------
    @property
    def shown_table(self) -> int:
        return self.ctrl & 1 if self.vertical else (self.ctrl >> 1) & 1

    def tiles(self, row: int) -> bytes:
        base = self.shown_table * 0x400 + row * 32
        return bytes(self.nt[base : base + 32])

    def text(self, row: int) -> str:
        return "".join(chr(t & 0x7F) if 0x20 <= (t & 0x7F) < 0x7F else "?" for t in self.tiles(row))

    def highlighted(self, row: int) -> bool:
        return all(t & HL for t in self.tiles(row))

    def screen(self) -> list[str]:
        """30 rows of text; a highlighted row is marked with '>' in place of its first column."""
        return [(">" + self.text(r)[1:]) if self.highlighted(r) else self.text(r) for r in range(30)]

    def screenshot(self, path) -> None:
        """PNG of the shown nametable, using the ROM's CHR, attributes and palette RAM."""
        w, h = 256, 240
        rows = []
        bg_table = 0x1000 if self.ctrl & 0x10 else 0
        nt = self.shown_table * 0x400
        for y in range(h):
            line = bytearray()
            for x in range(w):
                t = self.nt[nt + (y // 8) * 32 + x // 8]
                base = bg_table + t * 16 + (y % 8)
                lo, hi = self.chr[base], self.chr[base + 8]
                c = ((lo >> (7 - x % 8)) & 1) | (((hi >> (7 - x % 8)) & 1) << 1)
                attr = self.nt[nt + 0x3C0 + (y // 32) * 8 + x // 32]
                p = (attr >> (((y // 16) & 1) * 4 + ((x // 16) & 1) * 2)) & 3
                line += NES_RGB[self.pal[p * 4 + c] if c else self.pal[0]]
            rows.append(bytes(line))
        _write_png(path, w, h, rows)


def _write_png(path, w, h, rows):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + r for r in rows)
    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw)))
        f.write(chunk(b"IEND", b""))


# A common 2C02 approximation; only used for screenshots.
_PAL_HEX = (
    "545454 001E74 081090 300088 440064 5C0030 540400 3C1800 202A00 083A00 004000 003C00 00323C 000000 000000 000000 "
    "989698 084CC4 3032EC 5C1EE4 8814B0 A01464 982220 783C00 545A00 287200 087C00 007628 006678 000000 000000 000000 "
    "ECEEEC 4C9AEC 787CEC B062EC E454EC EC58B4 EC6A64 D48820 A0AA00 74C400 4CD020 38CC6C 38B4CC 3C3C3C 000000 000000 "
    "ECEEEC A8CCEC BCBCEC D4B2EC ECAEEC ECAED4 ECB4B0 E4C490 CCD278 B4DE78 A8E290 98E2B4 A0D6E4 A0A2A0 000000 000000"
)
NES_RGB = [bytes.fromhex(h) for h in _PAL_HEX.split()]
