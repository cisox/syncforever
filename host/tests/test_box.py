import json
import os
from datetime import datetime

import pytest

from syncforever import apps, appserver
from syncforever.box import START_SETTLE, STAT_EVERY, Box, StartWatch, policy_for
from syncforever.config import Config
from syncforever.ops import WriteNotAllowed
from syncforever.protocol import Cmd, LinkError
from syncforever.sim import FakeN8Pro


class Port(FakeN8Pro):
    """A simulated cart with SerialTransport's reopen(): `gone` decides whether it comes back."""

    def __init__(self, root, gone=False):
        super().__init__(root)
        self.gone = gone
        self.reopens = 0
        self.closed = False

    def reopen(self, wait=10.0):
        self.reopens += 1
        if self.gone:
            raise LinkError("cart did not come back")

    def close(self):
        self.closed = True
        super().close()


@pytest.fixture
def lib(tmp_path):
    d = tmp_path / "lib"
    d.mkdir()
    return d


def make_box(lib, tmp_path, carts, **cfg):
    log = []
    carts = iter(carts)

    def open_cart(log_):
        cart = next(carts, None)
        if cart is None:
            raise KeyboardInterrupt
        return cart

    box = Box(
        Config(library=lib, state=tmp_path / "state", **cfg),
        open_cart,
        log=log.append,
        now=lambda: datetime(2026, 11, 1, 12, 0, 0),
        sleep=lambda s: None,
    )
    return box, log


def script_polls(monkeypatch, steps):
    """AppServer.poll runs these in turn: an exception class/instance is raised."""
    steps = iter(steps)

    def poll(self, timeout=0.05):
        step = next(steps, KeyboardInterrupt)
        if step is not None:
            raise step

    monkeypatch.setattr(appserver.AppServer, "poll", poll)


def test_a_short_usb_blink_keeps_the_session(sd, lib, tmp_path, monkeypatch):
    port = Port(sd)
    box, log = make_box(lib, tmp_path, [port])
    script_polls(monkeypatch, [None, OSError("Device not configured"), None])
    with pytest.raises(KeyboardInterrupt):
        box.run()
    assert port.reopens == 1
    assert sum("cart connected" in line for line in log) == 1  # setup ran once
    assert any("USB blinked" in line for line in log)


def test_a_gone_cart_ends_the_session_and_the_next_one_is_set_up(sd, lib, tmp_path, monkeypatch):
    first, second = Port(sd, gone=True), Port(sd)
    box, log = make_box(lib, tmp_path, [first, second])
    script_polls(monkeypatch, [OSError("Device not configured"), None])
    with pytest.raises(KeyboardInterrupt):
        box.run()
    assert first.closed
    assert any(line.endswith("cart gone") for line in log)
    assert sum("cart connected" in line for line in log) == 2
    assert sum("set the cart clock" in line for line in log) == 2


def test_a_cart_that_wont_connect_is_retried(sd, lib, tmp_path, monkeypatch):
    class Mute(Port):
        def read(self, n):
            return b""

    box, log = make_box(lib, tmp_path, [Mute(sd), Port(sd)])
    script_polls(monkeypatch, [])
    with pytest.raises(KeyboardInterrupt):
        box.run()
    assert any("not answering (console off?)" in line for line in log)
    assert sum("cart connected" in line for line in log) == 1


def test_a_silent_cart_is_logged_once_and_again_after_it_has_answered(sd, lib, tmp_path, monkeypatch):
    # With a hub and one console on at a time, the others sit on USB unanswering, all day.
    class Mute(Port):
        device = "/dev/ttyACM1"

        def read(self, n):
            return b""

    class Answers(Port):
        device = "/dev/ttyACM1"

    box, log = make_box(lib, tmp_path, [Mute(sd), Mute(sd), Mute(sd), Answers(sd), Mute(sd)])
    script_polls(monkeypatch, [])
    with pytest.raises(KeyboardInterrupt):
        box.run()
    silent = [line for line in log if "not answering" in line]
    assert len(silent) == 1 and silent[0].startswith("[ttyACM1] on USB")
    assert sum("cart connected" in line for line in log) == 1


