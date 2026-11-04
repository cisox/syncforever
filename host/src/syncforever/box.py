"""The box service (`sfe serve`): every connected cart gets a session (one thread each): set it
up, watch it, answer its app, until it's gone; a cart that comes back gets a new one. Each
session uses its console's folders (Config.console: [nes], [genesis]).

A menu that may be in use only ever gets stats (F_FINFO) from the box: a ping is read as the
menu's own MCU reply (Error:40), and opening a file closes the menu's (Error:09; CLAUDE.md).
So: at connect, set the clock. Then stat recent.bin every few seconds (StartWatch): a new date
means the menu just started a game or our app, and once that has settled the menu isn't
running, so the box can open files: back up the recent games' changed saves, install or update
the Sync Forever app (apps.py). Meanwhile answer the app (appserver.py), which backs up every
save when it opens. A session lasts until the cart is gone for longer than a program switch
(the console is off or the cable is out).
Each step that fails is logged and skipped; the box keeps going.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import PurePosixPath

from . import apps, ops
from .appserver import AppServer
from .carts import FXPAK, PROFILES, X7
from .config import Config
from .firmware import FirmwareStore
from .n8pro import N8Pro
from .protocol import EverDriveError, LinkError
from .romsync import Manifest
from .saves import SaveStore, backup_flat, backup_recent, recent_path, summary

# The cart drops off USB for ~0.2 s when the menu starts a program or ours exits, if nothing
# holds the port (hardware, 2026-11-01). Longer than this is a power-off or an unplug.
CART_GONE = 3.0
WAIT_EVERY = 1.0
RECENT_GAMES = 5  # games whose saves are checked after a start (the app checks them all)
STAT_EVERY = 2.0  # recent.bin stats at the menu: 3-4 ms each, safe while the menu works
START_SETTLE = 5.0  # after recent.bin's date changes, before opening files: the start is over
FIRMWARE_WAIT = 10.0  # the app session waits this long for a firmware check still running
FXPAK_POLL = 2.0  # seconds between INFOs (which ROM runs): cheap, safe at the menu or in a game
FXPAK_APP_CHECK = 1.0  # while serving the app: seconds between INFOs (is it still running?)
FXPAK_SETTLE = 10.0  # after the running ROM changes, before backing up: its save has landed
FXPAK_BACKUP_EVERY = 600.0  # and a backup this often regardless (saves change mid-game)
X7_READ_TIMEOUT = 0.2  # the X7's port: short reads, the frame reader keeps its own deadlines


class CartGone(Exception):
    pass


class NoAnswer(Exception):
    """A port is there but no cart answers: usually its console is off (a cart shows up on USB
    from the cable's power alone). Normal with a hub and one console on at a time, so it's said
    once per port and retried quietly."""


def policy_for(dest: str) -> ops.WritePolicy:
    """Sync and launch copies may write anywhere under dest (the root: anywhere but /EDN8,
    which romsync.guard refuses on its own)."""
    d = ops.norm(dest)
    return ops.WritePolicy(anywhere=True) if not d else ops.WritePolicy((d,))


def serial_carts(port: str | None):
    """open_cart for real hardware: block until the cart's serial port appears, then open it."""
    from .transport import SerialTransport, find_ports

    def open_cart(log):
        said = False
        while True:
            if port:
                devices = [port] if _exists(port) else []
            else:
                devices = [p.device for p in find_ports()]
            if len(devices) == 1:
                try:
                    return SerialTransport(devices[0])
                except OSError as e:
                    log(f"can't open {devices[0]}: {e}")
            elif len(devices) > 1 and not said:
                log(f"several EverDrives ({', '.join(devices)}): set [box] port")
                said = True
            elif not said:
                log("waiting for an EverDrive on USB")
                said = True
            time.sleep(WAIT_EVERY)

    return open_cart


def serial_devices() -> list[str]:
    """Every cart's serial port now connected (PRO carts, X7s, FXPAKs)."""
    from . import fxpak, x7
    from .transport import find_ports

    return [p.device for p in (*find_ports(), *x7.find_ports(), *fxpak.find_ports())]


def open_serial(device: str):
    """The PRO carts' port, or an X7's: marked `x7`, named by its FTDI chip's serial number."""
    from . import x7
    from .transport import SerialTransport

    for p in x7.find_ports():
        if p.device == device:
            t = SerialTransport(device, timeout=X7_READ_TIMEOUT)
            t.x7 = True
            t.label = X7.short
            t.usb_serial = safe_id(p.serial_number or device.rsplit("-", 1)[-1])
            return t
    from . import fxpak

    for p in fxpak.find_ports():
        if p.device == device:
            from .transport import open_port

            t = open_port(device, timeout=0.2, write_timeout=fxpak.WRITE_TIMEOUT)
            t.fxpak = True
            t.label = FXPAK.short
            # Every FXPAK reports DEMO00000000: one per box until they're told apart otherwise.
            t.usb_serial = safe_id(p.serial_number or "FXPAK")
            return t
    t = SerialTransport(device)
    t.label, t.usb_id = _pro_label(device)
    return t


def _pro_label(device: str) -> str:
    """Which PRO cart a port is, from its USB ID, before it has answered anything (its console
    may be off). Firmware before the Krikzz IDs: just "EverDrive"."""
    from .transport import find_ports

    port = next((p for p in find_ports() if p.device == device), None)
    usb = f"{port.vid:04x}:{port.pid:04x}" if port is not None and port.vid is not None else None
    pid = port.pid if port is not None else None
    return next((c.short for c in PROFILES.values() if c.device == pid), "EverDrive"), usb


def safe_id(raw: str) -> str:
    """A device-reported name made safe for a file name (cart-<id>.json): letters, digits, _ and
    - only, else a hash of it. A USB serial number is whatever the device says."""
    import hashlib
    import re

    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw or ""):
        return raw
    return "h" + hashlib.sha256((raw or "").encode()).hexdigest()[:16]


