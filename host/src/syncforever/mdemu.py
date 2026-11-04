"""Just enough Mega Drive to test the Sync Forever Genesis app (md/) without a console.

The Genesis twin of nesemu.py. A machine68k 68000 runs the ROM against a minimal VDP (VRAM,
CRAM, VSRAM through the data and control ports), the 3-button pad, and a fake Mega EverDrive
(FIFO + MCU command parser, big-endian like the real cart). A test oracle, not an emulator:
no rendering pipeline, no DMA, no interrupts, no Z80 or sound.

Timing model: the app waits for vblank only through edio's gVsync(), which polls the VDP
status until the vblank flag clears, then until it sets. Status reads alternate "not in
vblank" / "in vblank", and each "in vblank" read starts a new frame, so a frame costs two
reads however long the app took. Valid only because the app reads the status solely to wait.

machine68k is GPL-2.0 and a dev-only dependency: only tests import this module; the box never
does, and nothing here is shipped.
"""

from __future__ import annotations

import struct
from collections import deque

import machine68k

from .nesemu import CartError, _write_png

CYCLES_PER_FRAME = 127_840  # NTSC 68000: 7.67 MHz / 60
HANG_FRAMES = 30  # this many frames' cycles without a vblank wait: the app is stuck

BUTTONS = {"UP": 0x01, "DOWN": 0x02, "LEFT": 0x04, "RIGHT": 0x08, "B": 0x10, "C": 0x20, "A": 0x40, "START": 0x80}

# Mega EverDrive PRO (md/edio/everdrive.h)
EDIO = 0xA130D0
REG_FIFO_DATA, REG_FIFO_STAT, REG_SYS_STAT, REG_TIMER = EDIO + 1, EDIO + 2, EDIO + 4, EDIO + 6
FIFO_EMPTY = 0x8000  # FIFOSTAT bit 15 set: nothing for the CPU (as the app reads it; verified on hardware)
FIFO_COUNT = 0x7FF
CMD_STATUS, CMD_USB_WR, CMD_REINIT, CMD_F_DIR_LD = 0x10, 0x22, 0x25, 0xC5

VDP_DATA, VDP_CTRL = 0xC00000, 0xC00004
VBLANK_FLAG = 0x0008
PLANE_A = 0xC000  # edio's APLAN
PLANE_W, SCREEN_W, SCREEN_H = 64, 40, 28
JOY_DATA_1 = 0xA10003


class FakeMegaEverDrive:
    """The cart side of edio's FIFO on the Mega: parses the commands the app sends, answers some.

    `usb_out` collects what the app sends to the box (CMD_USB_WR); `to_cpu` is what the app
    will read next, so a test (or the box) feeds it with `host_send`.
    """

    def __init__(self):
        self.to_cpu: deque[int] = deque()
        self.rx = bytearray()
        self.usb_out = bytearray()
        self.rebooted = False
        self.commands: list[int] = []

    def fifo_stat(self) -> int:
        n = min(len(self.to_cpu), FIFO_COUNT)
        return n if n else FIFO_EMPTY

    def read_data(self) -> int:
        return self.to_cpu.popleft() if self.to_cpu else 0

    def write_data(self, value: int) -> None:
        self.rx.append(value)
        self._parse()

    def host_send(self, data: bytes) -> None:
        """The box writes into the cart FIFO (MEM_WR to ADDR_FIFO on hardware)."""
        self.to_cpu.extend(data)

    def _parse(self):
        rx = self.rx
        while rx:
            if rx[0] == 0:  # ed_fifo_flush and the halt routine's "exec" bytes
                del rx[0]
                continue
            if len(rx) < 4:
                return
            if rx[0] != ord("+") or rx[1] != ord("+") ^ 0xFF or rx[2] ^ rx[3] != 0xFF:
                raise CartError(f"bad command header {bytes(rx[:4]).hex()}")
            used = self._command(rx[2], rx[4:])
            if used is None:
                return  # wait for more bytes
            self.commands.append(rx[2])
            del rx[: 4 + used]

    def _command(self, cmd, body) -> int | None:
        if cmd == CMD_STATUS:
            self.to_cpu.extend((0xA5, 0x00))  # big-endian u16 0xA500: STATUS_KEY_OLD, no error
            return 0
        if cmd == CMD_F_DIR_LD:  # ed_init's "is the disk up" check: args u8, path str
            if len(body) < 3:
                return None
            (n,) = struct.unpack_from(">H", body, 1)
            return 3 + n if len(body) >= 3 + n else None
        if cmd == CMD_USB_WR:
            if len(body) < 2:
                return None
            (n,) = struct.unpack_from(">H", body)
            if len(body) < 2 + n:
                return None
            self.usb_out += body[2 : 2 + n]
            return 2 + n
        if cmd == CMD_REINIT:  # ed_cmd_reboot: back to the menu
            self.rebooted = True
            return 0
        raise CartError(f"unsupported command 0x{cmd:02X}")


