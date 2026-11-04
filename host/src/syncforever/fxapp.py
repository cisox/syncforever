"""The Sync Forever SNES app's link, box side: docs/app-protocol.md's frames over two rings in
the cart's memory, which the FXPAK lets the box read and write over USB while a ROM runs
(fxpak.read_mem/write_mem; the app side is snes/src/link_io.c, the layout docs/snes-plan.md).

  box -> app: 256 bytes in the app's ROM at $00:F000 (SNES space 0x007000), the box's head
              after them (+0x100), then how far the box has read the app's ring (+0x101).
              The box rewrites all of it in one PUT, head and ack last, so the app never sees
              a head ahead of its bytes.
  app -> box: the app's SRAM (SNES space 0xE00000): "SFE1", how far the app has read the
              box's ring (+4), the app's head (+5), a counter it bumps at each start (+6),
              then its 256-byte ring (+8).

FxApp wraps an FxPak: app_read/fifo_write for AppServer, everything else (stat, open, ...)
straight to the cart. Only use it while INFO says the app is running: at the menu, SNES
space 0 is the menu's own ROM.

**The cart's autosave and our writes must not meet** (2026-11-03: uploads stalled the cart's
USB with the app up, never at the menu). While a ROM runs, the firmware's main loop takes a
CRC32 of save RAM about every 260 ms and, once it has seen a change and then 5 passes without
one, writes the save RAM to its .srm on the SD card (snes.c snes_main_loop). PUT data, file or
SNES space, is handled inside the USB interrupt (usbinterface.c usbint_recv_block): one landing
during that save hangs the cart's USB until a power cycle. Our app's ring lives in save RAM, so
every message changes it. So every PUT here waits for a moment no save can be running (_quiet):
within EARLY of a change the box has just seen (no save comes before ~1.3 s), or once save RAM
has been still for SETTLE (any save it caused is over).
"""

from __future__ import annotations

import time

from .fxpak import SRAM_BASE
from .protocol import FA_WRITE, LinkError

INBOX = 0x7000  # SNES space of $00:F000 in a 32 KiB LoROM
RING = 256
MAGIC = b"SFE1"
S_TAIL, S_HEAD, S_EPOCH, S_RING = 4, 5, 6, 8
SRAM_READ = S_RING + RING
POLL = 0.03  # seconds between SRAM reads while waiting
ROOM_WAIT = 5.0  # seconds to wait for the app to make room in the box's ring
# The autosave (module docstring): the earliest a save follows a change (5 passes >= 260 ms,
# 1.3 s) less a margin, and the latest one has finished (detection, 5 slow passes, the write).
EARLY = 1.0
SETTLE = 3.0
QUIET_POLL = 0.01
QUIET_MAX = 8.0  # longer than any gap between the app's own writes (HELLO every 2 s)
CONSUME_WAIT = 0.3  # seconds to watch the app take a message (its tail moves: a change)
PUT_RATE = 200 * 1024  # bytes/s, under the measured ~260 KiB/s: how long an upload holds the USB