def test_connect_neither_pings_nor_opens_files(sd, lib, tmp_path, monkeypatch):
    """A menu that may be in use gets stats only: a ping is read as its own MCU reply
    (Error:40), opening a file closes its own (Error:09). Hardware, 2026-11-01."""
    port = Port(sd)
    box, log = make_box(lib, tmp_path, [port], saves=tmp_path)
    script_polls(monkeypatch, [None] * 3)
    with pytest.raises(KeyboardInterrupt):
        box.run()
    assert "ready" in log
    assert Cmd.MEM_WR not in port.commands  # no ping
    assert Cmd.F_FOPN not in port.commands and Cmd.F_DIR_LD not in port.commands
    assert Cmd.F_FINFO in port.commands  # recent.bin's date
    assert not (sd / "Sync Forever.nes").exists()


def test_after_a_start_the_app_is_installed_once_and_saves_backed_up(sd, lib, tmp_path):
    (tmp_path / "store").mkdir()
    recent(sd, "/Zelda.nes")
    save(sd, "Zelda.nes", b"LINK")
    box, log = make_box(lib, tmp_path, [], saves=tmp_path / "store")
    dev = connected(sd)
    cart = box.cart(dev)
    cart.after_start()
    assert not (sd / "Sync Forever.nes").exists()  # a game runs: reads only (never tried to write)
    assert "recent games: checked 1 files, backed up 1: Zelda.nes/bram.srm" in log
    cart.after_start()
    assert "recent games: checked 1 files, backed up 0" in log


def boom():
    raise ValueError("boom")


def test_a_failing_step_doesnt_stop_the_others(sd, lib, tmp_path):
    (tmp_path / "store").mkdir()
    recent(sd, "/Zelda.nes")
    save(sd, "Zelda.nes", b"LINK")
    box, log = make_box(lib, tmp_path, [], app_path="/EDN8/Sync Forever.nes", saves=tmp_path / "store")
    cart = box.cart(connected(sd))
    cart._step("one", boom)
    cart.after_start()
    assert "one failed: boom" in log
    assert any(line.startswith("recent games: checked 1") for line in log)


def test_missing_save_folder_is_skipped_not_created(sd, lib, tmp_path):
    recent(sd, "/Zelda.nes")
    save(sd, "Zelda.nes", b"LINK")
    box, log = make_box(lib, tmp_path, [], app_path="", saves=tmp_path / "store")
    box.cart(connected(sd)).after_start()
    assert not (tmp_path / "store").exists()
    assert any("save folder missing" in line for line in log)


# --- StartWatch -----------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def connected(sd):
    from syncforever.n8pro import N8Pro

    d = N8Pro(FakeN8Pro(sd))
    d.connect()
    return d


def recent(sd, *roms, when=None):
    folder = sd / "EDN8" / "sysdata"
    folder.mkdir(parents=True, exist_ok=True)
    f = folder / "recent.bin"
    f.write_bytes(b"".join(r.encode().ljust(512, b"\0") for r in roms))
    if when is not None:
        os.utime(f, (when, when))


def save(sd, game, data):
    folder = sd / "EDN8" / "gamedata" / game
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "bram.srm").write_bytes(data)


@pytest.fixture
def watched(sd):
    recent(sd, "/Zelda.nes", when=1_700_000_000)
    clock, runs, log = Clock(), [], []
    w = StartWatch(connected(sd), lambda: runs.append(clock.now), log.append, clock)

    def run(seconds):
        end = clock.now + seconds
        while clock.now < end:
            clock.now = round(clock.now + 0.1, 1)
            w.step()

    return w, clock, runs, run


def test_the_first_date_is_only_recorded(watched):
    _w, _clock, runs, run = watched
    run(20)
    assert runs == []


def test_work_runs_once_a_start_has_settled(watched, sd):
    _w, clock, runs, run = watched
    run(3)
    recent(sd, "/DuckTales.nes", "/Zelda.nes", when=1_700_000_100)  # the menu starts DuckTales
    run(2)  # the next stat sees it
    seen = clock.now
    assert runs == []
    run(START_SETTLE + 1)
    assert len(runs) == 1 and runs[0] - seen <= START_SETTLE + STAT_EVERY
    run(20)
    assert len(runs) == 1  # nothing new


def test_a_new_start_within_the_settle_time_restarts_it(watched, sd):
    _w, _clock, runs, run = watched
    run(3)
    recent(sd, "/A.nes", when=1_700_000_100)
    run(3)
    recent(sd, "/B.nes", when=1_700_000_200)  # reset and started another game quickly
    run(START_SETTLE - 1)
    assert runs == []
    run(5)
    assert len(runs) == 1