class Vdp:
    def __init__(self):
        self.vram = bytearray(0x10000)
        self.cram = [0] * 64
        self.vsram = [0] * 40
        self.regs = [0] * 32
        self.code = 0
        self.addr = 0
        self.pending = False  # the first half of a command word arrived
        self.vblank = True

    def control(self, v: int) -> None:
        if self.pending:
            self.pending = False
            self.addr = (self.addr & 0x3FFF) | ((v & 3) << 14)
            self.code = (self.code & 3) | ((v >> 2) & 0x3C)
        elif v & 0xC000 == 0x8000:
            self.regs[(v >> 8) & 0x1F] = v & 0xFF
        else:
            self.pending = True
            self.code = (v >> 14) & 3
            self.addr = v & 0x3FFF

    def data(self, v: int) -> None:
        self.pending = False
        code = self.code & 0xF
        if code == 1:
            a = self.addr & 0xFFFE
            self.vram[a : a + 2] = v.to_bytes(2, "big")
        elif code == 3:
            self.cram[(self.addr >> 1) & 63] = v & 0x0EEE
        elif code == 5:
            self.vsram[(self.addr >> 1) % 40] = v & 0x7FF
        else:
            raise CartError(f"VDP data write with code {self.code:#x}")
        self.addr = (self.addr + self.regs[15]) & 0xFFFF

    def status(self) -> int:
        self.pending = False
        return 0x3400 | (VBLANK_FLAG if self.vblank else 0)

    def cell(self, x: int, y: int, plane: int = PLANE_A) -> int:
        a = plane + (y * PLANE_W + x) * 2
        return int.from_bytes(self.vram[a : a + 2], "big")

    def rgb(self, index: int) -> tuple[int, int, int]:
        c = self.cram[index]
        return tuple(((c >> shift) & 0xE) * 255 // 14 for shift in (0, 4, 8))


class MegaDrive:
    def __init__(self, rom: bytes, *, cart: FakeMegaEverDrive | None = None):
        if rom[0x100:0x104] != b"SEGA":
            raise ValueError("not a Mega Drive ROM (no SEGA at 0x100)")
        self.cart = cart
        self.vdp = Vdp()
        self.frame = 0
        self.buttons = 0
        self.th = 0x40
        self.exited = False
        self._target = 0
        self.mach = machine68k.Machine(machine68k.CPUType.M68000, 16 * 1024)
        self.mem, self.cpu = self.mach.mem, self.mach.cpu
        self.mem.w_block(0, rom)
        for page in (0xA0, 0xA1, 0xC0):
            for width in (0, 1, 2):
                self.mem.set_special_range_read_func(page << 16, width, self._reader(width))
                self.mem.set_special_range_write_func(page << 16, width, self._writer(width))
        self.cpu.pulse_reset()

    # --- the bus ----------------------------------------------------------------------
    def _reader(self, width):
        def read(addr):
            if width == 2:
                return (self._read16(addr) << 16) | self._read16(addr + 2)
            if width == 1:
                return self._read16(addr)
            word = self._read16(addr & ~1)
            return word & 0xFF if addr & 1 else word >> 8

        return read

    def _writer(self, width):
        def write(addr, value):
            if width == 2:
                self._write16(addr, value >> 16, 0xFFFF)
                self._write16(addr + 2, value & 0xFFFF, 0xFFFF)
            elif width == 1:
                self._write16(addr, value, 0xFFFF)
            elif addr & 1:
                self._write16(addr & ~1, value & 0xFF, 0x00FF)
            else:
                self._write16(addr, (value & 0xFF) << 8, 0xFF00)

        return write

    def _read16(self, addr: int) -> int:
        if addr in (VDP_CTRL, VDP_CTRL + 2):
            return self._status()
        if addr == JOY_DATA_1 - 1:
            return self._pad()
        if addr == 0xA10000:
            return 0x00A0  # version register: overseas NTSC, no TMSS
        if self.cart and addr == REG_FIFO_DATA - 1:
            return self.cart.read_data()
        if self.cart and addr == REG_FIFO_STAT:
            return self.cart.fifo_stat()
        return 0

    def _write16(self, addr: int, value: int, mask: int) -> None:
        if addr in (VDP_CTRL, VDP_CTRL + 2):
            self.vdp.control(value)
        elif addr in (VDP_DATA, VDP_DATA + 2):
            self.vdp.data(value)
        elif addr == JOY_DATA_1 - 1 and mask & 0xFF:
            self.th = value & 0x40
        elif self.cart and addr == REG_FIFO_DATA - 1 and mask & 0xFF:
            self.cart.write_data(value & 0xFF)
            if self.cart.rebooted:
                self.exited = True
                self.mach.abort_execute()
        # everything else (TMSS, Z80, PSG, pad control, SYSSTAT) is ignored

    def _status(self) -> int:
        """Alternate the vblank flag; past the target frame, hold it off: the app then waits
        in gVsync until the next run_frames (abort_execute can't stop a slice from here)."""
        if self.vdp.vblank or self.frame < self._target:
            self.vdp.vblank = not self.vdp.vblank
            if self.vdp.vblank:
                self.frame += 1
        return self.vdp.status()

    def _pad(self) -> int:
        b = self.buttons
        if self.th:  # TH high: C B R L D U
            pressed = b & 0x3F
        else:  # TH low: Start A 0 0 D U
            pressed = (b & 0x03) | ((b & 0x40) >> 2) | ((b & 0x80) >> 2) | 0x0C
        return ((~pressed & 0x3F) | self.th) & 0xFF

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
        self._target = self.frame + n
        spent = 0
        while self.frame < self._target and not self.exited:
            before = self.frame
            self.mach.execute(CYCLES_PER_FRAME)
            spent = 0 if self.frame != before else spent + 1
            if spent > HANG_FRAMES:
                raise RuntimeError(f"app stuck at PC {self.cpu.r_pc():#x}: no vblank wait in {HANG_FRAMES} frames")
        self.buttons = 0

    def press(self, buttons: int | str, frames: int = 2, after: int = 4) -> None:
        """Tap: hold for `frames`, then release for `after` frames so the app redraws."""
        self.run_frames(frames, buttons)
        self.run_frames(after)

    # --- looking at it ----------------------------------------------------------------
    def text(self, row: int) -> str:
        """Plane A's row as text: edio's font is the ASCII code as the tile number."""
        chars = []
        for x in range(SCREEN_W):
            c = self.vdp.cell(x, row) & 0x7FF
            chars.append(chr(c) if 32 <= c < 127 else " ")
        return "".join(chars)

    def ink(self, row: int, x: int) -> int:
        """The CRAM colour (0x0BGR) of the text at (x, row): ink is colour 15 of its palette."""
        pal = (self.vdp.cell(x, row) >> 13) & 3
        return self.vdp.cram[pal * 16 + 15]

    def tiles(self, row: int) -> list[int]:
        return [self.vdp.cell(x, row) & 0x7FF for x in range(SCREEN_W)]

    def screen(self) -> list[str]:
        return [self.text(r) for r in range(SCREEN_H)]

    def screenshot(self, path) -> None:
        """Plane A over the backdrop, as a PNG (no plane B, no sprites, no scrolling)."""
        backdrop = self.vdp.rgb(self.vdp.regs[7] & 0x3F)
        rows = []
        for y in range(SCREEN_H * 8):
            line = bytearray()
            for x in range(SCREEN_W * 8):
                cell = self.vdp.cell(x // 8, y // 8)
                tile, pal = cell & 0x7FF, (cell >> 13) & 3
                px, py = x % 8, y % 8
                if cell & 0x0800:  # horizontal flip
                    px = 7 - px
                if cell & 0x1000:  # vertical flip
                    py = 7 - py
                byte = self.vdp.vram[(tile * 32 + py * 4 + px // 2) & 0xFFFF]
                index = (byte >> 4) if px % 2 == 0 else (byte & 15)
                line += bytes(self.vdp.rgb(pal * 16 + index) if index else backdrop)
            rows.append(bytes(line))
        _write_png(path, SCREEN_W * 8, SCREEN_H * 8, rows)