class FxApp:
    def __init__(self, dev, *, clock=time.monotonic, sleep=time.sleep, save_file: str = ""):
        self.dev = dev
        self.save_file = save_file  # the app's .srm (sd2snes/saves/Sync Forever.srm): after_first_copy
        self.clock = clock
        self.sleep = sleep
        self.restarts = 0  # the app started again while this ran (its counter moved)
        # When save RAM last changed, as (no earlier than, no later than); None: unknown. It
        # may have changed just before we looked: assume it did.
        self.changed: tuple[float | None, float] = (None, clock())
        self._snap: bytes | None = None
        self._snap_at: float | None = None
        self._load()
        self._skip_old_messages()

    def _skip_old_messages(self) -> None:
        """Save RAM persists (the cart reloads its .srm when the app starts), so until the app has
        set up its side, the last session's messages are still there: a box that read them
        replayed HELLO, SYNC_PLAN and SYNC_RUN (a strict sync nobody asked for, 2026-11-03).
        Only what the app writes from now on counts; it says HELLO again every 2 s."""
        s = self._app()
        if s is not None:
            self.ack = s[S_HEAD]

    def _load(self) -> None:
        """Our side as the cart has it: the box may have restarted, or the ROM been reloaded."""
        mine = self.dev.read_mem(INBOX, RING + 2)
        self.ring = bytearray(mine[:RING])
        self.head, self.ack = mine[RING], mine[RING + 1]
        self.app_tail = self.head
        self.epoch: int | None = None

    def __getattr__(self, name):
        return getattr(self.dev, name)

    def _app(self) -> bytes | None:
        """The app's side (SRAM), or None before it has set it up."""
        s = self.dev.read_mem(SRAM_BASE, SRAM_READ)
        now = self.clock()
        if s != self._snap:
            self.changed = (self._snap_at, now)  # since our last look
        self._snap, self._snap_at = s, now
        if s[:4] != MAGIC:
            return None
        if self.epoch is not None and s[S_EPOCH] != self.epoch:
            # It took our positions as its start. Reread them anyway: a reloaded ROM (BOOT)
            # starts the box's side from zero.
            self.restarts += 1
            self._load()
        self.epoch = s[S_EPOCH]
        self.app_tail = s[S_TAIL]
        return s

    def app_read(self, timeout: float = 0.05) -> bytes:
        """What the app sent since the last call, waiting up to timeout for something."""
        end = self.clock() + timeout
        while True:
            s = self._app()
            if s is not None and s[S_HEAD] != self.ack:
                n = (s[S_HEAD] - self.ack) % RING
                data = bytes(s[S_RING + (self.ack + i) % RING] for i in range(n))
                self.ack = s[S_HEAD]
                self._quiet(0.02)
                self.dev.write_mem(INBOX + RING + 1, bytes([self.ack]))
                return data
            if self.clock() >= end:
                return b""
            self.sleep(POLL)

    def fifo_write(self, data: bytes) -> None:
        """Into the box -> app ring, as fast as the app reads it."""
        while data:
            self._quiet(0.02)  # first: it may find the app restarted and reload our side
            room = RING - 1 - (self.head - self.app_tail) % RING
            if not room:
                self._wait_for_room()
                continue
            chunk, data = data[:room], data[room:]
            for b in chunk:
                self.ring[self.head] = b
                self.head = (self.head + 1) % RING
            self.dev.write_mem(INBOX, bytes(self.ring) + bytes([self.head, self.ack]))
            self._watch_consume()

    def open(self, path: str, mode: int, size: int | None = None) -> None:
        """A file upload holds the cart's USB for its whole length: start it only when no
        autosave can come before it ends."""
        if mode & FA_WRITE:
            self._quiet((size or 0) / PUT_RATE + 0.1)
        self.dev.open(path, mode, size)

    def _quiet(self, duration: float) -> None:
        """Wait until a PUT of `duration` seconds can't meet the cart's autosave (module
        docstring): it ends within EARLY of a change we saw, or save RAM has been still for
        SETTLE. Reads save RAM as it waits, so a change the app makes is seen at once."""
        end = self.clock() + QUIET_MAX
        while True:
            self._app()
            now = self.clock()
            lo, hi = self.changed
            if lo is not None and now + duration < lo + EARLY:
                return
            if now >= hi + SETTLE:
                return
            if now >= end:
                raise LinkError("the SNES app keeps changing its save RAM: not writing to the cart now")
            self.sleep(QUIET_POLL)

    def _watch_consume(self) -> None:
        """Watch the app take what we just wrote (its tail moves): that change, timed closely,
        lets the next write go out at once instead of waiting SETTLE."""
        end = self.clock() + CONSUME_WAIT
        while self.clock() < end:
            self._app()
            if self.app_tail == self.head:
                return
            self.sleep(QUIET_POLL)

    def after_first_copy(self) -> None:
        """Move the app's .srm next to the card's free space (romsync.run_plan calls this once a
        sync's first file is on the card). Each autosave rewrites it (FA_CREATE_ALWAYS), and
        FatFs then looks for free space from that file's old place (ff.c, "Reuse the cluster
        hole"): with the .srm behind 46 GB a computer copied on, every new file walked 1.4
        million FAT entries in the USB interrupt, ~11 s each (2026-11-04). The first file has
        just left the search at the free space; deleted now, the next autosave makes the .srm
        there, and later searches start right behind it. It holds only this app's rings, which
        the app sets up afresh when it starts."""
        if self.save_file and self.dev.stat(self.save_file) is not None:
            self.dev.delete(self.save_file)

    def app_exit(self) -> None:
        """The app's B: back to the menu."""
        self.dev.menu_reset()

    def boot_app(self, path: str) -> None:
        """Start the app's file again (after the box updated it). The reloaded ROM's inbox is
        zeros, so the box's side starts over."""
        self.dev.boot(path)
        self._load()
        self._skip_old_messages()

    def _wait_for_room(self) -> None:
        end = self.clock() + ROOM_WAIT
        while self.clock() < end:
            self.sleep(POLL)
            self._app()
            if (self.head - self.app_tail) % RING < RING - 1:
                return
        raise LinkError("the SNES app isn't reading")
