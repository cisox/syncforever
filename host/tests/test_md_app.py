"""The Sync Forever Genesis app end to end: the app (md/) in mdemu, started by the fake Mega
EverDrive's menu, talking to the real box code (appserver.py) through EverDrive, as on hardware.

Builds the ROM with `make -C md` first; skipped when m68k-elf-gcc isn't installed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from syncforever import appserver, ops, romsync
from syncforever.carts import MEGA_PRO
from syncforever.firmware import Firmware, Release
from syncforever.mdemu import FakeMegaEverDrive, MegaDrive
from syncforever.n8pro import EverDrive
from syncforever.sim import FakeN8Pro

MD_DIR = Path(__file__).resolve().parents[2] / "md"
ROM = MD_DIR / "build" / "sync-forever.md"
APP_PATH = "syncforever-test/Sync Forever.md"
DEST = "syncforever-test/sync"

# md/src/main.c
ROW_TITLE, ROW_STATUS, ROW_MODE, ROW_ADD, ROW_REPLACE, ROW_REMOVE = 2, 6, 9, 11, 12, 13
ROW_WARN, ROW_SAVES, ROW_FIRMWARE, ROW_HELP = 15, 18, 19, 25
LABEL = "Status:"
C_OK, C_WAIT, C_BAD, C_LABEL = 0x02C2, 0x02EE, 0x066E, 0x0AAA
COL_STATUS = 10


@pytest.fixture(scope="session")
def rom() -> bytes:
    if not shutil.which("m68k-elf-gcc"):
        pytest.skip("m68k-elf-gcc not installed (brew install m68k-elf-gcc)")
    subprocess.run(["make", "-C", str(MD_DIR), "-s"], check=True, capture_output=True)
    return ROM.read_bytes()


def cart_rom(seed: int, size: int = 0x8000) -> bytes:
    return bytes((seed + i * 7) & 0xFF for i in range(size))


LIBRARY = {
    "Sonic.md": cart_rom(1),
    "Streets.bin": cart_rom(2),
    "Puzzle/Columns.gg": cart_rom(3),
    "notes.txt": b"not a ROM",
}
LIBRARY_ROMS = sorted(rel for rel in LIBRARY if not rel.endswith(".txt"))


class Session:
    """Console + Mega EverDrive + box, stepped one frame at a time."""

    def __init__(self, rom, tmp_path, library_files=LIBRARY):
        self.sd = tmp_path / "sd"
        (self.sd / "syncforever-test").mkdir(parents=True)
        (self.sd / APP_PATH).write_bytes(rom)
        self.lib = tmp_path / "library"
        self.lib.mkdir()
        for rel, data in library_files.items():
            (self.lib / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.lib / rel).write_bytes(data)
        self.manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
        self.sim = FakeN8Pro(self.sd, profile=MEGA_PRO)
        self.sim.on_start = self._boot
        self.md: MegaDrive | None = None
        self.dev = EverDrive(self.sim)
        self.dev.connect()
        self.log: list[str] = []
        self.now = 0.0
        self.server = self.new_server()
        self.serving = True
        ops.launch(self.dev, APP_PATH)  # the user picks Sync Forever in the menu
        assert self.md is not None

    def new_server(self):
        return appserver.AppServer(
            self.dev,
            self.lib,
            dest=DEST,
            policy=ops.WritePolicy(),
            manifest=self.manifest,
            saves=None,
            app_path=APP_PATH,
            log=self.log.append,
            clock=lambda: self.now,
            sleep=lambda seconds: None,
        )

    def _boot(self, rel, data):
        if rel != APP_PATH:
            return None
        self.md = MegaDrive(data, cart=FakeMegaEverDrive())
        return self.md

    def frames(self, n=1, buttons=0):
        for _ in range(n):
            if not self.md.exited:
                self.md.run_frames(1, buttons)
            if self.serving:
                self.server.poll(timeout=0)
            self.now += 1 / 60

    def press(self, button, after=20):
        self.frames(2, button)
        self.frames(after)

    def text(self, row):
        """A row's text; the status row without its label, the help row without the version."""
        t = self.md.text(row).strip()
        if row == ROW_STATUS and t.startswith(LABEL):
            t = t[len(LABEL) :].strip()
        if row == ROW_HELP and t.endswith(f"v{appserver.PROTO_VERSION}"):
            t = t[:-2].strip()
        return t

    def status_ink(self):
        return self.md.ink(ROW_STATUS, COL_STATUS)