def test_policy_for_dest():
    assert policy_for("/").check("/Zelda.nes") == "Zelda.nes"
    assert policy_for("/roms").check("/roms/x.nes") == "roms/x.nes"
    with pytest.raises(WriteNotAllowed):
        policy_for("/roms").check("/other/x.nes")


def test_the_app_file_is_checked_at_the_first_hello_of_a_connection(sd, lib, tmp_path):
    box, log = make_box(lib, tmp_path, [])
    dev = connected(sd)
    cart = box.cart(dev)
    assert cart.update_app() is True  # installed: the running copy is stale
    assert cart.update_app() is False  # once per connection
    assert box.cart(dev).update_app() is False  # a new connection: checked again, current
    assert "app is up to date at /Sync Forever.nes" in log


def test_a_mega_gets_the_genesis_app(sd, lib, tmp_path):
    from syncforever.carts import MEGA_PRO
    from syncforever.n8pro import EverDrive

    box, log = make_box(lib, tmp_path, [])  # app_path defaults to /Sync Forever.nes
    dev = EverDrive(FakeN8Pro(sd, profile=MEGA_PRO))
    dev.connect()
    box.cart(dev)._install_app()  # at its first HELLO, or sfe install
    assert (sd / "Sync Forever.md").read_bytes() == apps.packaged(".md")
    assert not (sd / "Sync Forever.nes").exists()
    assert "app installed at /Sync Forever.md (app font)" in log
    assert box.cart(dev).server("x").app_path == "Sync Forever.md"  # what a restart launches


def test_two_carts_are_served_at_once_each_with_its_consoles_folders(sd, tmp_path, monkeypatch):
    """One session thread per cart; carts told apart by what they report, logs tagged."""
    import threading

    from syncforever.carts import MEGA_PRO
    from syncforever.config import ConsoleSettings

    nes_sd, md_sd = tmp_path / "n8", tmp_path / "mega"
    for d in (nes_sd, md_sd):
        d.mkdir()
    (tmp_path / "nes-lib").mkdir()
    (tmp_path / "md-lib").mkdir()
    cfg = Config(
        state=tmp_path / "state",
        consoles={
            "nes": ConsoleSettings(library=tmp_path / "nes-lib"),
            "genesis": ConsoleSettings(library=tmp_path / "md-lib", dest="/sync"),
        },
    )
    servers, log = {}, []
    lock = threading.Lock()

    def poll(self, timeout=0.05):
        with lock:
            servers[self.dev.profile.key] = self
        raise OSError("test over")  # ends this session's thread the way a lost cart does

    monkeypatch.setattr(appserver.AppServer, "poll", poll)
    carts = {"/dev/a": lambda: FakeN8Pro(nes_sd), "/dev/b": lambda: FakeN8Pro(md_sd, profile=MEGA_PRO)}
    import time as _time

    def sleep(seconds):
        main = threading.current_thread() is threading.main_thread()
        if main and (len(servers) == 2 or _time.monotonic() > deadline):
            raise KeyboardInterrupt  # stop the supervisor (never a session thread)
        _time.sleep(0.02)

    deadline = _time.monotonic() + 5

    box = Box(cfg, log=log.append, now=lambda: datetime(2026, 11, 2), sleep=sleep, tag=True)
    with pytest.raises(KeyboardInterrupt):
        box.serve_all(lambda: list(carts), lambda dev: carts[dev]())
    for thread in box.threads:  # none may outlive the test: a later test patches poll too
        thread.join(5)
        assert not thread.is_alive()
    assert servers["nes"].library == tmp_path / "nes-lib"
    assert (servers["genesis"].library, servers["genesis"].dest) == (tmp_path / "md-lib", "sync")
    assert any(line.startswith("[N8 PRO ") and "cart connected: EverDrive-N8 PRO" in line for line in log)
    assert any(line.startswith("[Mega PRO ") and "cart connected: Mega EverDrive PRO" in line for line in log)


