"""The box side of the Sync Forever NES app: answer its requests (docs/app-protocol.md).

The app shows a sync plan (romsync.py) of the ROM library (a folder) against the
cart, and runs it on request. The box writes while the app runs and idles: the design's rule.
Before the first plan of a session it backs up every save: the menu isn't running, so this is
the one moment the box can read the cart at length without meeting a busy menu.
"""

from __future__ import annotations

import struct
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from . import firmware as fw
from . import ops, romsync, saves
from .protocol import EverDriveError

PROTO_VERSION = 7
BOOT_AFTER = 1.0  # seconds the SNES app shows RESTART's text before the box restarts it
MAGIC = b"SF"
MAX_PAYLOAD = 1024

# app -> box
HELLO, SYNC_PLAN, SYNC_RUN, PING, EXIT, STOP = 0x01, 0x06, 0x07, 0x0A, 0x0B, 0x0C
# box -> app
WELCOME, STATUS, RESTART, ERROR, PLAN, SYNC_DONE, COPY, PONG = (
    0x81,
    0x83,
    0x84,
    0x85,
    0x86,
    0x87,
    0x89,
    0x8A,
)
COPY_EVERY = 0.25  # COPY progress frames at most this often (plus each file's first and last)
LIBRARY_CHECK = 10.0  # seconds between looks at the library while the app shows a plan
STOP_LISTEN = 0.1  # seconds the box listens for the app's STOP after each file's STATUS

DEFAULT_MODE = 0xFF  # SYNC_PLAN: "the box's default mode"
# PLAN's firmware state: no line, the cart runs the newest (green), a newer one is waiting (red;
# on the X7 and FXPAK: a newer one exists, installed from a computer), the newest isn't known
# (plain; X7 and FXPAK)
FW_NONE, FW_CURRENT, FW_WAITING, FW_UNKNOWN = 0, 1, 2, 3
# PLAN's MSU setting (the sync screen shows it): the cart plays no MSU games, off, on
MSU_NONE, MSU_OFF, MSU_ON = 0, 1, 2

# Relaunching our app after replacing it: it shows RESTART's text and exits; the box starts the
# new copy through the menu (*n, *s). Talking to the menu as it comes back shows Error:40/48/2A
# (pings every 0.5 s from 0.5 s, launch at the first answer; hardware, 2026-11-01). So: first
# ping MENU_FIRST_PING after RESTART (the app exits ~0.75 s after it), launch MENU_SETTLE after
# the first answer. 1.5 + 0.3 tested clean (started in 2.3 s), then
# failed once with Error:40 on the N8 (2.81 s, 2026-11-02);
# 3.0 + 2.0 tested clean too, and is what we use: 2.25 s from the app's exit to the first ping.
# The app shows "Updating..." then "Restarting..." for APP_EXIT_AFTER before it exits
# (EXIT_DELAY + RESTART_HOLD frames in nes/src/plat.c and md/src/main.c), so the timings count from there.
APP_EXIT_AFTER = 2.25
MENU_PING_EVERY = 0.5
MENU_WAIT = 15.0
MENU_FIRST_PING = APP_EXIT_AFTER + 2.25
MENU_SETTLE = 2.0
WAIT_STEP = 0.02  # poll() sleep while waiting, so serve doesn't spin
# Carts with reset_to_menu (the Mega) skip the pings: the box resets the console into the menu
# RESET_AFTER the RESTART (once the app's two screens have shown) and launches when the menu
# sends `r`.
# Pinging a Mega menu as it boots showed Error:2A/40 (hardware, 2026-11-02).
RESET_AFTER = APP_EXIT_AFTER + 0.25
# The app is open while it has been heard from this recently: it pings every second while
# idle, says hello every 2 s while connecting, and waits silently only on the box's answer.
APP_QUIET = 3.0