def counts(s):
    return [s.text(r).split()[-1] for r in (ROW_ADD, ROW_REPLACE, ROW_REMOVE)]


@pytest.fixture
def s(rom, tmp_path):
    session = Session(rom, tmp_path)
    session.frames(60)
    return session


# --- connecting and the plan ---------------------------------------------------------


def test_opens_on_the_default_plan(s):
    assert s.text(ROW_STATUS) == "Ready to sync"
    assert s.status_ink() == C_OK
    assert s.md.ink(ROW_STATUS, 2) == C_LABEL  # "Status:"
    assert s.text(ROW_MODE) == "Mode: <Add only>  MSU off"
    assert counts(s) == [str(len(LIBRARY_ROMS)), "0", "0"]
    assert s.text(ROW_HELP) == "A:Sync  Left/Right:Mode  B:Exit"
    assert s.text(ROW_SAVES) == "Saves: backup off"
    assert s.md.text(ROW_HELP)[36:38] == f"v{appserver.PROTO_VERSION}"
    assert s.text(ROW_TITLE) == ""  # the title is art tiles, not text
    assert all(t >= 0x200 for t in s.md.tiles(ROW_TITLE)[8:32] if t)


def test_sync_copies_the_library_then_b_returns_to_the_menu(s):
    s.press("A", after=60)
    assert s.text(ROW_STATUS).startswith("Sync done in ")  # stays up while it checks again
    assert s.text(ROW_HELP) == "Left/Right:Mode  B:Exit"  # nothing left to do
    assert s.text(ROW_ADD).split()[-1] == "0"
    for rel in LIBRARY_ROMS:
        assert (s.sd / DEST / rel).read_bytes() == LIBRARY[rel], rel
    assert not (s.sd / DEST / "notes.txt").exists()
    s.press("B", after=60)
    assert s.md.exited
    assert s.sim.running is None  # the menu is back


def test_second_visit_has_nothing_to_do(rom, tmp_path):
    s = Session(rom, tmp_path)
    s.frames(60)
    s.press("A", after=60)
    s.press("B", after=60)
    ops.launch(s.dev, APP_PATH)
    s.frames(60)
    assert counts(s) == ["0", "0", "0"]
    assert s.text(ROW_STATUS) == "Nothing to do"
    assert s.text(ROW_HELP) == "Left/Right:Mode  B:Exit"


def test_left_right_change_the_mode(s):
    s.press("RIGHT", after=30)
    assert s.text(ROW_MODE) == "Mode: <Update>  MSU off"
    s.press("LEFT", after=30)
    s.press("LEFT", after=30)
    assert s.text(ROW_MODE) == "Mode: <Mirror STRICT>  MSU off"
    assert s.text(ROW_WARN) == "Removes ROMs not in library!"
    s.press("RIGHT", after=30)
    assert s.text(ROW_MODE) == "Mode: <Add only>  MSU off"
    assert s.text(ROW_WARN) == ""


def test_b_returns_to_the_menu_without_syncing(s):
    s.press("B", after=60)
    assert s.md.exited
    assert not (s.sd / DEST).exists()


# --- losing the box --------------------------------------------------------------------


def test_connects_when_the_box_starts_late(rom, tmp_path):
    s = Session(rom, tmp_path)
    s.serving = False
    s.frames(200)
    assert s.text(ROW_STATUS) == "Connecting..."
    assert s.status_ink() == C_WAIT
    s.frames(150)  # ~5 s with nobody answering
    assert s.text(ROW_STATUS) == "Disconnected"
    assert s.status_ink() == C_BAD
    assert s.text(ROW_HELP) == "B:Exit"
    s.serving = True
    s.frames(150)
    assert s.text(ROW_STATUS) == "Ready to sync"