def test_the_box_leaves_the_cart_alone_while_the_app_is_open(sd, lib, tmp_path, monkeypatch):
    """The app's pings would land inside a cart reply (hardware: stat read 'S' as 0x53)."""
    steps = []
    monkeypatch.setattr(StartWatch, "step", lambda self: steps.append(self.started_at))
    active = iter([True, True, False])
    monkeypatch.setattr(appserver.AppServer, "app_active", lambda self: next(active, False))
    script_polls(monkeypatch, [None, None, None])
    box, _ = make_box(lib, tmp_path, [Port(sd)])
    with pytest.raises(KeyboardInterrupt):
        box.run()
    assert steps == [None]  # one step, after the app went quiet, with the pending start dropped


class SwappedPort(Port):
    """The port reopens with another cart behind it (same device name, a different serial)."""

    def reopen(self, wait=10.0):
        super().reopen(wait)
        self.serial_words = (0x0BADCAFE, 0x00004E86)


def test_a_different_cart_on_the_same_port_starts_a_new_session(sd, lib, tmp_path, monkeypatch):
    port = SwappedPort(sd)
    box, log = make_box(lib, tmp_path, [port, Port(sd)])
    script_polls(monkeypatch, [OSError("Device not configured"), None])
    with pytest.raises(KeyboardInterrupt):
        box.run()
    assert "a different cart is on this port: starting over" in log
    assert not any("USB blinked" in line for line in log)
    assert sum("cart connected" in line for line in log) == 2  # set up again, as the new cart


def test_connect_says_when_the_app_is_missing(sd, lib, tmp_path, monkeypatch):
    port = Port(sd)
    box, log = make_box(lib, tmp_path, [port])
    script_polls(monkeypatch, [None])
    with pytest.raises(KeyboardInterrupt):
        box.run()
    assert any("no Sync Forever app at /Sync Forever.nes yet" in line for line in log), log


def test_connect_is_quiet_when_the_app_is_there(sd, lib, tmp_path, monkeypatch):
    (sd / "Sync Forever.nes").write_bytes(b"old")
    box, log = make_box(lib, tmp_path, [Port(sd)])
    script_polls(monkeypatch, [None])
    with pytest.raises(KeyboardInterrupt):
        box.run()
    assert not any("no Sync Forever app" in line for line in log)


def test_a_port_is_named_by_its_usb_id_before_the_cart_answers(monkeypatch):
    from types import SimpleNamespace

    from syncforever import box as boxmod
    from syncforever import transport

    ports = [
        SimpleNamespace(device="/dev/ttyACM0", vid=0x38DF, pid=0x17),
        SimpleNamespace(device="/dev/ttyACM1", vid=0x38DF, pid=0x18),
    ]
    monkeypatch.setattr(transport, "find_ports", lambda: ports)
    assert boxmod._pro_label("/dev/ttyACM0") == ("N8 PRO", "38df:0017")
    assert boxmod._pro_label("/dev/ttyACM1") == ("Mega PRO", "38df:0018")
    assert boxmod._pro_label("/dev/ttyUSB9") == ("EverDrive", None)
    assert boxmod._who(SimpleNamespace(device="/dev/ttyACM1", label="Mega PRO")) == "[Mega PRO ttyACM1]"


def test_a_mega_moves_by_copying_on_the_cart(sd, lib, tmp_path):
    # F_FMV moves nothing on the N8 and Mega; F_FCP copies (2026-11-04): copy, then delete.
    from syncforever import ops, romsync
    from syncforever.carts import MEGA_PRO
    from syncforever.n8pro import EverDrive

    dev = EverDrive(FakeN8Pro(sd, profile=MEGA_PRO))
    dev.connect()
    library = tmp_path / "md"
    (library / "Sega").mkdir(parents=True)
    (library / "Sega" / "Sonic.md").write_bytes(b"S" * 4000)
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    policy = ops.WritePolicy(("roms",))
    plan = romsync.make_plan(dev, library, "roms", "strict", manifest)
    romsync.run_plan(dev, plan, library, "roms", manifest, policy=policy)
    (library / "Sega" / "Sonic.md").rename(library / "Sonic.md")  # moved into another folder
    plan = romsync.make_plan(dev, library, "roms", "strict", manifest)
    assert plan.moves == [("roms/Sega/Sonic.md", "Sonic.md")] and plan.add == []
    dev.t.commands.clear()
    romsync.run_plan(dev, plan, library, "roms", manifest, policy=policy)
    assert (sd / "roms" / "Sonic.md").read_bytes() == b"S" * 4000
    assert not (sd / "roms" / "Sega" / "Sonic.md").exists() and not (sd / "roms" / "Sega").exists()
    assert 0xD6 in dev.t.commands and 0xCC not in dev.t.commands  # F_FCP; no F_FWR: nothing sent over USB
    assert romsync.make_plan(dev, library, "roms", "strict", manifest).empty