@dataclass
class PendingLaunch:
    cart_path: str
    started: float
    deadline: float
    next_ping: float
    answered_at: float | None = None  # first time the menu answered
    reset: bool = False  # reset into the menu (CartProfile.menu_reset) instead of pinging


TEXT_MAX = 30  # the app keeps 30 characters of a message
DETAIL_MAX = 63  # a STATUS detail (a file name): the app shortens it to fit its line


def _duration(secs: float) -> str:
    """Short enough for the status line: "45 s", "14 min", "1 h 14 min"."""
    if secs < 60:
        return f"{max(1, round(secs))} s"
    mins = round(secs / 60)
    return f"{mins} min" if mins < 60 else f"{mins // 60} h {mins % 60} min"


def _what_changed(plan: romsync.Plan) -> str:
    """A finished sync, for the line under the status (DETAIL_MAX, and about 30 columns shown)."""
    counts = (("Moved", plan.moves), ("Added", plan.add), ("Replaced", plan.replace), ("Removed", plan.remove))
    return "  ".join(f"{word} {len(items)}" for word, items in counts if items)


# --- framing ----------------------------------------------------------------------------


def frame(kind: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload too long ({len(payload)})")
    return MAGIC + bytes([kind]) + struct.pack("<H", len(payload)) + payload


def text(s: str, limit: int = 255) -> bytes:
    """str field: u8 length + printable ASCII ('?' for anything else)."""
    b = bytes(c if 0x20 <= c < 0x7F else 0x3F for c in s.encode("utf-8", "replace"))[:limit]
    return bytes([len(b)]) + b


class Deframer:
    """Pull frames out of a byte stream, skipping anything that isn't one."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(MAGIC)
            if i < 0:
                del self.buf[: max(0, len(self.buf) - 1)]  # keep a trailing 'S'
                return out
            del self.buf[:i]
            if len(self.buf) < 5:
                return out
            kind = self.buf[2]
            (n,) = struct.unpack_from("<H", self.buf, 3)
            if n > MAX_PAYLOAD:
                del self.buf[:1]  # not a real frame: resync
                continue
            if len(self.buf) < 5 + n:
                return out
            out.append((kind, bytes(self.buf[5 : 5 + n])))
            del self.buf[: 5 + n]


# --- server -----------------------------------------------------------------------------


class AppServer:
    def __init__(
        self,
        dev,
        library: Path | None,
        *,
        dest: str,
        policy: ops.WritePolicy,
        manifest: romsync.Manifest,
        saves: saves.SaveStore | None = None,
        sync_mode: str = "add",
        msu_on: bool = False,
        first_look: str = "auto",
        update_app: Callable[[], bool] = lambda: False,
        firmware: Callable[[], fw.Firmware | None] = lambda: None,
        firmware_record: Callable[[str, bool], None] = lambda file, present: None,
        app_path: str = "",
        on_connect: Callable[[], None] = lambda: None,
        firmware_line: Callable[[], tuple[str, int]] | None = None,
        copy_panel: bool = False,
        log: Callable[[str], None] = lambda msg: None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.dev = dev
        self.library = Path(library) if library else None  # None: not set (saves only)
        self.dest = policy.check(dest)
        self.policy = policy
        self.manifest = manifest
        self.saves = saves
        self.backed_up = False  # this session's save backup is done
        self.saves_text = ""  # its result, sent with every PLAN
        self.firmware = firmware
        self.firmware_record = firmware_record  # (file, present): what the box put on the card
        self.fw_text, self.fw_state = "", FW_NONE  # the app's firmware line, with every PLAN
        if sync_mode not in romsync.MODES:
            raise ValueError(f"unknown sync mode {sync_mode!r}")
        self.sync_mode = sync_mode
        self.first_look = first_look  # the console's first_look setting (romsync.FIRST_LOOKS)
        self.msu_on = msu_on  # the console's msu setting: MSU games sync with their audio (msu.py)
        self.update_app = update_app  # brings the app on the cart up to date; True if it changed
        self.app_path = ops.norm(app_path) if app_path else ""
        self.on_connect = on_connect  # before WELCOME (the X7: scan its emulators)
        self.firmware_line = firmware_line  # (text, state) instead of the .efu check (the X7)
        self.copy_panel = copy_panel  # the app shows COPY progress (the X7's; not the PRO apps)
        # Plan again when the library changes while the app shows a plan (_watch_library).
        self.watch_library = True
        self._library_seen: tuple | None = None  # (library_stamp, when) of the plan shown
        self._library_offline = False  # the watch found it gone: no sync until it's back
        self.plan_number = 0  # of the last PLAN sent (SYNC_RUN says which one the app showed)
        self.sync_result = ""  # SYNC_DONE's text, until the plan after it has gone out
        self.note = ""  # the line under the status with the next PLAN: a sync's summary, say
        self._copy_sent = (0.0, None)  # (when, (n, stage)) of the last COPY
        self.clock = clock
        self.sleep = sleep
        self.pending: PendingLaunch | None = None  # relaunching the app
        self.launched: list[str] = []  # cart paths started, for tests and the log
        self.launched_at: float | None = None  # when the box last started the app itself
        self.plan: romsync.Plan | None = None  # last plan shown to the app
        self.greeted = False  # this server WELCOMEd the app (PONG tells an app it doesn't know to say hello)
        self.heard: float | None = None  # when the app last sent something (after answering it)
        self.log = log
        self.rx = Deframer()

    # transport
    def poll(self, timeout: float = 0.05) -> list[int]:
        """Read and answer what the app sent; returns the message types handled."""
        if self.pending:
            self._wait_for_menu()
            return []
        data = self.dev.app_read(timeout)
        handled = []
        for kind, payload in self.rx.feed(data):
            self.handle(kind, payload)
            handled.append(kind)
        if handled:
            self.heard = self.clock()  # after the answer: a long sync doesn't age it
        self._watch_library()
        return handled

    def just_launched(self, within: float) -> bool:
        """The box itself started the app this recently: the menu's recent list changes, but
        that's no game start (and if the launch failed, the menu is up)."""
        return self.launched_at is not None and self.clock() - self.launched_at < within

    def app_active(self) -> bool:
        """Our app is open: nothing but its answers may go to the cart (its frames would mix
        with a cart reply)."""
        return self.heard is not None and self.clock() - self.heard < APP_QUIET

    def send(self, kind: int, payload: bytes = b"") -> None:
        self.dev.fifo_write(frame(kind, payload))

    def _text(self, msg: str) -> bytes:
        return text(msg, TEXT_MAX)

    # requests
    def handle(self, kind: int, payload: bytes) -> None:
        try:
            if kind == HELLO:
                self._hello(payload)
            elif kind == SYNC_PLAN:
                self._sync_plan(payload[0])
            elif kind == SYNC_RUN:
                self._sync_run(payload[0], payload[1])
            elif kind == PING:
                self.send(PONG, bytes([self.greeted]))
            elif kind == EXIT:
                self._exit()
            elif kind == STOP:
                pass  # a sync's B, after it ended: nothing to stop
            else:
                self.log(f"ignored message 0x{kind:02X}")
        except (struct.error, IndexError):
            self._error("Bad request")
        except (EverDriveError, OSError, ValueError) as e:
            self.log(f"error: {e}")
            if isinstance(e, ValueError):
                self._error(str(e))  # written for the screen
            else:
                self._error("Couldn't finish", str(e))

    def _exit(self) -> None:
        """EXIT: the SNES app's B. The box resets the console to the menu (FXPAK MENU_RESET)."""
        leave = getattr(self.dev, "app_exit", None)
        if leave is None:
            self.log("ignored EXIT: this cart's app leaves by itself")
            return
        self.log("app exit: back to the menu")
        leave()

    def _error(self, msg: str, detail: str | None = None) -> None:
        """detail: the line under the status (a file's name, or what went wrong)."""
        self.send(ERROR, self._text(msg) + (b"" if detail is None else text(detail, DETAIL_MAX)))

    def _hello(self, payload: bytes) -> None:
        version = payload[0]
        now = self.clock()
        # The app runs, the menu doesn't: a safe moment to bring the app's file up to date.
        welcome = "Connected"
        if self.update_app():
            boot = getattr(self.dev, "boot_app", None)
            if boot:
                # The SNES app: the box starts the new file itself (FXPAK BOOT).
                self.send(RESTART, self._text("Updating..."))
                self.sleep(BOOT_AFTER)
                boot(self.app_path)
                self.log("app updated: started the new copy")
                return
            if not self.dev.profile.menu_launch:
                # The X7: the box can't start a ROM from its menu, and a ROM can't get back to
                # it. The running copy carries on; the new file runs from the next start.
                self.log("app updated on the card: the new version runs from the next start")
                welcome = "Updated for next start"
                self.note = "Updated: power off and on to use"  # WELCOME's text is gone by then
            else:
                self.send(RESTART, self._text("Updating..."))
                reset = self.dev.profile.menu_reset
                first = now + (RESET_AFTER if reset else MENU_FIRST_PING)
                self.pending = PendingLaunch(self.app_path, now, now + MENU_WAIT, first, reset=reset)
                self.log("restarting the app once the menu is back")
                return
        if version != PROTO_VERSION:
            self.log(f"app speaks v{version}, the box v{PROTO_VERSION}")
            return self._error(f"App too old (v{version})")
        self.log("app connected")
        self.on_connect()
        self.backed_up = False
        self.greeted = True
        self.send(WELCOME, self._text(welcome))

    # sync
    def _mode(self, code: int) -> str:
        if code == DEFAULT_MODE:
            return self.sync_mode
        if code >= len(romsync.MODES):
            raise ValueError("Unknown sync mode")
        return romsync.MODES[code]

    def _progress(
        self, msg: str, detail: str | None = None, expect: int | None = None, left: int | None = None
    ) -> None:
        """STATUS: the status line, and the line under it (a file's name; apps shorten it in
        the middle); during a sync, the seconds this copy should take (0 for a removal) and
        the seconds everything left should (the SNES and Genesis apps' sync screen counts them:
        the box sends nothing while a file copies). Apps read only as far as they know."""
        body = self._text(msg)
        if detail is not None:
            body += text(detail, DETAIL_MAX)
            if expect is not None or left is not None:
                body += struct.pack("<HI", min(expect or 0, 0xFFFF), min(left or 0, 0xFFFFFFFF))
        self.send(STATUS, body)

    def _watch_library(self) -> None:
        """While the app shows a plan, look at the library every LIBRARY_CHECK seconds; if it
        changed, plan again from the same cart listing (nothing changes the cart while the app is
        open) and send the new PLAN unasked: the app shows it ("Library changed")."""
        if not self.watch_library or self.plan is None or self._library_seen is None or not self.app_active():
            return
        stamp, at = self._library_seen
        now = self.clock()
        if now - at < LIBRARY_CHECK:
            return
        try:
            new = romsync.library_stamp(self.library)
            if not self.library.is_dir():
                self._library_seen = (stamp, now)
                self._library_gone("Library offline")
                return
            self._library_seen = (new, now)
            if new == stamp and not self._library_offline:
                return
            # First, so the app waits for the new PLAN (planning can take seconds over a network),
            # instead of missing its PONGs and showing "Disconnected" (2026-11-04).
            self.send(STATUS, self._text("Library changed..."))
            self.plan = romsync.make_plan(
                self.dev,
                self.library,
                self.dest,
                self.plan.mode,
                self.manifest,
                keep=(self.app_path,),
                msu=self.dev.profile.msu,
                msu_on=self.msu_on,
                first_look=self.first_look,
                tree=self.plan.tree,
            )
        except (EverDriveError, OSError, ValueError) as e:
            self._library_gone(str(e))  # the app is waiting for a plan: it hears why there's none
            return
        self._library_offline = False
        self._log_plan("library changed: sync plan")
        self._send_plan()

    def _library_gone(self, why: str) -> None:
        """Keep the plan on screen, refuse to run it, and say so once."""
        if not self._library_offline:
            self.log(f"library check: {why}")
            self._error(why)
        self._library_offline = True

    def _stop_requested(self) -> bool:
        """The app's B during a sync: it sends STOP when a STATUS reaches it (on the SNES it
        can't speak while a file copies), so listen briefly after each one."""
        for kind, _payload in self.rx.feed(self.dev.app_read(STOP_LISTEN)):
            if kind == STOP:
                return True
            self.log(f"ignored message 0x{kind:02X} during a sync")
        return False

    def _backup_saves(self) -> list | None:
        """Every save, now (the menu isn't running). Sets saves_text, the app's "Saves:" line.
        None when skipped or failed (logged)."""
        self.backed_up = True
        if self.saves is None:
            self.saves_text = "backup off"
            return None
        if self.saves.missing():  # don't create it: it may just not be mounted
            self.log(f"save folder missing: {self.saves.missing()}")
            self.saves_text = "folder missing"
            return None
        self._progress("Backing up saves")
        said_at = self.clock()
        eta = romsync.Eta(1, self.clock)

        def tick(done: int, total: int, name: str) -> None:
            nonlocal said_at
            if self.clock() - said_at >= romsync.STATUS_EVERY:
                said_at = self.clock()
                eta.total = total
                left = eta.left(done - 1)
                self._progress(f"Saves {done} of {total}", name, left=None if left is None else max(1, round(left)))

        try:
            results = saves.backup_all(self.dev, self.saves, in_app=True, progress=tick)
        except (EverDriveError, OSError) as e:  # logged; the sync still goes ahead
            self.log(f"save backup failed: {e}")
            self.saves_text = "backup failed"
            return None
        self.log(f"saves: {saves.summary(results)}")
        new = sum(r.status == "saved" for r in results)
        n = len(results)
        self.saves_text = (
            f"up to date ({n})" if not new else f"Backed up ({n})" if new == n else f"{new} backed up ({n})"
        )
        return results

    def _sync_plan(self, code: int) -> None:
        if not self.backed_up:
            self._backup_saves()
            self._progress("Checking firmware")
            self._firmware()
            self._progress("Scanning library")
        mode = self._mode(code)
        if self.library is None:
            raise ValueError("No library set")
        if not self.library.is_dir():
            raise ValueError("Library offline")
        self.plan = romsync.make_plan(
            self.dev,
            self.library,
            self.dest,
            mode,
            self.manifest,
            progress=self._progress,
            keep=(self.app_path,),
            msu=self.dev.profile.msu,
            msu_on=self.msu_on,
            first_look=self.first_look,
        )
        self._library_seen = (romsync.library_stamp(self.library), self.clock())
        if self.sync_result:  # the plan's progress lines replaced it: the app keeps what's up
            self._progress(self.sync_result)
            self.sync_result = ""
        self._log_plan("sync plan")
        self._send_plan()

    def _log_plan(self, what: str) -> None:
        plan = self.plan
        msu = f", {plan.msu_games} MSU games included" if plan.msu_games else ""
        self.log(f"{what} ({plan.mode}): {plan.summary()}; {plan.unchanged} files up to date{msu}")
        if plan.trusted:
            spot = f" after a spot check of {plan.spot_checked}" if plan.spot_checked else ""
            self.log(f"  {plan.trusted} files on the card taken as the library's by name and size{spot}")
        if plan.spot_failed:
            self.log(f"  a spot check found a file that differs: {plan.spot_failed}; checking every file")
        for rel in plan.skipped:
            self.log(f"  skipped (the cart can't store this name): {rel}")
        for rel in plan.left_out:
            self.log(f"  left out (an MSU game; [{self.dev.profile.key}] msu is off): {rel}")

    def _send_plan(self) -> None:
        mode = self.plan.mode
        cap = lambda items: min(len(items), 0xFFFF)
        body = bytes([romsync.MODES.index(mode)]) + self._text(romsync.MODE_NAMES[mode])
        adds = len(self.plan.add) + len(self.plan.moves)  # a move puts a file at a new name: shown as an add
        body += struct.pack("<HHH", min(adds, 0xFFFF), cap(self.plan.replace), min(self.plan.removals, 0xFFFF))
        body += self._text(self.saves_text)
        body += self._text(self.fw_text) + bytes([self.fw_state])
        body += bytes([MSU_ON if self.msu_on else MSU_OFF] if self.dev.profile.msu else [MSU_NONE])
        self.plan_number = (self.plan_number + 1) & 0xFF
        body += bytes([self.plan_number])
        body += text(self.note, DETAIL_MAX)
        self.note = ""
        self.send(PLAN, body)

    # firmware (firmware.py): once per session, with the save backup
    def _firmware(self) -> None:
        if self.firmware_line is not None:
            try:
                self.fw_text, self.fw_state = self.firmware_line()
            except (EverDriveError, OSError, ValueError) as e:
                self.log(f"firmware check failed: {e}")
                self.fw_text, self.fw_state = "", FW_NONE
            return
        info = self.firmware()
        if info is None:
            self.fw_text, self.fw_state = "", FW_NONE
            return
        self.fw_text = f"v{info.release.version}"
        try:
            current = self._runs(info)
        except (EverDriveError, OSError, ValueError) as e:
            self.log(f"firmware check failed: {e}")
            self.fw_text, self.fw_state = "", FW_NONE
            return
        self.fw_state = FW_CURRENT if current else FW_WAITING
        try:
            if current:
                self._remove_firmware_files(info)
            elif info.path:
                self._deliver_firmware(info)
        except (EverDriveError, OSError, ValueError) as e:  # logged; never in the sync's way
            self.log(f"firmware delivery failed: {e}")

    def _runs(self, info: fw.Firmware) -> bool:
        """The cart runs info.release: its menu file matches the .efu's (read now: the app runs,
        the menu doesn't). Without one to compare, the build date's verdict (info.current)."""
        if info.marker is None:
            return info.current
        name, size, crc = info.marker
        entry = self.dev.stat(name)
        if entry is None or entry.size != size:
            return False
        return zlib.crc32(saves.read_file(self.dev, name) or b"") == crc

    def _deliver_firmware(self, info: fw.Firmware) -> None:
        """The newest .efu at the card root, for the menu to install (efu-install.txt)."""
        name = info.release.file
        data = info.path.read_bytes()
        entry = self.dev.stat(name)
        if entry is not None and entry.size == len(data):
            return
        self._progress("Copying firmware", f"{name} ({len(data) / (1 << 20):.1f} MB)")
        policy = ops.WritePolicy((name,))
        sent = ops.upload(self.dev, BytesIO(data), name, policy=policy, overwrite=True, menu_check=False)
        self._progress("Checking firmware", name)
        said_at = self.clock()

        def tick(done: int, total: int) -> None:
            nonlocal said_at
            if self.clock() - said_at >= romsync.STATUS_EVERY:
                said_at = self.clock()
                self._progress("Checking firmware", f"{name} {done * 100 // total if total else 0}%")

        ops.verify(self.dev, sent.path, sent.sha256, progress=tick)
        self.firmware_record(name, True)
        self.log(f"firmware {info.release.version} copied to /{name}: install it from the cart's menu")

    def _remove_firmware_files(self, info: fw.Firmware) -> None:
        """The cart runs the newest: the files the box delivered are done with. Never others:
        a firmware file the owner put there is theirs."""
        for name in sorted(info.delivered):
            if self.dev.stat(name) is not None:
                self.dev.delete(ops.WritePolicy((name,)).check(name))
                self.log(f"removed /{name} (delivered by the box): the cart runs v{info.release.version}")
            self.firmware_record(name, False)

    def _sync_run(self, code: int, seen: int) -> None:
        """seen: the number of the plan the app showed. The box may have planned again as A was
        pressed: never run a plan the user didn't see."""
        if seen != self.plan_number:
            raise ValueError("Plan changed: check it")
        if self._library_offline:
            raise ValueError("Library offline")
        mode = self._mode(code)
        plan = self.plan if self.plan and self.plan.mode == mode else None
        self.plan = None  # a plan runs once; the next sync plans afresh
        if plan is None:
            raise ValueError("Plan first")
        self.log(f"sync ({mode}): {plan.summary()}")
        started = self.clock()
        total = len(plan.moves) + len(plan.add) + len(plan.replace) + plan.removals
        try:
            summary = romsync.run_plan(
                self.dev,
                plan,
                self.library,
                self.dest,
                self.manifest,
                policy=self.policy,
                progress=self._progress,
                copying=self._copying if self.copy_panel else None,
                log=self.log,
                stop=self._stop_requested,
            )
        except (EverDriveError, OSError) as e:
            self.log(f"sync failed after {plan.done} of {total}, on {plan.doing}: {e}")
            self._error(f"Failed: {plan.done}/{total} done", plan.doing)
            return
        if plan.stopped is not None:
            self.log(f"sync stopped after {plan.stopped} of {total}: {summary}")
            self.sync_result = f"Stopped: {plan.stopped}/{total} done"
            self.send(SYNC_DONE, self._text(self.sync_result))
            return
        self.log(f"sync done: {summary}")
        self.sync_result = f"Sync done in {_duration(self.clock() - started)}"
        self.note = _what_changed(plan)
        self.send(SYNC_DONE, self._text(self.sync_result))

    def _copying(self, n: int, total: int, name: str, stage: int, done: int, size: int) -> None:
        """COPY: n:u16 total:u16 stage:u8 done:u32 size:u32 name:str (little-endian, like PLAN)."""
        now = self.clock()
        last_at, last = self._copy_sent
        edge = last != (n, stage) or done >= size
        if not edge and now - last_at < COPY_EVERY:
            return
        self._copy_sent = (now, (n, stage))
        body = struct.pack("<HHBII", min(n, 0xFFFF), min(total, 0xFFFF), stage, done, size)
        self.send(COPY, body + text(name, 255))

    # relaunch
    def _wait_for_menu(self) -> None:
        p = self.pending
        now = self.clock()
        if now < p.next_ping:
            self.sleep(WAIT_STEP)
            return
        if p.reset:
            p.reset = False
            if self.dev.reset_to_menu():
                self._start(p)
                return
            self.log("reset to the menu got no answer; pinging instead")
            p.next_ping = self.clock() + MENU_FIRST_PING
            return
        p.next_ping = now + MENU_PING_EVERY
        if self.dev.menu_ping(timeout=0.3):
            if p.answered_at is None:
                p.answered_at = now
                p.next_ping = now + MENU_SETTLE
                p.deadline = max(p.deadline, now + MENU_SETTLE + 1)
                return
            self._start(p)
        elif now > p.deadline:
            self.pending = None
            self.log(f"gave up: the menu never came back to start /{p.cart_path}")

    def _start(self, p: PendingLaunch) -> None:
        self.pending = None
        self.rx = Deframer()
        self.greeted = False  # a new copy: it says hello
        ops.launch(self.dev, p.cart_path)
        self.launched.append(p.cart_path)
        self.launched_at = self.clock()
        self.log(f"started /{p.cart_path} {self.clock() - p.started:.2f} s after RESTART")