def _exists(path):
    import os

    return os.path.exists(path)


class StartWatch:
    """Stat recent.bin every STAT_EVERY s; START_SETTLE s after its date changes (the menu
    started something) and stays put, run `work`: the menu isn't running then. The first stat
    only records the date. A start within the settle time restarts it. The remaining risk: the
    player resets back to the menu and is starting the next game exactly when `work` runs."""

    def __init__(self, dev, work, log, clock=time.monotonic):
        self.dev = dev
        self.work = work
        self.log = log
        self.clock = clock
        self.date = None
        self.seen = False  # the first stat sets self.date
        self.started_at = None
        self.next_stat = clock()

    def step(self) -> None:
        now = self.clock()
        if self.started_at is not None and now >= self.started_at + START_SETTLE:
            self.started_at = None
            self.work()
            return
        if now < self.next_stat:
            return
        self.next_stat = now + STAT_EVERY
        entry = self.dev.stat(recent_path(self.dev))
        date = entry.mtime if entry else None
        if not self.seen:
            self.seen, self.date = True, date
        elif date != self.date:
            self.date = date
            self.started_at = now
            self.log("the menu started a game or the app")


class Box:
    """The box: one CartSession per connected cart. `run` serves one cart (open_cart: the
    tests, --sim, --port); `serve_all` finds every EverDrive and serves each in its own thread."""

    def __init__(
        self,
        cfg: Config,
        open_cart: Callable | None = None,
        *,
        log: Callable[[str], None] = print,
        now: Callable[[], datetime] = datetime.now,
        sleep: Callable[[float], None] = time.sleep,
        tag: bool = False,
        firmware: FirmwareStore | None = None,
    ):
        self.cfg = cfg
        if firmware is None and cfg.firmware:
            firmware = FirmwareStore(cfg.state.expanduser() / "firmware", log=log)
        self.firmware = firmware  # None: no firmware checks
        self.open_cart = open_cart
        self.log = log
        self.now = now
        self.sleep = sleep
        self.tag = tag  # prefix log lines with the cart ("[Mega PRO 3C85]")
        self.threads: list[threading.Thread] = []  # every session thread serve_all started
        self.not_answering: set[str] = set()  # ports already logged as silent (NoAnswer)

    def run(self) -> None:
        """Forever (until KeyboardInterrupt): one cart session after another."""
        while True:
            t = self.open_cart(self.log)
            self._serve(t)

    def serve_all(self, find_devices: Callable[[], list[str]], open_device: Callable[[str], object]) -> None:
        """Forever: a thread per EverDrive port; a port that comes back gets a new session.
        Carts are told apart by what they report (profile, serial), not by port names."""
        active: dict[str, threading.Thread] = {}
        said_waiting = False
        while True:
            for device in find_devices():
                thread = active.get(device)
                if thread is None or not thread.is_alive():
                    thread = threading.Thread(target=self._serve_device, args=(device, open_device), daemon=True)
                    active[device] = thread
                    self.threads.append(thread)
                    thread.start()
            active = {d: t for d, t in active.items() if t.is_alive()}
            if not active and not said_waiting:
                self.log("waiting for an EverDrive on USB")
            said_waiting = not active
            self.sleep(WAIT_EVERY)

    def _serve_device(self, device, open_device) -> None:
        try:
            t = open_device(device)
        except OSError as e:
            self.log(f"can't open {device}: {e}")
            return
        self._serve(t)

    def _serve(self, t) -> None:
        try:
            if getattr(t, "x7", False):
                self.x7_session(t)
            elif getattr(t, "fxpak", False):
                self.fxpak_session(t)
            else:
                self.session(t)
        except CartGone:
            self.log(f"{_who(t)} cart gone")
        except NoAnswer as e:
            port = _port_name(t)
            if port not in self.not_answering:
                self.not_answering.add(port)
                usb = f", USB {t.usb_id}" if getattr(t, "usb_id", None) else ""
                self.log(f"{_who(t)} on USB but not answering (console off?), waiting for it ({e}{usb})")
            self.sleep(WAIT_EVERY)
        except (LinkError, OSError, *_serial_errors()) as e:  # unplugged mid-call: termios.error (FXPAK)
            self.log(f"{_who(t)} lost the cart: {e}")
            self.sleep(WAIT_EVERY)
        finally:
            t.close()

    def cart(self, dev) -> CartSession:
        return CartSession(self, dev)

    def session(self, t) -> None:
        dev = N8Pro(t)
        try:
            dev.connect()
            info = dev.sys_info()
        except LinkError as e:
            raise NoAnswer(str(e)) from e
        self.not_answering.discard(_port_name(t))
        cart = self.cart(dev)
        cart.serial = info.serial
        cart.built = info.built
        cart.log(f"cart connected: {dev.profile.name} (serial {info.serial}) on {_short_port(t)}")
        cart.setup()
        cart.report_app()
        server = cart.server(info.serial)
        watch = StartWatch(dev, cart.after_start, cart.log)
        cart.log("ready")
        while True:
            try:
                server.poll(timeout=0.1)
                if server.app_active() or server.just_launched(START_SETTLE + STAT_EVERY + 1):
                    # The app pings every second: its frames would land inside a cart reply
                    # (stat read 'S' = 0x53 as the status, hardware 2026-11-02). While it's open
                    # the box only answers it; its own backup covers the recent games. After the
                    # box relaunched it, the recent list's change is no game start: if the launch
                    # failed (Error:40), the menu is up (hardware 2026-11-02).
                    watch.started_at = None
                elif not server.pending:  # a relaunch talks to the menu: nothing else may
                    watch.step()
            except (OSError, LinkError) + _serial_errors() as e:
                reopen = getattr(t, "reopen", None)
                if reopen is None:
                    raise
                try:
                    reopen(wait=CART_GONE)
                except LinkError:
                    raise CartGone from e
                if not _same_cart(dev, cart.serial):
                    # Carts are told apart by what they report, not the port's name: a cart
                    # swapped within CART_GONE gets the same device name (seen 2026-11-02).
                    cart.log("a different cart is on this port: starting over")
                    raise CartGone from e
                cart.log(f"USB blinked ({e}); port open again")
                server.rx = type(server.rx)()

    def x7_session(self, t) -> None:
        """An EverDrive-64 X7: the box only listens. Its menu has no file commands, and pinging
        it while someone uses it is untested, so nothing goes to the cart until our app says
        hello; the app then serves the card's files to the session (x7.py, docs/n64-plan.md)."""
        server = self.x7_server(t)
        while True:
            server.poll(timeout=0.5)

    def fxpak_session(self, t) -> None:
        """An FXPAK PRO (SNES): the cart's MCU serves its SD card at any time. The box installs
        the Sync Forever app, backs up saves now, when the running ROM changes and every so often,
        and serves the app while it runs (fxapp.py: its link goes through cart memory)."""
        from .fxpak import FxPak

        dev = FxPak(t)
        try:
            info = dev.info()
        except LinkError as e:
            raise NoAnswer(str(e)) from e
        self.not_answering.discard(_port_name(t))
        cart = self.cart(dev)
        cart.serial = getattr(t, "usb_serial", "FXPAK")
        cart.log(f"cart connected: {info.device} on {_short_port(t)}, firmware {info.version}; running {info.rom}")
        cart.fx_version = info.version
        cart.setup()
        app = apps.cart_path(self.cfg.app_path, dev.profile)

        def app_runs(rom: str) -> bool:
            return bool(app) and ops.norm(rom).casefold() == app.casefold()

        def install_at_menu(at_menu: bool) -> None:
            # Only at the menu: in a game, the cart's autosave can meet the upload (fxapp.py).
            # In our app, AppServer's HELLO checks it, through FxApp's gate.
            if at_menu and not cart.app_checked:
                cart.update_app()

        install_at_menu(info.at_menu)
        clock = time.monotonic
        rom, changed_at, backed_at = info.rom, None, None
        while True:
            if app_runs(rom):
                self.fxpak_app(dev, cart, app_runs)
                rom, changed_at = dev.info().rom, clock()  # the app backed up as it opened
                cart.log(f"now running {rom}")
                backed_at = clock()
                continue
            now = clock()
            if (
                backed_at is None
                or (changed_at is not None and now - changed_at >= FXPAK_SETTLE)
                or (now - backed_at >= FXPAK_BACKUP_EVERY)
            ):
                why = "connected" if backed_at is None else "game changed" if changed_at is not None else "timer"
                cart.backup_flat_saves(why)
                backed_at, changed_at = clock(), None
            self.sleep(FXPAK_POLL)
            now_info = dev.info()
            running = now_info.rom
            if running != rom:
                cart.log(f"now running {running}")
                rom, changed_at = running, clock()
                install_at_menu(now_info.at_menu)

    def fxpak_app(self, dev, cart, app_runs) -> None:
        """Serve the SNES app until INFO says another ROM runs (the user pressed reset)."""
        from .fxapp import FxApp

        cart.log("Sync Forever app running")
        app = apps.cart_path(self.cfg.app_path, dev.profile) or ""
        save_file = f"sd2snes/saves/{PurePosixPath(app).stem}.srm" if app else ""
        server = cart.server(cart.serial, dev=FxApp(dev, clock=time.monotonic, sleep=self.sleep, save_file=save_file))
        server.firmware_line = cart.sd2snes_line
        clock = time.monotonic
        check_at = clock() + FXPAK_APP_CHECK
        while True:
            server.poll(timeout=0.1)
            if clock() >= check_at:
                if not app_runs(dev.info().rom):
                    return
                check_at = clock() + FXPAK_APP_CHECK

    def x7_server(self, t) -> AppServer:
        from .x7 import X7App

        dev = X7App(t)
        cart = self.cart(dev)
        cart.serial = getattr(t, "usb_serial", "X7")
        cart.log(
            f"cart connected: {dev.profile.name} (USB serial {cart.serial}) on {_short_port(t)}; waiting for the app"
        )
        cart.setup()
        server = cart.server(cart.serial)
        server.on_connect = lambda: cart.log(f"emulators: {' '.join(dev.scan_emulators()) or 'none'}")
        server.copy_panel = True
        server.firmware_line = cart.os_line
        return server