def test_idle_app_notices_the_box_going_away_and_coming_back(s):
    s.frames(300)  # idle: the heartbeat keeps it connected
    assert s.text(ROW_STATUS) == "Ready to sync"
    s.serving = False
    s.frames(250)
    assert s.text(ROW_STATUS) == "Disconnected"
    assert s.status_ink() == C_BAD
    assert s.text(ROW_ADD) == ""  # the plan is gone: it may be stale
    assert s.text(ROW_HELP) == "B:Exit"
    s.serving = True
    s.frames(200)
    assert s.text(ROW_STATUS) == "Ready to sync"
    assert counts(s) == [str(len(LIBRARY_ROMS)), "0", "0"]


def test_a_restarted_box_gets_a_fresh_hello(s):
    s.server = s.new_server()
    s.log.clear()
    s.frames(150)
    assert "app connected" in s.log
    assert s.text(ROW_STATUS) == "Ready to sync"
    s.press("A", after=300)
    assert s.text(ROW_STATUS).startswith("Sync done in ")


def test_box_stops_answering_a_request(s):
    s.serving = False
    s.press("RIGHT", after=1850)
    assert s.text(ROW_STATUS) == "Disconnected"
    s.serving = True
    s.frames(200)
    assert s.text(ROW_STATUS) == "Ready to sync"


def test_the_box_knows_while_the_app_is_open(s):
    """While it is, the box sends the cart nothing but answers (box.py: its pings would land
    inside a cart reply)."""
    s.frames(300)
    assert s.server.app_active()
    s.press("B", after=60)
    s.now += appserver.APP_QUIET
    assert not s.server.app_active()


# --- the rest of the protocol ------------------------------------------------------------


def test_an_updated_app_restarts_itself(rom, tmp_path):
    s = Session(rom, tmp_path)
    updates = iter([True, False])  # the box replaced the file, then it's current
    s.server.update_app = lambda: next(updates)
    first = s.md
    s.frames(30)
    assert first.text(ROW_STATUS + 8).strip() == "Updating..."  # the exit screen, row 14
    s.frames(60)
    assert first.text(14).strip() == "Restarting..."  # it comes back: the box relaunches it
    for _ in range(400):  # the box resets into the menu and starts the new copy
        s.frames(1)
        if s.md is not first:
            break
    assert s.md is not first and first.exited
    assert any(line.startswith("started /") for line in s.log)
    s.frames(60)
    assert s.text(ROW_STATUS) == "Ready to sync"


def test_junk_in_the_fifo_is_skipped(s):
    s.md.host_send(b"*t*t\x00S" + appserver.frame(appserver.STATUS, appserver.text("Hello")))
    s.frames(5)
    assert s.text(ROW_STATUS) == "Hello"


def test_long_messages_fill_the_line(s):
    s.server.send(appserver.STATUS, appserver.text("x" * 40, appserver.TEXT_MAX))
    s.frames(5)
    assert s.text(ROW_STATUS) == "x" * 30


def test_library_offline_shows_an_error(rom, tmp_path):
    s = Session(rom, tmp_path)
    shutil.rmtree(s.lib)
    s.frames(60)
    assert s.text(ROW_STATUS) == "Library offline"
    assert s.status_ink() == C_BAD
    assert s.text(ROW_HELP) == "A:Check again  B:Exit"


def test_screenshot(s, tmp_path):
    out = tmp_path / "md.png"
    s.md.screenshot(out)
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


# --- firmware ----------------------------------------------------------------------------

ART_TILE = 0x200  # md/src/main.c
VERSION_CHARS = "0123456789v."  # md/tools/art.py


def firmware_line(s):
    """The firmware row, its version tiles (build/art.h ART_VERSION) read back as characters."""
    art_h = (MD_DIR / "build" / "art.h").read_text()
    first = ART_TILE + int(art_h.split("#define ART_VERSION ")[1].split()[0])
    out = []
    for t in s.md.tiles(ROW_FIRMWARE):
        k = t - first
        out.append(VERSION_CHARS[k] if 0 <= k < len(VERSION_CHARS) else chr(t) if 32 <= t < 127 else " ")
    return "".join(out).strip()