def mega_plan(sd, tmp_path, library):
    from syncforever import romsync
    from syncforever.carts import MEGA_PRO
    from syncforever.n8pro import EverDrive

    dev = EverDrive(FakeN8Pro(sd, profile=MEGA_PRO))
    dev.connect()
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    return dev, manifest, romsync.make_plan(dev, library, "roms", "strict", manifest)


def test_a_rom_moved_into_a_folder_needs_no_record_to_be_moved(sd, tmp_path):
    # A reorganised library, its files on the card from before the box kept records (2026-11-04:
    # add 941, remove 415, no moves): the same name and size, read once to be sure.
    from syncforever import ops, romsync

    (sd / "roms").mkdir(exist_ok=True)
    (sd / "roms" / "Sonic.md").write_bytes(b"S" * 4000)
    library = tmp_path / "md"
    (library / "MD").mkdir(parents=True)
    (library / "MD" / "Sonic.md").write_bytes(b"S" * 4000)
    dev, manifest, plan = mega_plan(sd, tmp_path, library)
    assert plan.moves == [("roms/Sonic.md", "MD/Sonic.md")] and plan.add == []
    assert "roms/Sonic.md" not in plan.remove
    dev.t.commands.clear()
    said = []
    romsync.run_plan(
        dev,
        plan,
        library,
        "roms",
        manifest,
        policy=ops.WritePolicy(("roms",)),
        progress=lambda m, d=None, **k: said.append(m),
    )
    assert (sd / "roms" / "MD" / "Sonic.md").read_bytes() == b"S" * 4000 and not (sd / "roms" / "Sonic.md").exists()
    assert 0xCC not in dev.t.commands  # no F_FWR: nothing sent over USB
    assert said[0] == "Moving 1 of 1"
    assert manifest.get("roms/MD/Sonic.md")["size"] == 4000 and manifest.get("roms/Sonic.md") is None


def test_the_same_name_and_size_with_other_contents_is_uploaded(sd, tmp_path):
    from syncforever import ops, romsync

    (sd / "roms").mkdir(exist_ok=True)
    (sd / "roms" / "Sonic.md").write_bytes(b"o" * 4000)  # an older hack: same name, same size
    library = tmp_path / "md"
    (library / "MD").mkdir(parents=True)
    (library / "MD" / "Sonic.md").write_bytes(b"N" * 4000)
    dev, manifest, plan = mega_plan(sd, tmp_path, library)
    assert plan.moves
    lines = []
    romsync.run_plan(dev, plan, library, "roms", manifest, policy=ops.WritePolicy(("roms",)), log=lines.append)
    assert (sd / "roms" / "MD" / "Sonic.md").read_bytes() == b"N" * 4000
    assert not (sd / "roms" / "Sonic.md").exists()
    assert any(line.startswith("not the same file") for line in lines)


def test_a_mega_checks_a_first_look_on_the_cart_without_reading(sd, tmp_path, monkeypatch):
    # F_FCRC, ~4 MB/s against ~700 KiB/s to read (2026-11-04): an SD card's worth of files.
    from syncforever import ops, romsync
    from syncforever.carts import MEGA_PRO
    from syncforever.n8pro import EverDrive
    from syncforever.protocol import Cmd

    monkeypatch.setattr(ops, "CRC_CHUNK", 1000)  # the big file takes several, chained
    (sd / "roms").mkdir(exist_ok=True)
    (sd / "roms" / "Big.md").write_bytes(bytes(range(250)) * 14)
    (sd / "roms" / "Hack.md").write_bytes(b"o" * 4000)
    library = tmp_path / "md"
    library.mkdir()
    (library / "Big.md").write_bytes(bytes(range(250)) * 14)
    (library / "Hack.md").write_bytes(b"N" * 4000)
    dev = EverDrive(FakeN8Pro(sd, profile=MEGA_PRO))
    dev.connect()
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    said = []
    plan = romsync.make_plan(
        dev, library, "roms", "update", manifest, progress=lambda m, d=None, left=None: said.append(m)
    )
    assert "Verifying 1/2" in said and "Verifying 2/2" in said
    assert Cmd.F_FCRC in dev.t.commands and Cmd.F_FRD not in dev.t.commands
    assert plan.unchanged == 1 and plan.replace == ["Hack.md"]
    # On record for next time, and for finding moves: the library's checksum.
    assert manifest.get("roms/Big.md")["sha"] == ops.sha256_file(library / "Big.md")
    assert manifest.get("roms/Hack.md") is None
    dev.t.commands.clear()
    romsync.make_plan(dev, library, "roms", "update", manifest)
    assert dev.t.commands.count(Cmd.F_FCRC) == 4  # Hack.md again; Big.md is on record