class CartSession:
    """One connected cart: its console's folders (Config.console), its app, its saves."""

    def __init__(self, box: Box, dev):
        self.box = box
        self.cfg = box.cfg
        self.dev = dev
        self.serial = ""
        self.built = None  # SYS_INF build date: which firmware it runs (firmware.py)
        self.fx_version = ""  # the FXPAK's firmware version (INFO)
        self._fw_check: threading.Thread | None = None  # this connection's firmware check
        c = box.cfg.console(dev.profile.key)
        self.library = c.library
        self.sync_mode = c.sync_mode
        self.msu_on = c.msu
        self.first_look = c.first_look
        self.policy = policy_for(c.dest)
        self.dest = self.policy.check(c.dest)
        self.store = SaveStore(c.saves, states=c.states) if c.saves or c.states else None
        self.app_checked = False  # the app file is checked once per connection
        self._said_no_app = False

    def log(self, msg: str) -> None:
        if self.box.tag:
            msg = f"[{self.dev.profile.short} {self.serial[-4:]}] {msg}"
        self.box.log(msg)

    def server(self, serial, dev=None) -> AppServer:
        """dev: what the server talks through, if not the cart itself (the SNES app's link)."""
        manifest = Manifest(self.cfg.state.expanduser() / f"cart-{serial}.json")
        return AppServer(
            dev or self.dev,
            self.library,
            dest=self.dest,
            policy=self.policy,
            manifest=manifest,
            saves=self.store,
            sync_mode=self.sync_mode,
            msu_on=self.msu_on,
            first_look=self.first_look,
            update_app=self.update_app,
            firmware=self.firmware,
            firmware_record=self._firmware_record,
            app_path=apps.cart_path(self.cfg.app_path, self.dev.profile) or "",
            log=self.log,
        )

    def setup(self) -> None:
        """At connect: no pings, no file opens (the menu may be in use)."""
        if self.library is None:
            self.log(f"no ROM library for {self.dev.profile.key}: set [{self.dev.profile.key}] library")
        elif not self.library.is_dir():
            self.log(f"library folder missing: {self.library} (not mounted?)")
        if self.cfg.set_clock and hasattr(self.dev, "rtc_set"):  # the X7 has no clock to set
            self._step("clock", self._set_clock)
        if self.box.firmware and self.dev.profile.firmware_feed:
            self._fw_check = threading.Thread(target=self._refresh_feed, daemon=True)
            self._fw_check.start()
        if self.box.firmware and self.dev.profile.firmware_site:
            # Network only, never the cart: in the background, the app uses what's there.
            self._fw_check = threading.Thread(target=self._refresh_firmware, daemon=True)
            self._fw_check.start()

    def report_app(self) -> None:
        """At connect: say if our app isn't on the card. Only a stat, which is safe while someone
        uses the menu."""
        if not self.cfg.app_path or not self.dev.profile.app_install or self.dev.profile.app_ext is None:
            return
        path = apps.cart_path(self.cfg.app_path, self.dev.profile)
        try:
            if self.dev.stat(path) is not None:
                return
        except (EverDriveError, OSError) as e:
            self.log(f"couldn't check for the app: {e}")
            return
        self.log(f"no Sync Forever app at /{path} yet: with the console at its menu, run: sudo sfe install")

    def _refresh_firmware(self) -> None:
        try:
            self.box.firmware.refresh(self.dev.profile)
        except Exception as e:  # noqa: BLE001 - a background check must not end the box
            self.log(f"firmware check failed: {e}")

    def _refresh_feed(self) -> None:
        try:
            self.box.firmware.refresh_versions(self.dev.profile.firmware_feed)
        except Exception as e:  # noqa: BLE001 - a background check must not end the box
            self.log(f"firmware check failed: {e}")

    def os_line(self) -> tuple[str, int]:
        """The X7's firmware line: the OS version in its menu file (read through our app), green
        if it's Krikzz's newest, red if there's a newer one, plain if the newest isn't known."""
        from .appserver import FW_NONE
        from .firmware import os_version
        from .saves import read_file

        menu = read_file(self.dev, f"{self.dev.profile.system_dir}/OS64.v64")
        version = os_version(menu) if menu else None
        if version is None:
            self.log("couldn't read the X7's OS version")
            return "", FW_NONE
        return self._version_line("OS", version)

    def sd2snes_line(self) -> tuple[str, int]:
        """The FXPAK's firmware line: the version INFO reports against the newest sd2snes tag."""
        return self._version_line("firmware", self.fx_version)

    def _version_line(self, what: str, version: str) -> tuple[str, int]:
        """("v<version>", green if it's the feed's newest, red if there's newer, plain if unknown)."""
        from .appserver import FW_CURRENT, FW_UNKNOWN, FW_WAITING
        from .firmware import version_key

        if self._fw_check is not None:
            self._fw_check.join(FIRMWARE_WAIT)
        feed = self.dev.profile.firmware_feed
        newest = self.box.firmware.newest_version(feed) if self.box.firmware else None
        try:
            behind = newest is not None and version_key(version) < version_key(newest)
        except ValueError:  # not numbers: say what it runs, uncoloured
            newest, behind = None, False
        state = FW_UNKNOWN if newest is None else FW_WAITING if behind else FW_CURRENT
        self.log(f"{what} v{version}" + (f", newest v{newest}" if newest else ""))
        return f"v{version}", state

    def firmware(self):
        """For the app server: the newest firmware and whether this cart runs it."""
        if not self.box.firmware:
            return None
        if self._fw_check is not None:
            self._fw_check.join(FIRMWARE_WAIT)  # the app may connect before the check is done
        return self.box.firmware.for_cart(self.dev.profile, self.built, self.serial)

    def _firmware_record(self, file: str, present: bool) -> None:
        if self.box.firmware:
            self.box.firmware.record(self.serial, file, present)

    def after_start(self) -> None:
        """A game or our app runs: the menu doesn't, so files can be read. Reads only: writing
        during someone else's game was never tried (docs/test-results.md). The app's file is
        updated when the app itself says hello (update_app), and first installed at the menu
        (sfe install)."""
        if self.store:
            self._step("save check", self._backup_saves)

    def _step(self, what, fn):
        try:
            fn()
        except (EverDriveError, OSError, ValueError) as e:
            self.log(f"{what} failed: {e}")

    def update_app(self) -> bool:
        """The app said hello: check its file once per connection (it runs, so the menu
        doesn't). True if the file changed: the running copy is stale."""
        if not self.cfg.app_path or self.app_checked or not self.dev.profile.app_install or not self._has_app():
            return False
        self.app_checked = True
        try:
            return self._install_app() != "current"
        except (EverDriveError, OSError, ValueError) as e:
            self.log(f"Sync Forever app failed: {e}")
            return False

    def _has_app(self) -> bool:
        """Carts without an app of ours (CartProfile.app_ext) are skipped, logged once."""
        if self.dev.profile.app_ext is not None:
            return True
        if not self._said_no_app:
            self._said_no_app = True
            self.log(f"no Sync Forever app for the {self.dev.profile.name} yet")
        return False

    def _install_app(self) -> str:
        r = apps.ensure_app(self.dev, apps.cart_path(self.cfg.app_path, self.dev.profile), check_menu=False)
        if r.status == "current":
            self.log(f"app is up to date at /{r.path}")
        else:
            self.log(f"app {r.status} at /{r.path} ({r.font} font)")
        return r.status

    def _set_clock(self):
        self.dev.rtc_set(self.box.now())
        self.log(f"set the cart clock to {self.dev.rtc_get()}")

    def backup_flat_saves(self, why: str) -> None:
        """Every save file on a flat-save cart (the FXPAK), each new version kept."""
        if self.store is None:
            return
        if self.store.missing():
            self.log(f"save folder missing: {self.store.missing()} (not mounted?); backup skipped")
            return
        try:
            results = backup_flat(self.dev, self.store)
        except (EverDriveError, OSError) as e:
            self.log(f"save backup failed: {e}")
            return
        self.log(f"saves ({why}): {summary(results)}")

    def _backup_saves(self):
        if self.store.missing():
            self.log(f"save folder missing: {self.store.missing()} (not mounted?); save check skipped")
            return
        results = backup_recent(self.dev, self.store, RECENT_GAMES, check_menu=False)
        self.log(f"recent games: {summary(results)}")


def _same_cart(dev, serial: str) -> bool:
    """After a reopen: still the cart this session set up? (SYS_INF only: no menu talk.)"""
    try:
        return dev.sys_info().serial == serial
    except (EverDriveError, OSError) + _serial_errors():
        return False


def _port_name(t) -> str:
    return str(getattr(t, "device", None) or getattr(t, "port", None) or "cart")


def _who(t) -> str:
    """A log line's tag before the cart has said who it is: from its USB ID and port."""
    port = _short_port(t)
    label = getattr(t, "label", None)
    return f"[{label} {port}]" if label else f"[{port}]"


def _short_port(t) -> str:
    return _port_name(t).rsplit("/", 1)[-1]


def _serial_errors() -> tuple:
    try:
        import termios

        import serial

        return (termios.error, serial.SerialException)
    except ImportError:  # no termios off POSIX
        import serial

        return (serial.SerialException,)
