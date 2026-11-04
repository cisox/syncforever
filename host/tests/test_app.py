"""The Sync Forever NES app end to end: the app (nes/) in nesemu, started by the fake cart's
menu, talking to the real box code (appserver.py) through N8Pro, as on hardware.

Builds the ROM with `make -C nes` first; skipped when cc65 isn't installed.
"""

import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

from syncforever import apps, appserver, chr, ops, romsync, saves
from syncforever.firmware import Firmware, Release
from syncforever.n8pro import N8Pro
from syncforever.nesemu import CFG_MAP_IDX, FakeEverDrive, Nes
from syncforever.sim import FakeN8Pro

NES_DIR = Path(__file__).resolve().parents[2] / "nes"
ROM = NES_DIR / "build" / "sync-forever.nes"
APP_PATH = "syncforever-test/Sync Forever.nes"
DEST = "syncforever-test/sync"

# nes/src/screen.h and app/app.c
ROW_HEADER, ROW_STATUS, ROW_HELP = 2, 7, 26
ROW_DETAIL = ROW_STATUS + 1
ROW_MODE, ROW_ADD, ROW_REPLACE, ROW_REMOVE, ROW_WARN, ROW_SAVES, ROW_FIRMWARE = 10, 12, 13, 14, 16, 19, 20
LABEL = "Status:"


@pytest.fixture(scope="session")
def rom() -> bytes:
    if not shutil.which("cc65"):
        pytest.skip("cc65 not installed (brew install cc65)")
    subprocess.run(["make", "-C", str(NES_DIR), "-s"], check=True, capture_output=True)
    return ROM.read_bytes()