def test_library_hashes_from_before_crcs_still_work(tmp_path):
    from syncforever import ops, romsync

    f = tmp_path / "a.md"
    f.write_bytes(b"A" * 100)
    st = f.stat()
    sha, crc = ops.file_hashes(f)
    store = tmp_path / "library.json"
    store.write_text(json.dumps({str(f): [st.st_size, st.st_mtime_ns, sha]}))
    hashes = romsync.LibraryHashes(store)
    assert hashes.sha(f) == sha and not hashes.dirty  # no read for what's on record
    assert hashes.both(f) == (sha, crc) and hashes.dirty
    hashes.save()
    assert romsync.LibraryHashes(store).both(f) == (sha, crc)


def test_a_plan_counts_the_library_files_it_must_read(sd, tmp_path):
    # Files on record, their library checksums not kept: minutes over a network, under a status that
    # said only "Checking library" (2026-11-04).
    from syncforever import romsync
    from syncforever.carts import MEGA_PRO
    from syncforever.n8pro import EverDrive

    (sd / "roms").mkdir(exist_ok=True)
    library = tmp_path / "md"
    library.mkdir()
    for name in ("A.md", "B.md"):
        (sd / "roms" / name).write_bytes(b"x" * 100)
        (library / name).write_bytes(b"x" * 100)
    dev = EverDrive(FakeN8Pro(sd, profile=MEGA_PRO))
    dev.connect()
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    romsync.make_plan(dev, library, "roms", "update", manifest)
    hashes = manifest.path.with_name("cart-library.json")
    hashes.unlink()
    now = [0.0]

    def clock():
        now[0] += 2.0
        return now[0]

    said = []
    plan = romsync.make_plan(
        dev, library, "roms", "update", manifest, progress=lambda m, d=None, left=None: said.append((m, d)), clock=clock
    )
    assert ("Library 1/2", "A.md") in said and ("Library 2/2", "B.md") in said
    assert plan.unchanged == 2 and hashes.exists()
    said.clear()
    romsync.make_plan(
        dev, library, "roms", "update", manifest, progress=lambda m, d=None, left=None: said.append(m), clock=clock
    )
    assert not [m for m in said if m.startswith("Library")]  # kept


def test_a_slow_plan_counts_as_it_goes(sd, tmp_path):
    from syncforever import romsync
    from syncforever.carts import MEGA_PRO
    from syncforever.n8pro import EverDrive

    (sd / "roms").mkdir(exist_ok=True)
    library = tmp_path / "md"
    library.mkdir()
    for name in ("A.md", "B.md"):
        (library / name).write_bytes(b"x" * 100)
    dev = EverDrive(FakeN8Pro(sd, profile=MEGA_PRO))
    dev.connect()
    now = [0.0]

    def clock():
        now[0] += romsync.STATUS_EVERY
        return now[0]

    said = []
    romsync.make_plan(
        dev,
        library,
        "roms",
        "add",
        romsync.Manifest(tmp_path / "state" / "cart.json"),
        progress=lambda m, d=None, left=None: said.append((m, d)),
        clock=clock,
    )
    assert ("Scanning library", "2 ROMs") in said
    assert ("Reading the card", "0 folders") in said
    assert ("Checking library", "2 of 2") in said