def mega_firmware(tmp_path, current):
    path = tmp_path / "download.efu"
    path.write_bytes(bytes(range(256)) * 40)
    release = Release("26.0925", "mega-pro-fw-v26.0925.efu")
    return Firmware(release, current, path)


@pytest.mark.parametrize(("current", "colour"), [(True, C_OK), (False, C_BAD)])
def test_the_firmware_line_is_green_or_red(rom, tmp_path, current, colour):
    s = Session(rom, tmp_path)
    s.server.firmware = lambda: mega_firmware(tmp_path, current)
    s.frames(90)
    assert s.text(ROW_STATUS) == "Ready to sync"
    assert firmware_line(s) == "Firmware: v26.0925"
    assert s.md.vdp.cram[2 * 16 + 14] == colour  # the accent palette's colour 14
    assert (s.sd / "mega-pro-fw-v26.0925.efu").exists() is not current


def test_no_firmware_line_without_a_check(s):
    assert s.text(ROW_FIRMWARE) == ""


def test_copies_show_the_file_name_under_the_status(s):
    long = "Phantasy Star IV - The End of the Millennium (USA).md"
    s.server.send(appserver.STATUS, appserver.text("2/5") + appserver.text(long, appserver.DETAIL_MAX))
    s.frames(10)
    assert s.text(ROW_STATUS) == "2/5"
    shown = s.md.text(ROW_STATUS + 1).strip()
    assert shown == long[:16] + "..." + long[-17:] and len(shown) == 36
    s.server.send(appserver.STATUS, appserver.text("Backing up saves"))
    s.frames(10)
    assert s.md.text(ROW_STATUS + 1).strip() == ""


ART_TILE, ART_BAR_FULL, ART_BAR_EMPTY = 0x200, 57, 58  # md/build/art.h


def bar(s, y):
    """A progress bar row: (filled cells, its percent), or (None, "") when there's none."""
    tiles = [t & 0x7FF for t in s.md.tiles(y)[2:32]]
    if ART_TILE + ART_BAR_EMPTY not in tiles and ART_TILE + ART_BAR_FULL not in tiles:
        return None, s.text(y)
    return tiles.count(ART_TILE + ART_BAR_FULL), s.md.text(y)[32:].strip()


def app_frames(s):
    """The frames the app sent that the box hasn't read."""
    return [kind for kind, _ in appserver.Deframer().feed(s.dev.app_read(0))]


def test_the_sync_screen_and_b_stops_after_the_file(s):
    s.serving = False  # the box takes the A, then only what this test sends
    s.press("A", after=5)
    assert app_frames(s) == [appserver.SYNC_RUN]
    name = "Castlevania - Bloodlines (USA) (MSU-MD).bin (653 MB)"
    s.server._progress("Copying 3 of 61", name, expect=148, left=2550)
    s.frames(2 * 60 + 5)  # two seconds of the app's own clock
    assert s.text(ROW_STATUS) == "Copying 3 of 61"
    assert s.text(ROW_MODE) == "Mode: <Add only>  MSU off"
    assert s.text(ROW_ADD) == "Castlevania - Bloodlines (USA)"
    assert s.text(ROW_REPLACE) == "(MSU-MD).bin (653 MB)"
    assert s.text(ROW_REMOVE) == ""
    assert s.text(ROW_WARN) == "This file: 0:02 / 2:28"
    assert s.text(ROW_SAVES) == "Time left: about 43 min"  # 2548 s, rounded up
    assert s.md.text(ROW_STATUS + 1).strip() == ""  # no detail line: the screen has the name
    assert bar(s, 16) == (0, "1%") and bar(s, ROW_FIRMWARE) == (0, "0%")
    s.server._progress("Copying 4 of 61", "big.md", expect=10, left=20)
    s.frames(5 * 60 + 5)  # half of this file, a little of the whole
    assert bar(s, 16) == (15, "50%")  # 30 cells
    assert bar(s, ROW_FIRMWARE)[1] == "31%"  # 7 s since the sync started, 15 left
    s.frames(20 * 60)
    assert bar(s, 16) == (29, "99%")  # past the estimate: held short of done
    assert s.text(ROW_HELP) == "B:Stop after this file"
    s.press("B", after=5)
    assert s.text(ROW_HELP) == "Stopping after this file..."
    assert app_frames(s) == []  # not while the file copies
    s.server._progress("Removing 1 of 2", "old.md", expect=0, left=2)
    s.frames(5)
    assert app_frames(s) == [appserver.STOP]  # with the next STATUS, between files
    assert s.text(ROW_WARN) == "" and s.text(ROW_SAVES) == "Time left: under a minute"
    assert bar(s, 16) == (None, "")  # a removal has no file bar
    s.server.send(appserver.SYNC_DONE, appserver.text("Stopped: 2 of 63 done"))
    s.frames(10)
    assert s.text(ROW_STATUS) == "Stopped: 2 of 63 done"
    assert s.text(ROW_MODE) == "Mode: <Add only>  MSU off"  # the plan screen again