def game(mapper=1, size=0x8000, seed=0) -> bytes:
    header = b"NES\x1a" + bytes([size // 0x4000, 0, (mapper & 15) << 4, mapper & 0xF0]) + bytes(8)
    return header + bytes((seed + i) & 0xFF for i in range(size))


class Session:
    """Console + cart + box, stepped one frame at a time."""

    def __init__(self, rom, tmp_path, library_files, store=None):
        self.sd = tmp_path / "sd"
        (self.sd / "syncforever-test").mkdir(parents=True)
        (self.sd / APP_PATH).write_bytes(rom)
        self.lib = tmp_path / "library"
        self.lib.mkdir()
        for rel, data in library_files.items():
            (self.lib / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.lib / rel).write_bytes(data)

        self.sim = FakeN8Pro(self.sd)
        self.sim.on_start = self._boot
        self.nes: Nes | None = None
        self.dev = N8Pro(self.sim)
        self.dev.connect()
        self.log: list[str] = []
        self.now = 0.0
        self.server = appserver.AppServer(
            self.dev,
            self.lib,
            dest=DEST,
            policy=ops.WritePolicy(),
            manifest=romsync.Manifest(tmp_path / "state" / "cart.json"),
            saves=store,
            app_path=APP_PATH,
            log=self.log.append,
            clock=lambda: self.now,
            sleep=lambda seconds: None,
        )
        self.serving = True
        ops.launch(self.dev, APP_PATH)  # the user picks Sync Forever in the menu
        assert self.nes is not None

    def _boot(self, rel, data):
        if rel != APP_PATH:
            return None  # an ordinary game: nothing to emulate
        self.nes = Nes(data, cart=FakeEverDrive())
        return self.nes

    def frames(self, n=1, buttons=0):
        for _ in range(n):
            if not self.nes.exited:
                self.nes.run_frames(1, buttons)
            if self.serving:
                self.server.poll(timeout=0)
            self.now += 1 / 60

    def press(self, button, after=20):
        self.frames(2, button)
        self.frames(after)

    def text(self, row):
        """A row's text; the status row without its "Status:" label."""
        t = self.nes.text(row).strip()
        if row == ROW_STATUS and t.startswith(LABEL):
            t = t[len(LABEL) :].strip()
        if row == ROW_HELP and t.endswith(f"v{appserver.PROTO_VERSION}"):
            t = t[:-2].strip()  # the version in the corner
        return t

    def palette(self):
        return list(self.nes.pal[:16])


def counts(s):
    return [s.text(r).split()[-1] for r in (ROW_ADD, ROW_REPLACE, ROW_REMOVE)]


def has_header(nes):
    """The double-size title: chr.TITLE_SLOTS from column 4 of header rows 1-2 (art.h)."""
    top, bottom = nes.tiles(ROW_HEADER + 1), nes.tiles(ROW_HEADER + 2)
    return list(top[4:6]) == chr.TITLE_SLOTS[0:2] and list(bottom[4:6]) == chr.TITLE_SLOTS[2:4]


LIBRARY = {
    "Zelda.nes": game(1, seed=1),
    "Contra.nes": game(2, seed=2),
    "notes.txt": b"not a ROM",
    "Puzzle/Tetris.nes": game(1, seed=3),
    "Puzzle/Dr. Mario.nes": game(1, seed=4),
}
LIBRARY_ROMS = sorted(rel for rel in LIBRARY if rel.endswith(".nes"))


@pytest.fixture
def s(rom, tmp_path):
    session = Session(rom, tmp_path, LIBRARY)
    session.frames(60)
    return session


# --- connecting and the plan ---------------------------------------------------------


def test_opens_on_the_default_plan(s):
    assert has_header(s.nes)
    assert s.nes.text(ROW_STATUS).strip() == "Status: Ready to sync"
    assert s.text(ROW_MODE) == "Mode: <Add only>"
    assert counts(s) == [str(len(LIBRARY_ROMS)), "0", "0"]
    assert s.text(ROW_HELP) == "A:Sync  <>:Mode  B:Exit"
    assert s.text(ROW_SAVES) == "Saves: backup off"
    assert s.nes.violations == []
    assert s.nes.text(0).strip() == s.nes.text(1).strip() == ""  # overscan margin
    assert s.nes.text(ROW_HELP)[29:31] == f"v{appserver.PROTO_VERSION}"


def test_sync_copies_the_library_then_b_returns_to_the_menu(s):
    s.press("A", after=60)
    assert s.text(ROW_STATUS).startswith("Sync done in ")
    assert counts(s) == ["0", "0", "0"]  # checked again: all synced
    assert s.text(ROW_HELP) == "<>:Mode  B:Exit"
    for rel in LIBRARY_ROMS:
        assert (s.sd / DEST / rel).read_bytes() == LIBRARY[rel], rel
    assert not (s.sd / DEST / "notes.txt").exists()
    s.press("B", after=60)
    assert s.nes.exited
    assert s.nes.cart.config[CFG_MAP_IDX] == 255
    assert s.dev.menu_ping()


def test_second_visit_has_nothing_to_do(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    s.frames(60)
    s.press("A", after=60)
    s.press("B", after=60)
    ops.launch(s.dev, APP_PATH)  # open the app again
    s.frames(60)
    assert counts(s) == ["0", "0", "0"]
    assert s.text(ROW_STATUS) == "Nothing to do"  # said straight away
    assert s.text(ROW_HELP) == "<>:Mode  B:Exit"
    s.press("A", after=30)
    assert s.text(ROW_STATUS) == "Nothing to do"
    assert s.dev.app_read(0) == b""  # A sent nothing


def test_left_right_change_the_mode(s):
    blanks = s.nes.blanks
    s.press("RIGHT", after=30)
    assert s.text(ROW_MODE) == "Mode: <Update>"
    s.press("LEFT", after=30)
    s.press("LEFT", after=30)
    assert s.text(ROW_MODE) == "Mode: <Mirror STRICT>"
    assert s.text(ROW_WARN) == "Removes ROMs not in library!"
    s.press("RIGHT", after=30)
    assert s.text(ROW_MODE) == "Mode: <Add only>"
    assert s.text(ROW_WARN) == ""
    assert s.text(ROW_STATUS) == "Ready to sync"
    assert s.nes.blanks == blanks  # rows rewritten in place: the screen never flashes
    assert s.nes.violations == []


def test_mirror_removes_what_left_the_library(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    s.frames(60)
    s.press("A", after=60)
    s.press("B", after=60)
    (s.lib / "Contra.nes").unlink()
    (s.sd / DEST / "Mine.nes").write_bytes(game(0, seed=50))  # not synced: Mirror keeps it
    ops.launch(s.dev, APP_PATH)
    s.frames(60)
    s.press("RIGHT", after=30)
    s.press("RIGHT", after=30)
    assert s.text(ROW_MODE) == "Mode: <Mirror>"
    assert counts(s) == ["0", "0", "1"]
    s.press("A", after=60)
    assert not (s.sd / DEST / "Contra.nes").exists()
    assert (s.sd / DEST / "Mine.nes").exists()


def test_b_returns_to_the_menu_without_syncing(s):
    s.press("B", after=60)
    assert s.nes.exited
    assert not (s.sd / DEST).exists()


# --- save backup ---------------------------------------------------------------------


def put_save(s, game, data):
    folder = s.sd / "EDN8" / "gamedata" / game
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bram.srm").write_bytes(data)


def test_opening_the_app_backs_up_every_save(rom, tmp_path):
    (tmp_path / "store").mkdir()
    store = saves.SaveStore(tmp_path / "store")
    s = Session(rom, tmp_path, LIBRARY, store=store)
    put_save(s, "Zelda.nes", b"LINK")
    put_save(s, "Metroid.nes", b"SAMUS")
    s.frames(60)
    assert [p.read_bytes() for p in store.versions("Zelda.nes", "bram.srm")] == [b"LINK"]
    assert [p.read_bytes() for p in store.versions("Metroid.nes", "bram.srm")] == [b"SAMUS"]
    assert "saves: checked 2 files, backed up 2: Metroid.nes/bram.srm, Zelda.nes/bram.srm" in s.log
    assert s.text(ROW_STATUS) == "Ready to sync"
    assert s.text(ROW_SAVES) == "Saves: Backed up (2)"
    s.press("RIGHT", after=30)  # another plan: no second backup, the same line
    assert sum(line.startswith("saves:") for line in s.log) == 1
    assert s.text(ROW_SAVES) == "Saves: Backed up (2)"


def test_nothing_new_reads_up_to_date(rom, tmp_path):
    (tmp_path / "store").mkdir()
    store = saves.SaveStore(tmp_path / "store")
    store.add("Zelda.nes", "bram.srm", b"LINK")
    s = Session(rom, tmp_path, LIBRARY, store=store)
    put_save(s, "Zelda.nes", b"LINK")
    s.frames(60)
    assert s.text(ROW_SAVES) == "Saves: up to date (1)"


def test_missing_save_folder_is_skipped_not_created(rom, tmp_path):
    store = saves.SaveStore(tmp_path / "store")  # not mounted
    s = Session(rom, tmp_path, LIBRARY, store=store)
    put_save(s, "Zelda.nes", b"LINK")
    s.frames(60)
    assert not (tmp_path / "store").exists()
    assert any("save folder missing" in line for line in s.log)
    assert s.text(ROW_STATUS) == "Ready to sync"
    assert s.text(ROW_SAVES) == "Saves: folder missing"


def test_a_failed_backup_doesnt_block_the_sync(rom, tmp_path, monkeypatch):
    (tmp_path / "store").mkdir()
    s = Session(rom, tmp_path, LIBRARY, store=saves.SaveStore(tmp_path / "store"))

    def boom(*a, **k):
        raise OSError("disk went away")

    monkeypatch.setattr(saves, "backup_all", boom)
    s.frames(60)
    assert "save backup failed: disk went away" in s.log
    assert s.text(ROW_STATUS) == "Ready to sync"
    assert s.text(ROW_SAVES) == "Saves: backup failed"


# --- after a sync ----------------------------------------------------------------------


def test_a_sync_is_checked_again_when_it_ends(s):
    s.press("A", after=60)
    assert "sync plan (add): add 0, replace 0, remove 0; 4 files up to date" in s.log  # planned again by itself
    assert s.text(ROW_STATUS).startswith("Sync done in ")  # stays up: the counts say the rest
    assert counts(s) == ["0", "0", "0"]
    (s.lib / "Metroid.nes").write_bytes(game(1, seed=9))
    s.press("RIGHT", after=30)  # a new plan (Update): the new ROM shows
    assert counts(s) == ["1", "0", "0"]
    assert s.text(ROW_STATUS) == "Ready to sync"


def test_copies_show_the_file_name_under_the_status(s):
    long = "International Cricket Championship (Japan) (Rev 1).nes"
    s.server.send(appserver.STATUS, appserver.text("2/5") + appserver.text(long, appserver.DETAIL_MAX))
    s.frames(10)
    assert s.text(ROW_STATUS) == "2/5"
    assert s.nes.text(ROW_STATUS + 1).strip() == "International...n) (Rev 1).nes"  # 30: the line's width
    s.server.send(appserver.STATUS, appserver.text("3/5") + appserver.text("Zelda.nes"))
    s.frames(10)
    assert s.nes.text(ROW_STATUS + 1).strip() == "Zelda.nes"
    s.server.send(appserver.STATUS, appserver.text("Backing up saves"))  # no detail: blank
    s.frames(10)
    assert s.nes.text(ROW_STATUS + 1).strip() == ""


def test_the_mode_row_survives_status_messages(s):
    s.press("A", after=60)  # sync: SYNC_DONE's text replaces msg_text
    assert s.text(ROW_MODE) == "Mode: <Add only>"


# --- the link ------------------------------------------------------------------------


def test_connects_when_the_box_starts_late(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    s.serving = False
    s.frames(200)
    assert s.text(ROW_STATUS) == "Connecting..."
    s.frames(150)  # ~5 s with nobody answering
    assert s.text(ROW_STATUS) == "Disconnected"
    assert s.text(ROW_HELP) == "B:Exit"
    s.serving = True
    s.frames(150)
    assert s.text(ROW_STATUS) == "Ready to sync"


def test_box_stops_answering_a_request(s):
    s.serving = False
    s.press("RIGHT", after=1850)  # a plan request nobody answers
    assert s.text(ROW_STATUS) == "Disconnected"
    s.serving = True
    s.frames(200)
    assert s.text(ROW_STATUS) == "Ready to sync"


def test_idle_app_notices_the_box_going_away_and_coming_back(s):
    s.frames(300)  # idle: the heartbeat keeps it connected
    assert s.text(ROW_STATUS) == "Ready to sync"
    s.serving = False
    s.frames(250)  # ~4 s without an answer
    assert s.text(ROW_STATUS) == "Disconnected"
    assert s.text(ROW_ADD) == ""  # the plan is gone: it may be stale
    assert s.text(ROW_HELP) == "B:Exit"
    s.serving = True
    s.frames(200)
    assert s.text(ROW_STATUS) == "Ready to sync"
    assert counts(s) == ["4", "0", "0"]


def test_a_restarted_box_gets_a_fresh_hello(s):
    old = s.server
    s.server = appserver.AppServer(
        s.dev, s.lib, dest=DEST, policy=ops.WritePolicy(), manifest=old.manifest, saves=None,
        app_path=APP_PATH, log=s.log.append, clock=lambda: s.now, sleep=lambda seconds: None,
    )  # fmt: skip
    s.log.clear()
    s.frames(150)  # the next PING: the new box doesn't know the app, so it says hello again
    assert "app connected" in s.log
    assert s.text(ROW_STATUS) == "Ready to sync"
    s.press("A", after=300)
    assert s.text(ROW_STATUS).startswith("Sync done in ")


def test_junk_in_the_fifo_is_skipped(s):
    # Stray menu pings (the box watches the menu), a zero, and an 'S' just before a frame.
    s.nes.host_send(b"*t*t\x00S" + appserver.frame(appserver.STATUS, appserver.text("Hello")))
    s.frames(5)
    assert s.text(ROW_STATUS) == "Hello"


def test_library_offline_shows_an_error(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    shutil.rmtree(s.lib)
    s.frames(60)
    assert s.text(ROW_STATUS) == "Library offline"
    assert s.text(ROW_HELP) == "A:Check again  B:Exit"


def test_an_old_app_is_told_to_update(rom, tmp_path, monkeypatch):
    v = appserver.PROTO_VERSION
    monkeypatch.setattr(appserver, "PROTO_VERSION", v + 1)
    s = Session(rom, tmp_path, LIBRARY)
    s.frames(60)
    assert s.text(ROW_STATUS) == f"App too old (v{v})"


def test_an_updated_app_restarts_itself(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    updates = iter([True, False])  # the box replaced the file, then it's current
    s.server.update_app = lambda: next(updates)
    first = s.nes
    s.frames(30)
    assert s.text(14) == "Updating..."
    s.frames(60)
    assert s.text(14) == "Restarting..."  # it comes back: the box relaunches it
    s.frames(90)
    assert first.exited
    assert s.server.launched == []  # no ping while the menu comes back (MENU_FIRST_PING)
    frames = round((appserver.MENU_FIRST_PING + appserver.MENU_SETTLE + 1) * 60)
    s.frames(frames)
    assert s.server.launched == [APP_PATH]
    assert s.nes is not first  # the new copy, started by the box through the menu
    s.frames(60)
    assert s.text(ROW_STATUS) == "Ready to sync"


def test_long_messages_fill_the_line(s):
    """cc65 2.19 miscompiled the clamp in rx_str: texts of 30+ chars showed as 3-4 letters."""
    s.server.send(appserver.STATUS, appserver.text("x" * 40, appserver.TEXT_MAX))
    s.frames(5)
    assert s.text(ROW_STATUS) == "x" * 23  # columns 9-31


# --- colours ---------------------------------------------------------------------------


def test_status_colours(s):
    assert s.palette()[1] == 0x16  # charcoal and red
    assert s.palette()[14] == 0x29  # "Ready to sync": green
    s.server.send(appserver.STATUS, appserver.text("Copying"))
    s.frames(5)
    assert s.palette()[14] == s.palette()[10]  # a progress message: the label colour
    s.server.send(appserver.ERROR, appserver.text("Oops"))
    s.frames(5)
    assert s.palette()[14] == 0x26  # red


# --- the copy the box ships ------------------------------------------------------------


def test_packaged_app_is_up_to_date(rom, tmp_path):
    """data/sync-forever.nes must be rebuilt (`make -C nes package`) whenever the app changes."""
    out = tmp_path / "pkg"
    subprocess.run(
        ["make", "-C", str(NES_DIR), "-s", f"B={out}", "MENU_ROM=/nonexistent"], check=True, capture_output=True
    )
    assert (out / "sync-forever.nes").read_bytes() == apps.packaged(), "run: make -C nes package"


# --- without an EverDrive ------------------------------------------------------------


def test_boot_without_everdrive(rom):
    nes = Nes(rom)
    nes.run_frames(30)
    assert has_header(nes)
    assert nes.text(ROW_STATUS).strip() == "Status: No EverDrive found"
    nes.press("B")
    assert not nes.exited


# --- the harness itself --------------------------------------------------------------


def _tiny_rom(code: bytes) -> bytes:
    prg = bytearray(0x8000)
    prg[: len(code)] = code
    prg[0x7FFC:0x7FFE] = b"\x00\x80"  # reset -> $8000
    return b"NES\x1a\x02\x01\x01\x00" + bytes(8) + bytes(prg) + bytes(0x2000)


def test_harness_flags_vram_writes_while_rendering():
    code = bytes.fromhex(
        "a90a 8d0120"  # LDA #$0A, STA $2001: rendering on
        "a920 8d0620 a900 8d0620"  # PPU_ADDR = $2000, mid-frame
        "a941 8d0720"  # STA $2007: a write the hardware would scatter
        "4c1480".replace(" ", "")  # JMP self
    )
    nes = Nes(_tiny_rom(code))
    nes.run_frames(2)
    assert {v.register for v in nes.violations} == {0x2006, 0x2007}
    assert nes.nt[0] == 0x41


def test_harness_no_violation_inside_vblank():
    code = bytes.fromhex(
        "a90a 8d0120"  # rendering on
        "2c0220 2c0220 10fb"  # wait for vblank
        "a920 8d0620 a900 8d0620 a941 8d0720"  # write in vblank
        "4c1b80".replace(" ", "")
    )
    nes = Nes(_tiny_rom(code))
    nes.run_frames(3)
    assert nes.violations == []
    assert nes.nt[0] == 0x41


# --- protocol pieces -----------------------------------------------------------------


def test_deframer_resyncs_and_waits_for_whole_frames():
    d = appserver.Deframer()
    f = appserver.frame(appserver.SYNC_PLAN, bytes([2]))
    assert d.feed(b"\x00*tS" + f[:4]) == []
    assert d.feed(f[4:] + b"SF\x01\xff\xff" + appserver.frame(appserver.HELLO, bytes([2]))) == [
        (appserver.SYNC_PLAN, b"\x02"),
        (appserver.HELLO, b"\x02"),
    ]


def test_text_field_is_printable_ascii():
    assert appserver.text("Pokémon\n") == b"\x09Pok??mon?"  # é is two UTF-8 bytes
    assert appserver.text("ab", 1) == b"\x01a"


# --- firmware ----------------------------------------------------------------------------

VERSION_TILES = dict(zip(chr.VERSION_SLOTS, chr.VERSION_CHARS, strict=True))
N8_FILES = ("edn8-pro-fw-v25.1109.efu", "edn8-pro-fw-v25.1123.efu")


def firmware_line(s):
    """The firmware row, its colour-2 version tiles read back as characters."""
    return "".join(VERSION_TILES.get(t) or bytes([t & 0x7F]).decode() for t in s.nes.tiles(ROW_FIRMWARE)).strip()


def n8_firmware(tmp_path, current, delivered=()):
    path = tmp_path / "download.efu"
    path.write_bytes(bytes(range(256)) * 40)
    return Firmware(Release("25.1123", N8_FILES[1]), current, path, frozenset(delivered))


def test_newer_firmware_is_copied_to_the_root_and_shown_in_red(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    info = n8_firmware(tmp_path, current=False)
    s.server.firmware = lambda: info
    records = []
    s.server.firmware_record = lambda file, present: records.append((file, present))
    s.frames(90)
    assert s.text(ROW_STATUS) == "Ready to sync"
    assert firmware_line(s) == "Firmware: v25.1123"
    assert s.palette()[2] == 0x26  # red
    assert (s.sd / N8_FILES[1]).read_bytes() == info.path.read_bytes()
    assert any("install it from the cart's menu" in line for line in s.log)
    assert records == [(N8_FILES[1], True)]  # the box remembers it put it there


def test_installed_firmware_is_green_and_only_the_boxs_files_go(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    for name in N8_FILES:
        (s.sd / name).write_bytes(b"old")
    # The box delivered the older one; the owner copied the newer one there themselves.
    s.server.firmware = lambda: n8_firmware(tmp_path, current=True, delivered=[N8_FILES[0]])
    records = []
    s.server.firmware_record = lambda file, present: records.append((file, present))
    s.frames(90)
    assert firmware_line(s) == "Firmware: v25.1123"
    assert s.palette()[2] == 0x29  # green
    assert not (s.sd / N8_FILES[0]).exists()
    assert (s.sd / N8_FILES[1]).read_bytes() == b"old"  # the owner's file stays
    assert records == [(N8_FILES[0], False)]


def test_no_firmware_line_without_a_check(s):
    assert s.text(ROW_FIRMWARE) == ""


NESOS_NEW = bytes(range(256)) * 32  # the menu file in the .efu


@pytest.mark.parametrize(("card_os", "colour"), [(NESOS_NEW, 0x29), (b"older menu" * 100, 0x26)])
def test_the_cards_menu_file_decides_green_or_red(rom, tmp_path, card_os, colour):
    """The N8's build date doesn't change with an update (hardware): the box compares the
    card's EDN8/nesos.nes with the .efu's CRC32 instead, while the app runs."""
    s = Session(rom, tmp_path, LIBRARY)
    (s.sd / "EDN8").mkdir(exist_ok=True)
    (s.sd / "EDN8" / "nesos.nes").write_bytes(card_os)
    marker = ("EDN8/nesos.nes", len(NESOS_NEW), zlib.crc32(NESOS_NEW))
    info = Firmware(Release("25.1123", N8_FILES[1]), False, n8_firmware(tmp_path, False).path, frozenset(), marker)
    s.server.firmware = lambda: info
    s.frames(90)
    assert firmware_line(s) == "Firmware: v25.1123"
    assert s.palette()[2] == colour
    assert (s.sd / N8_FILES[1]).exists() == (colour == 0x26)  # delivered only while it's needed


def test_a_rom_added_to_the_library_shows_up_by_itself(s):
    # The user leaves the app on its plan and copies a ROM to the library (appserver._watch_library).
    (s.lib / "New Game.nes").write_bytes(b"N" * 40000)
    s.frames(int(appserver.LIBRARY_CHECK * 60) + 30)
    assert s.nes.text(ROW_STATUS).strip() == "Status: Library changed"
    assert counts(s) == [str(len(LIBRARY_ROMS) + 1), "0", "0"]
    assert any(m.startswith("library changed: sync plan") for m in s.log), s.log
    assert s.nes.violations == []
    s.press("A", after=60)  # and syncs it
    assert (s.sd / DEST / "New Game.nes").read_bytes() == b"N" * 40000


def test_the_sync_screen_and_b_stops_after_the_file(s):
    s.serving = False
    s.press("A", after=5)
    assert [k for k, _ in appserver.Deframer().feed(s.dev.app_read(0))] == [appserver.SYNC_RUN]
    s.server._progress("Copying 3 of 61", "Legend of Zelda, The (USA).nes", expect=148, left=2550)
    s.frames(2 * 60 + 30)  # the clock starts when the app reads the STATUS, a few frames on
    assert s.nes.text(ROW_STATUS).strip() == "Status: Copying 3 of 61"
    assert s.text(ROW_ADD) == "Legend of Zelda, The (USA).nes"
    assert s.text(ROW_WARN) == "This file: 0:02 / 2:28"
    assert s.text(ROW_SAVES) == "Time left: about 43 min"
    assert s.text(ROW_HELP) == "B:Stop after this file"
    s.press("B", after=5)
    assert s.text(ROW_HELP) == "Stopping after this file..."
    assert appserver.Deframer().feed(s.dev.app_read(0)) == []
    s.server._progress("Removing 1 of 2", "old.nes", expect=0, left=2)
    s.frames(10)
    assert [k for k, _ in appserver.Deframer().feed(s.dev.app_read(0))] == [appserver.STOP]
    assert s.nes.violations == []


# --- what the app is told during slow work ---------------------------------------------------


def statuses(server):
    """Every STATUS's (text, detail) as the server sends it."""
    said, send = [], server.send

    def spy(kind, payload=b""):
        if kind == appserver.STATUS:
            n = payload[0]
            detail = payload[n + 2 : n + 2 + payload[n + 1]].decode() if len(payload) > n + 1 else None
            said.append((payload[1 : n + 1].decode(), detail))
        send(kind, payload)

    server.send = spy
    return said


def test_a_long_backup_keeps_telling_the_app(rom, tmp_path):
    # The app gives up after 30 s without a STATUS, and its HELLO lands in the box's next read.
    (tmp_path / "store").mkdir()
    s = Session(rom, tmp_path, LIBRARY, store=saves.SaveStore(tmp_path / "store"))
    for game in ("A.nes", "B.nes", "C.nes"):
        put_save(s, game, b"LINK")
    now = [0.0]

    def clock():
        now[0] += romsync.STATUS_EVERY
        return now[0]

    s.server.clock = clock
    said = statuses(s.server)
    assert s.server._backup_saves()
    assert ("Backing up saves", None) in said
    assert ("Saves 3 of 3", "C.nes") in said


def test_a_short_backup_says_little(rom, tmp_path):
    (tmp_path / "store").mkdir()
    s = Session(rom, tmp_path, LIBRARY, store=saves.SaveStore(tmp_path / "store"))
    put_save(s, "A.nes", b"LINK")
    s.server.clock = lambda: 0.0
    said = statuses(s.server)
    assert s.server._backup_saves()
    assert said == [("Backing up saves", None)]


def test_each_step_of_opening_the_app_is_named(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    said = statuses(s.server)
    s.frames(60)
    texts = [text for text, _ in said]
    assert texts.index("Checking firmware") < texts.index("Scanning library")
    assert s.text(ROW_STATUS) == "Ready to sync"


def test_sync_done_stays_up_after_a_slow_plan(rom, tmp_path):
    # The plan after a sync sends progress of its own; the app leaves its status alone at the PLAN.
    s = Session(rom, tmp_path, LIBRARY)
    s.frames(60)
    s.press("A", after=120)
    assert s.server.sync_result == ""  # the plan after the sync has gone out
    s.server.sync_result = "Sync done"
    said = statuses(s.server)
    s.server._sync_plan(appserver.DEFAULT_MODE)
    assert said[-1] == ("Sync done", None)
    assert s.server.sync_result == ""


# --- the full-screen view, results and errors ------------------------------------------------


def test_a_slow_step_of_a_plan_gets_the_sync_screen(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    s.frames(60)
    assert s.text(ROW_STATUS) == "Ready to sync"
    s.server._progress("Verifying 3/9", "Dragon Quest.nes", left=90)
    s.frames(30)
    assert s.text(ROW_STATUS) == "Verifying 3/9"
    assert s.text(ROW_DETAIL) == ""  # the name moved to the body
    assert s.text(ROW_ADD) == "Dragon Quest.nes"
    assert s.text(ROW_SAVES).startswith("Time left: about 2 min")
    assert s.text(ROW_FIRMWARE).endswith("%")
    assert s.text(ROW_MODE) == ""
    s.server._progress("Checking library", "1 of 9")  # the step's over: back to a plain status
    s.frames(30)
    assert s.text(ROW_DETAIL) == "1 of 9"
    assert s.text(ROW_ADD).startswith("Add")
    assert s.nes.violations == []


def test_a_plan_shows_its_note_under_the_status(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    s.frames(60)
    s.server.note = "Added 2  Removed 1"
    s.server._send_plan()
    s.frames(10)
    assert s.text(ROW_DETAIL) == "Added 2  Removed 1"
    assert s.server.note == ""  # once


def test_a_finished_sync_says_what_it_did(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    s.frames(60)
    s.press("A", after=120)
    assert s.text(ROW_STATUS).startswith("Sync done in ")
    assert s.text(ROW_DETAIL).startswith("Added ")


def test_an_error_shows_what_went_wrong_under_the_status(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    s.frames(60)
    s.server._error("Failed: 3/12 done", "Zelda.nes")
    s.frames(10)
    assert s.text(ROW_STATUS) == "Failed: 3/12 done"
    assert s.text(ROW_DETAIL) == "Zelda.nes"


def test_a_lost_box_gets_a_hint(rom, tmp_path):
    s = Session(rom, tmp_path, LIBRARY)
    s.frames(60)
    s.serving = False
    s.frames(260)
    assert s.text(ROW_STATUS) == "Disconnected"
    assert s.text(ROW_DETAIL) == "Is the box on and plugged in?"


def test_durations_and_summaries_fit_the_screen():
    assert appserver._duration(0.2) == "1 s"
    assert appserver._duration(45) == "45 s"
    assert appserver._duration(14 * 60) == "14 min"
    assert appserver._duration(74 * 60) == "1 h 14 min"
    plan = romsync.Plan("add", add=["a"] * 12, replace=["b"] * 2, remove=["c"] * 3)
    assert appserver._what_changed(plan) == "Added 12  Replaced 2  Removed 3"
    assert len(appserver._what_changed(plan)) <= 31