def _card_copy(sd, tmp_path, names, *, differs=()):
    from syncforever import romsync
    from syncforever.carts import MEGA_PRO
    from syncforever.n8pro import EverDrive

    (sd / "roms").mkdir(exist_ok=True)
    library = tmp_path / "md"
    library.mkdir()
    for name in names:
        (library / name).write_bytes(name.encode() * 100)
        (sd / "roms" / name).write_bytes(b"?" * len(name) * 100 if name in differs else name.encode() * 100)
    dev = EverDrive(FakeN8Pro(sd, profile=MEGA_PRO))
    dev.connect()
    return dev, library, romsync.Manifest(tmp_path / "state" / "cart.json")


def test_first_look_trust_takes_files_unread_and_a_library_change_still_replaces(sd, tmp_path):
    from syncforever import ops, romsync
    from syncforever.protocol import Cmd

    dev, library, manifest = _card_copy(sd, tmp_path, ["A.md", "B.md"], differs=["B.md"])
    plan = romsync.make_plan(dev, library, "roms", "update", manifest, first_look="trust")
    assert Cmd.F_FCRC not in dev.t.commands and Cmd.F_FRD not in dev.t.commands
    assert plan.trusted == 2 and plan.unchanged == 2 and not plan.replace  # B.md's difference goes unseen
    assert manifest.get("roms/A.md")["sha"] == ops.sha256_file(library / "A.md")
    (library / "A.md").write_bytes(b"N" * (library / "A.md").stat().st_size)
    plan = romsync.make_plan(dev, library, "roms", "update", manifest, first_look="trust")
    assert plan.replace == ["A.md"]


@pytest.mark.parametrize(
    ("limit", "checks_all"),
    [
        (9e9, True),  # quick enough: every file is checked
        (0.0, False),  # too long, all the same: a sample, then the rest trusted
    ],
)
def test_first_look_auto_checks_when_its_quick_and_samples_when_its_not(sd, tmp_path, monkeypatch, limit, checks_all):
    from syncforever import romsync

    monkeypatch.setattr(romsync, "FIRST_LOOK_LIMIT", limit)
    monkeypatch.setattr(romsync, "SPOT_CHECK_SECS", 0.0)  # a sample of one
    names = [f"{c}.md" for c in "ABCDEF"]
    dev, library, manifest = _card_copy(sd, tmp_path, names)
    said = []
    plan = romsync.make_plan(
        dev, library, "roms", "update", manifest, progress=lambda m, d=None, left=None: said.append(m)
    )
    assert plan.unchanged == 6 and not plan.replace
    if checks_all:
        assert plan.trusted == 0 and "Verifying 6/6" in said and not plan.spot_checked
    else:
        assert plan.spot_checked == 1 and plan.trusted == 5 and "Spot check 1/1" in said
        assert not [m for m in said if m.startswith("Verifying")]


def test_first_look_auto_checks_everything_once_a_sample_differs(sd, tmp_path, monkeypatch):
    from syncforever import romsync

    monkeypatch.setattr(romsync, "FIRST_LOOK_LIMIT", 0.0)
    names = [f"{c}.md" for c in "ABCDEF"]  # SPOT_CHECK_SECS' worth is all of these
    dev, library, manifest = _card_copy(sd, tmp_path, names, differs=["D.md"])
    plan = romsync.make_plan(dev, library, "roms", "update", manifest)
    assert plan.spot_failed == "D.md" and plan.trusted == 0
    assert plan.replace == ["D.md"] and plan.unchanged == 5


def test_first_look_trust_moves_a_file_without_asking_the_cart(sd, tmp_path):
    from syncforever import ops, romsync
    from syncforever.protocol import Cmd

    (sd / "roms").mkdir(exist_ok=True)
    (sd / "roms" / "Sonic.md").write_bytes(b"S" * 4000)
    library = tmp_path / "md"
    (library / "MD").mkdir(parents=True)
    (library / "MD" / "Sonic.md").write_bytes(b"S" * 4000)
    from syncforever.carts import MEGA_PRO
    from syncforever.n8pro import EverDrive

    dev = EverDrive(FakeN8Pro(sd, profile=MEGA_PRO))
    dev.connect()
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, library, "roms", "strict", manifest, first_look="trust")
    assert plan.moves == [("roms/Sonic.md", "MD/Sonic.md")]
    dev.t.commands.clear()
    romsync.run_plan(dev, plan, library, "roms", manifest, policy=ops.WritePolicy(("roms",)))
    assert (sd / "roms" / "MD" / "Sonic.md").exists() and Cmd.F_FCRC not in dev.t.commands