def test_a_rom_added_to_the_library_shows_up_by_itself(s):
    (s.lib / "New Game.md").write_bytes(cart_rom(9))
    s.frames(int(appserver.LIBRARY_CHECK * 60) + 30)
    assert s.text(ROW_STATUS) == "Library changed"
    assert counts(s) == [str(len(LIBRARY_ROMS) + 1), "0", "0"]
    nothing = len([m for m in s.log if m.startswith("library changed")])
    s.frames(int(appserver.LIBRARY_CHECK * 60) + 30)  # no change since: no new plan
    assert len([m for m in s.log if m.startswith("library changed")]) == nothing


def test_a_slow_new_plan_keeps_the_app_connected(s, monkeypatch):
    # Planning took ~10 s over a network: the app missed its PONGs and showed "Disconnected"
    # (2026-11-04). The box says "Library changed..." first, and the app waits for the plan.
    real = romsync.make_plan
    seen = []

    def slow(*args, **kw):
        for _ in range(5 * 60):  # five seconds of the console while the box plans
            s.md.run_frames(1, 0)
            seen.append(s.text(ROW_STATUS))
        return real(*args, **kw)

    monkeypatch.setattr(romsync, "make_plan", slow)
    (s.lib / "New Game.md").write_bytes(cart_rom(9))
    s.frames(int(appserver.LIBRARY_CHECK * 60) + 30)
    assert "Disconnected" not in seen and seen[-1] == "Library changed..."
    assert s.text(ROW_STATUS) == "Library changed"
    assert counts(s) == [str(len(LIBRARY_ROMS) + 1), "0", "0"]
    assert s.log.count("app connected") <= 1


def test_a_slow_step_of_a_plan_gets_the_sync_screen(s):
    s.server._progress("Verifying 3/9", "Sonic.md", left=90)
    s.frames(10)
    assert s.text(ROW_STATUS) == "Verifying 3/9"
    assert s.text(ROW_ADD) == "Sonic.md"
    assert s.text(ROW_MODE) == ""
    assert s.text(ROW_SAVES).startswith("Time left: about 2 min")
    s.server._progress("Checking library", "1 of 9")  # the step's over: back to a plain status
    s.frames(10)
    assert s.text(ROW_STATUS) == "Checking library"
    assert s.text(ROW_ADD).startswith("Add")


def test_notes_and_errors_show_under_the_status(s):
    s.server.note = "Added 2  Removed 1"
    s.server._send_plan()
    s.frames(10)
    assert s.text(ROW_STATUS + 1) == "Added 2  Removed 1"
    s.server._error("Failed: 3/12 done", "Sonic.md")
    s.frames(10)
    assert s.text(ROW_STATUS) == "Failed: 3/12 done"
    assert s.text(ROW_STATUS + 1) == "Sonic.md"
