"""fxpak.py against the fake FXPAK (fxsim.py): the file interface ops.py and sync use."""

import io
from dataclasses import replace

import pytest

from syncforever import fxapp, ops, romsync
from syncforever.fxpak import FxPak, parse_ls, request
from syncforever.fxsim import CartHung, FakeFxPak
from syncforever.protocol import FR_EXIST, DeviceError, LinkError, WedgedError

ANYWHERE = ops.WritePolicy(anywhere=True)


@pytest.fixture
def card(tmp_path):
    root = tmp_path / "sd"
    (root / "sd2snes" / "saves").mkdir(parents=True)
    (root / "sd2snes" / "saves" / "Zelda.srm").write_bytes(b"\1" * 8192)
    (root / "SNES").mkdir()
    (root / "SNES" / "Zelda.sfc").write_bytes(b"Z" * 1000)
    return root


@pytest.fixture
def dev(card):
    return FxPak(FakeFxPak(card), timeout=0.1)


def test_info(dev):
    i = dev.info()
    assert (i.version, i.device, i.at_menu) == ("1.11.2", "FXPAK PRO STM32", True)


def test_list_and_stat(dev):
    assert {(e.name, e.is_dir) for e in dev.list_dir("/")} == {("sd2snes", True), ("SNES", True)}
    assert [(e.name, e.size) for e in dev.list_dir("SNES")] == [("Zelda.sfc", 1000)]
    assert dev.stat("snes/zelda.sfc").size == 1000  # FAT ignores case
    assert dev.stat("SNES").is_dir
    assert dev.stat("/").is_dir


def test_missing_files_never_get_a_get(dev):
    assert dev.stat("SNES/Nope.sfc") is None
    assert dev.stat("Nowhere/Nope.sfc") is None
    with pytest.raises(DeviceError):
        dev.open("SNES/Nope.sfc", ops.FA_READ)
    with pytest.raises(DeviceError):
        ops.download(dev, "SNES/Nope.sfc", io.BytesIO())
    assert not dev.delete("SNES/Nope.sfc")
    assert all(op != 0 or path != "/SNES/Nope.sfc" for op, path in dev.port.requests)


def test_the_fake_hangs_like_the_cart(card):
    port = FakeFxPak(card)
    with pytest.raises(CartHung):
        port.write(request(0, path="/nope.bin"))


@pytest.mark.parametrize("size", [0, 1, 511, 512, 513, 100_000])
def test_upload_download_verify(dev, card, size):
    data = bytes(i * 7 & 0xFF for i in range(size))
    up = ops.upload(dev, io.BytesIO(data), "SNES/new/Game.sfc", policy=ANYWHERE)
    assert (card / "SNES" / "new" / "Game.sfc").read_bytes() == data
    ops.verify(dev, "SNES/new/Game.sfc", up.sha256)
    assert dev.info().version == "1.11.2"  # still in step afterwards


def test_upload_blocks_are_paced(dev, card, monkeypatch):
    """Each data block is drained and followed by a gap: back to back, the cart's USB stalls."""
    events = []
    monkeypatch.setattr(dev.port, "flush", lambda: events.append("flush"))
    monkeypatch.setattr("syncforever.fxpak.time.sleep", lambda s: events.append(s))
    ops.upload(dev, io.BytesIO(bytes(1500)), "SNES/Game.sfc", policy=ANYWHERE)
    assert events == ["flush", 0.001] * 3  # 1500 bytes: three blocks


def test_create_new_refuses_an_existing_file(dev):
    with pytest.raises(DeviceError) as e:
        ops.upload(dev, io.BytesIO(b"x"), "SNES/Zelda.sfc", policy=ANYWHERE)
    assert e.value.code == FR_EXIST
    ops.upload(dev, io.BytesIO(b"new"), "SNES/Zelda.sfc", policy=ANYWHERE, overwrite=True)
    assert dev.stat("SNES/Zelda.sfc").size == 3


def test_a_short_write_is_padded_so_the_cart_stays_in_step(dev, card):
    dev.open("SNES/short.sfc", ops.FA_WRITE, size=2000)
    dev.write(b"a" * 700)
    with pytest.raises(LinkError):
        dev.close()
    assert (card / "SNES" / "short.sfc").stat().st_size == 2000
    assert dev.info().version == "1.11.2"


def test_a_read_stopped_early_is_still_read_to_the_end(dev):
    dev.open("sd2snes/saves/Zelda.srm", ops.FA_READ)
    assert dev.read(10) == b"\1" * 10
    dev.close()
    assert dev.port.out == b"" and dev.port.aborts == 0  # every block taken, DTR never dropped
    assert dev.info().device == "FXPAK PRO STM32"


def test_sizes_never_abort(dev):
    dev.list_dir("sd2snes/saves")
    dev.stat("SNES/Zelda.sfc")
    assert dev.port.aborts == 0


def test_mkdir_and_delete(dev, card):
    assert dev.mkdir("Tmp") and not dev.mkdir("Tmp")
    ops.upload(dev, io.BytesIO(b"x"), "Tmp/a.sfc", policy=ANYWHERE)
    with pytest.raises(DeviceError):
        dev.delete("Tmp")  # not empty
    assert dev.delete("Tmp/a.sfc") and dev.delete("Tmp")
    assert not (card / "Tmp").exists()


def test_long_folders_span_blocks(dev, card):
    for i in range(60):
        (card / "SNES" / f"A rather long game name number {i:02d} (USA).sfc").write_bytes(b"")
    names = [e.name for e in dev.list_dir("SNES", sizes=False)]
    assert len(names) == 61 and "A rather long game name number 59 (USA).sfc" in names


def test_parse_ls():
    block = bytes([0]) + b"gb\0" + bytes([1]) + b"a.sfc\0" + bytes([0xFF])
    assert parse_ls(block.ljust(512, b"\0")) == ([(True, "gb"), (False, "a.sfc")], True)


def test_names_are_code_page_1252():
    # The cart's FatFs is code page 1252: "é" is the one byte 0xE9 both ways (2026-11-03: a
    # UTF-8 path for a folder LS had listed got FR_NO_FILE).
    block = bytes([0]) + b"Rod M\xe9rida\0" + bytes([1]) + b"\x81odd.sfc\0" + bytes([0xFF])
    entries, _ = parse_ls(block.ljust(512, b"\0"))
    assert entries[0] == (True, "Rod Mérida")
    assert request(4, path=entries[0][1])[256:].split(b"\0")[0] == b"/Rod M\xe9rida"
    assert request(4, path=entries[1][1])[256:].split(b"\0")[0] == b"/\x81odd.sfc"  # undefined in 1252: kept as is
    with pytest.raises(ValueError, match="code page 1252"):
        request(4, path="ドラクエ.sfc")


def test_a_folder_with_an_accent_lists_and_syncs(dev, card, tmp_path):
    (card / "msu1" / "DQ3 [Add by Rod Mérida]").mkdir(parents=True)
    (card / "msu1" / "DQ3 [Add by Rod Mérida]" / "dq3.sfc").write_bytes(b"D" * 600)
    lib = tmp_path / "lib"
    (lib / "msu1" / "DQ3 [Add by Rod Mérida]").mkdir(parents=True)
    (lib / "msu1" / "DQ3 [Add by Rod Mérida]" / "dq3.sfc").write_bytes(b"D" * 600)
    (lib / "ドラクエ.sfc").write_bytes(b"J" * 10)
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, lib, "", "update", manifest)
    assert (plan.add, plan.unchanged, plan.skipped) == ([], 1, ["ドラクエ.sfc"])
    assert "skipped 1" in plan.summary()


def test_sd2snes_is_a_system_folder():
    with pytest.raises(ops.WriteNotAllowed):
        romsync.guard("sd2snes/saves/Zelda.srm")


def test_the_fake_shifts_a_get_after_one_cut_short(card):
    (card / "small.bin").write_bytes(b"s" * 300)
    port = FakeFxPak(card)

    def cut_short():
        port.write(request(0, path="/sd2snes/saves/Zelda.srm"))
        port.dtr = False
        port.dtr = True

    cut_short()
    port.write(request(0, path="/SNES/Zelda.sfc"))  # 1000 bytes
    assert len(port.out) == 512 + 512  # the response, then 488 bytes' worth: one block, not two
    cut_short()
    with pytest.raises(CartHung):
        port.write(request(0, path="/small.bin"))  # starts past its end: USB stops


# --- saves and the box session ---------------------------------------------------------------


def test_flat_game_names():
    from syncforever.saves import flat_game

    assert flat_game("Chrono Trigger (USA).srm") == "Chrono Trigger (USA)"
    assert flat_game("Secret of Mana Plus (Turbo)(USA)01.state") == "Secret of Mana Plus (Turbo)(USA)"
    assert flat_game("Pokemon - Gold (USA).gtc") == "Pokemon - Gold (USA)"
    assert flat_game("Mega Man X2.srm") == "Mega Man X2"  # digits only come off save states


class Stop(Exception):
    pass


def run_session(card, tmp_path, *, polls, roms=(), app_path="", port=None, tick=lambda: None):
    """The box's FXPAK session until `roms` (what INFO reports after each sleep) run out.
    app_path "": no Sync Forever app (the box then only reads)."""
    from syncforever.box import Box
    from syncforever.config import Config

    (tmp_path / "saves").mkdir(exist_ok=True)
    port = port or FakeFxPak(card)
    port.usb_serial = "DEMO00000000"
    logs, now = [], [0.0]
    seq = list(roms)

    def sleep(seconds):
        now[0] += seconds
        tick()
        if seconds <= fxapp.QUIET_POLL:  # the autosave gate's waits: not a poll
            return
        if not seq:
            raise Stop
        port.rom = seq.pop(0)

    cfg = Config(saves=tmp_path / "saves", state=tmp_path / "state", app_path=app_path)
    box = Box(cfg, log=logs.append, sleep=sleep)
    import syncforever.box as boxmod

    real = boxmod.time.monotonic
    boxmod.time.monotonic = lambda: now[0]
    try:
        with pytest.raises(Stop):
            box.fxpak_session(port)
    finally:
        boxmod.time.monotonic = real
    return port, logs


def test_session_backs_up_saves_states_and_never_writes(card, tmp_path):
    (card / "sd2snes" / "states").mkdir()
    (card / "sd2snes" / "states" / "Zelda01.state").write_bytes(b"S" * 5000)
    (card / "sd2snes" / "saves" / "Empty.srm").write_bytes(b"")
    port, logs = run_session(card, tmp_path, polls=0)
    assert any("saves (connected): checked 3 files, backed up 3" in m for m in logs), logs
    assert sorted(p.name for p in (tmp_path / "saves" / "Zelda").iterdir()) == ["Zelda.srm", "Zelda01.state"]
    assert {op for op, _ in port.requests} <= {0, 4, 11}  # GET, LS, INFO only
    assert port.aborts == 1  # only for the empty file's GET


def test_session_backs_up_again_after_the_game_changes(card, tmp_path):
    game = "/SNES/Zelda.sfc"
    menu = "/sd2snes/m3nu.bin"
    (card / "sd2snes" / "saves" / "Zelda.srm").write_bytes(b"\2" * 8192)  # played since
    _port, logs = run_session(card, tmp_path, polls=6, roms=[game, game, *[menu] * 8])
    assert any(f"now running {menu}" in m for m in logs)
    assert any("saves (game changed)" in m for m in logs), logs


def test_session_installs_the_app_at_connect(card, tmp_path):
    from syncforever import apps

    _port, logs = run_session(card, tmp_path, polls=0, app_path="/Sync Forever.nes")
    assert (card / "Sync Forever.sfc").read_bytes() == apps.packaged(".sfc")
    assert any("app installed at /Sync Forever.sfc" in m for m in logs), logs


def test_session_installs_the_app_only_at_the_menu(card, tmp_path):
    # In a game, the cart's autosave can meet an upload (fxapp.py): wait for the menu.
    from syncforever import apps

    game, menu = "/SNES/Zelda.sfc", "/sd2snes/m3nu.bin"
    port = FakeFxPak(card, rom=game)
    run_session(card, tmp_path, polls=0, roms=[game] * 3, app_path="/Sync Forever.nes", port=port)
    assert not (card / "Sync Forever.sfc").exists()
    assert all(op != 1 for op, _ in port.requests)  # no PUT
    port = FakeFxPak(card, rom=game)
    run_session(card, tmp_path, polls=0, roms=[game, menu, menu], app_path="/Sync Forever.nes", port=port)
    assert (card / "Sync Forever.sfc").read_bytes() == apps.packaged(".sfc")


def test_session_serves_the_app_while_it_runs(card, tmp_path):
    from syncforever import apps, appserver
    from syncforever.appserver import Deframer, frame
    from syncforever.fxsim import FakeSnesApp

    app_rom, menu = "/Sync Forever.sfc", "/sd2snes/m3nu.bin"
    (card / "Sync Forever.sfc").write_bytes(apps.packaged(".sfc"))  # current: no restart
    port = FakeFxPak(card, rom=app_rom)
    app = FakeSnesApp(port)
    app.start()
    said = []

    def hello():  # the app says HELLO until answered (every 2 s; here at the box's first wait)
        if not said:
            said.append(1)
            app.send(frame(appserver.HELLO, bytes([appserver.PROTO_VERSION])))

    _port, logs = run_session(
        card, tmp_path, polls=0, roms=[app_rom] * 30 + [menu] * 60, app_path="/Sync Forever.nes", port=port, tick=hello
    )
    got = Deframer().feed(app.received())
    assert got and got[0][0] == appserver.WELCOME
    assert any("Sync Forever app running" in m for m in logs), logs
    assert any(f"now running {menu}" in m for m in logs), logs


# -- ROM sync -------------------------------------------------------------------------------


@pytest.fixture
def library(tmp_path):
    lib = tmp_path / "lib"
    (lib / "RPG").mkdir(parents=True)
    (lib / "Zelda.sfc").write_bytes(b"Z" * 1000)  # the same as the card's SNES/Zelda.sfc
    (lib / "RPG" / "Chrono.smc").write_bytes(b"C" * 3000)
    (lib / "notes.txt").write_text("not a ROM")
    return lib


def gets(dev):
    return [path for op, path in dev.port.requests if op == 0]


def test_sync_reads_an_unknown_cart_rom_once_then_trusts_the_manifest(dev, card, library, tmp_path):
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    said = []
    plan = romsync.make_plan(
        dev, library, "SNES", "update", manifest, progress=lambda m, d=None, left=None: said.append((m, d))
    )
    assert (plan.add, plan.replace, plan.unchanged) == (["RPG/Chrono.smc"], [], 1)
    assert said == [("Verifying 1/1", "Zelda.sfc")]
    assert gets(dev) == ["/SNES/Zelda.sfc"]  # no sizes in LS: read once, size and hash together
    romsync.run_plan(dev, plan, library, "SNES", manifest, policy=ops.WritePolicy(("SNES",)))
    assert (card / "SNES" / "RPG" / "Chrono.smc").read_bytes() == b"C" * 3000
    dev.port.requests.clear()
    again = romsync.make_plan(dev, library, "SNES", "update", manifest)
    assert again.empty and again.unchanged == 2
    assert gets(dev) == []  # both known to the manifest: nothing read


def test_sync_replaces_a_changed_rom(dev, card, library, tmp_path):
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    romsync.run_plan(
        dev, romsync.make_plan(dev, library, "SNES", "update", manifest), library, "SNES", manifest, policy=ANYWHERE
    )
    (library / "RPG" / "Chrono.smc").write_bytes(b"c" * 3000)  # same size, new contents
    plan = romsync.make_plan(dev, library, "SNES", "update", manifest)
    assert plan.replace == ["RPG/Chrono.smc"]
    romsync.run_plan(dev, plan, library, "SNES", manifest, policy=ANYWHERE)
    assert (card / "SNES" / "RPG" / "Chrono.smc").read_bytes() == b"c" * 3000


def test_strict_sync_at_the_root_never_touches_sd2snes(dev, card, library, tmp_path):
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    (card / "sd2snes" / "m3nu.bin").write_bytes(b"M")
    (card / "sd2snes" / "old.sfc").write_bytes(b"O")
    plan = romsync.make_plan(dev, library, "", "strict", manifest)
    assert plan.remove == ["SNES/Zelda.sfc"]  # not in the library at that path
    assert plan.remove_dirs == ["SNES"]
    romsync.run_plan(dev, plan, library, "", manifest, policy=ANYWHERE)
    assert (card / "sd2snes" / "old.sfc").exists() and (card / "sd2snes" / "saves" / "Zelda.srm").exists()
    assert (card / "Zelda.sfc").exists() and not (card / "SNES").exists()


def test_cli_sync_shows_the_plan_and_copies_only_with_yes(card, library, tmp_path, capsys):
    from syncforever.cli import main

    base = ["--sim", str(card), "--fxpak", "sync", "--library", str(library), "--dest", "SNES"]
    base += ["--state", str(tmp_path / "state"), "--mode", "update"]
    assert main(base) == 0
    out = capsys.readouterr().out
    assert "add     RPG/Chrono.smc" in out and "pass --yes" in out
    assert not (card / "SNES" / "RPG").exists()
    assert main([*base, "--yes"]) == 0
    assert (card / "SNES" / "RPG" / "Chrono.smc").read_bytes() == b"C" * 3000
    assert (tmp_path / "state" / "cart-sim.json").exists()
    assert main(base) == 0
    assert "plan: add 0, replace 0, remove 0, unchanged 2" in capsys.readouterr().out


def test_cli_strict_sync_never_removes_the_app(card, library, tmp_path, capsys, monkeypatch):
    from syncforever import config
    from syncforever.cli import main

    monkeypatch.setattr(config, "SEARCH", (tmp_path / "none.toml",))
    monkeypatch.delenv("SFE_CONFIG", raising=False)
    (card / "Sync Forever.sfc").write_bytes(b"APP")
    base = ["--sim", str(card), "--fxpak", "sync", "--library", str(library), "--dest", "/"]
    assert main([*base, "--state", str(tmp_path / "state"), "--mode", "strict", "--yes"]) == 0
    assert "Sync Forever.sfc" not in capsys.readouterr().out
    assert (card / "Sync Forever.sfc").read_bytes() == b"APP"


def test_the_apps_own_srm_is_no_save(dev, card, tmp_path):
    from syncforever.saves import SaveStore, backup_flat

    (card / "sd2snes" / "saves" / "Sync Forever.srm").write_bytes(b"SFE1" + bytes(2044))
    results = backup_flat(dev, SaveStore(tmp_path / "store"))
    assert [r.file for r in results] == ["Zelda.srm"]


def test_an_upload_stall_says_so_and_closes_the_transfer(card):
    import serial

    class Stalls(FakeFxPak):
        def write(self, data):
            if self.put is not None and self.put.get("done", 0) >= 1024:
                raise serial.SerialTimeoutException("Write timeout")
            super().write(data)

    dev = FxPak(Stalls(card), timeout=0.1)
    with pytest.raises(WedgedError, match="stopped taking upload data"):
        ops.upload(dev, io.BytesIO(b"x" * 4096), "SNES/big.sfc", policy=ANYWHERE, total=4096)
    assert dev._write is None  # what follows isn't refused as "a transfer is still open"


# -- MSU-1: a ROM's .msu and -N.pcm go with it (romsync.make_plan's extras) -----------------


@pytest.fixture
def msu_library(tmp_path):
    lib = tmp_path / "msulib"
    (lib / "msu1" / "Zelda MSU").mkdir(parents=True)
    d = lib / "msu1" / "Zelda MSU"
    (d / "zelda.sfc").write_bytes(b"Z" * 1000)
    (d / "zelda.msu").write_bytes(b"M" * 100)
    (d / "zelda-1.pcm").write_bytes(b"P" * 5000)
    (d / "zelda-2.pcm").write_bytes(b"Q" * 6000)
    return lib


def test_msu1_files_sync_with_their_rom_and_are_never_read_back(dev, card, msu_library, tmp_path):
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, msu_library, "", "update", manifest, msu="msu1", msu_on=True)
    assert sorted(plan.add) == [f"msu1/Zelda MSU/{n}" for n in ("zelda-1.pcm", "zelda-2.pcm", "zelda.msu", "zelda.sfc")]
    dev.port.requests.clear()
    romsync.run_plan(dev, plan, msu_library, "", manifest, policy=ANYWHERE)
    d = card / "msu1" / "Zelda MSU"
    assert (d / "zelda-2.pcm").read_bytes() == b"Q" * 6000 and (d / "zelda.msu").read_bytes() == b"M" * 100
    assert gets(dev) == ["/msu1/Zelda MSU/zelda.sfc"]  # only the ROM read back
    again = romsync.make_plan(dev, msu_library, "", "update", manifest, msu="msu1", msu_on=True)
    assert again.empty and gets(dev) == ["/msu1/Zelda MSU/zelda.sfc"]  # nothing more read


def test_msu1_files_already_on_the_cart_are_trusted_by_name(dev, card, msu_library, tmp_path):
    d = card / "msu1" / "Zelda MSU"
    d.mkdir(parents=True)
    (d / "zelda-1.pcm").write_bytes(b"P" * 5000)  # put there by hand
    (d / "old-1.pcm").write_bytes(b"O" * 10)  # nothing in the library
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, msu_library, "", "strict", manifest, msu="msu1", msu_on=True)
    assert "msu1/Zelda MSU/zelda-1.pcm" not in plan.add + plan.replace
    assert "msu1/Zelda MSU/old-1.pcm" in plan.remove
    assert "/msu1/Zelda MSU/zelda-1.pcm" not in gets(dev)  # never read to check it
    (msu_library / "msu1" / "Zelda MSU" / "zelda-1.pcm").write_bytes(b"p" * 5001)  # a new version
    assert (
        "msu1/Zelda MSU/zelda-1.pcm"
        in romsync.make_plan(dev, msu_library, "", "strict", manifest, msu="msu1", msu_on=True).replace
    )


def test_a_games_msu1_files_go_before_its_rom(dev, msu_library, tmp_path):
    # A sync cut short never leaves a ROM on the cart without them.
    (msu_library / "Mario.sfc").write_bytes(b"M" * 10)
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, msu_library, "", "update", manifest, msu="msu1", msu_on=True)
    assert plan.add[-1] == "msu1/Zelda MSU/zelda.sfc"
    assert plan.add[0] == "Mario.sfc"


def test_with_msu1_off_msu1_games_are_left_out(dev, msu_library, tmp_path):
    (msu_library / "Mario.sfc").write_bytes(b"M" * 10)
    (msu_library / "msu1" / "Zelda MSU" / "zelda-x.pcm").write_bytes(b"x")  # not a track: no matter
    (msu_library / "Other.sfc").write_bytes(b"O" * 10)
    (msu_library / "Other-1.pcm").write_bytes(b"1")  # a track alone makes it an MSU-1 game
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, msu_library, "", "update", manifest, msu="msu1")
    assert plan.add == ["Mario.sfc"]
    assert sorted(plan.left_out) == ["Other.sfc", "msu1/Zelda MSU/zelda.sfc"]
    assert "left out 2 MSU games (msu is off)" in plan.summary()


def test_with_msu1_off_strict_leaves_msu1_games_on_the_cart(dev, card, msu_library, tmp_path):
    d = card / "msu1" / "Zelda MSU"
    d.mkdir(parents=True)
    (d / "zelda.sfc").write_bytes(b"Z" * 1000)  # copied by hand, with its files
    (d / "zelda-1.pcm").write_bytes(b"P")
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, msu_library, "", "strict", manifest, msu="msu1")
    assert plan.add == [] and plan.remove == ["SNES/Zelda.sfc"]  # the card's other ROM, not this one
    assert not any(p.startswith("msu1") for p in plan.remove_dirs)


def test_a_big_copy_shows_its_size(dev, tmp_path):
    lib = tmp_path / "big"
    lib.mkdir()
    (lib / "g.sfc").write_bytes(b"G")
    (lib / "g-1.pcm").write_bytes(bytes(romsync.BIG))
    dev.block_gap = 0
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, lib, "", "update", manifest, msu="msu1", msu_on=True)
    said = []
    romsync.run_plan(
        dev,
        plan,
        lib,
        "",
        manifest,
        policy=ANYWHERE,
        progress=lambda m, d=None, **kw: said.append((m, d, kw["expect"])),
    )
    assert said[0] == ("Copying 1 of 2", "g-1.pcm (16 MB)", round(16384 / 220) + romsync.EXPECT_SLACK)
    assert said[1][:2] == ("Copying 2 of 2", "g.sfc")


def test_replacing_is_counted_on_its_own(dev, card, library, tmp_path):
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    (card / "SNES" / "Zelda.sfc").write_bytes(b"z" * 999)  # differs from the library's
    plan = romsync.make_plan(dev, library, "SNES", "update", manifest)
    said = []
    romsync.run_plan(
        dev, plan, library, "SNES", manifest, policy=ANYWHERE, progress=lambda m, d=None, **kw: said.append(m)
    )
    assert said == ["Copying 1 of 1", "Replacing 1 of 1"]


def test_msu_md_games_are_found_by_their_cue(tmp_path):
    from syncforever import msu

    lib = tmp_path / "md"
    g = lib / "MSU" / "Castlevania (MSU-MD)"
    g.mkdir(parents=True)
    (g / "Castlevania (MSU-MD).md").write_bytes(b"R")
    (g / "Castlevania (MSU-MD).cue").write_text('FILE "Castlevania (MSU-MD).bin" BINARY\n  TRACK 01 AUDIO\n')
    (g / "Castlevania (MSU-MD).bin").write_bytes(b"A")
    cd = lib / "CD" / "Sonic CD"
    cd.mkdir(parents=True)
    (cd / "Sonic CD.bin").write_bytes(b"D")  # a Sega CD disc: its cue plays its own .bin
    (cd / "Sonic CD.cue").write_text('FILE "Sonic CD.bin" BINARY\n')
    (lib / "Sonic.md").write_bytes(b"S")  # no cue: a plain ROM
    files = romsync.library_roms(lib, ("",))
    found = msu.games("msu-md", lib, files, (".md", ".bin", ".cue"))
    rom = "msu/castlevania (msu-md)/castlevania (msu-md).md"
    assert list(found) == [rom]
    assert found[rom] == [
        "MSU/Castlevania (MSU-MD)/Castlevania (MSU-MD).bin",
        "MSU/Castlevania (MSU-MD)/Castlevania (MSU-MD).cue",
    ]


def test_msu1_games_need_a_msu_or_numbered_pcm(tmp_path):
    from syncforever import msu

    lib = tmp_path / "sfc"
    lib.mkdir()
    for name in ("a.sfc", "a.msu", "b.sfc", "b-1.pcm", "b-12.pcm", "c.sfc", "c-x.pcm", "cc-1.pcm"):
        (lib / name).write_bytes(b"x")
    found = msu.games("msu1", lib, romsync.library_roms(lib, ("",)), (".sfc",))
    assert found == {"a.sfc": ["a.msu"], "b.sfc": ["b-1.pcm", "b-12.pcm"]}


def test_a_folder_ending_in_a_dot_syncs_once(dev, card, tmp_path):
    # FAT drops trailing dots: the library's "Super Smash T.V." is "Super Smash T.V" on the card,
    # and plans must see it as the same folder (2026-11-03: it was planned again every time).
    lib = tmp_path / "lib"
    g = lib / "MSU1" / "Super Smash T.V."
    g.mkdir(parents=True)
    (g / "Smash.sfc").write_bytes(b"S" * 100)
    (lib / "Bad: name.sfc").write_bytes(b"B")  # FAT can't hold ':' at all
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, lib, "", "update", manifest)
    assert plan.add == ["MSU1/Super Smash T.V./Smash.sfc"] and plan.skipped == ["Bad: name.sfc"]
    romsync.run_plan(dev, plan, lib, "", manifest, policy=ANYWHERE)
    assert (card / "MSU1" / "Super Smash T.V" / "Smash.sfc").exists()
    again = romsync.make_plan(dev, lib, "", "strict", manifest)
    assert again.add == [] and again.replace == [] and not any("Smash" in p for p in again.remove)


def test_copies_are_logged_and_a_big_one_shows_it_is_moving(dev, tmp_path):
    lib = tmp_path / "big"
    lib.mkdir()
    (lib / "g.sfc").write_bytes(b"G" * 2048)
    (lib / "g-1.pcm").write_bytes(bytes(romsync.BIG))
    dev.block_gap = 0
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, lib, "", "update", manifest, msu="msu1", msu_on=True)
    ticks = iter(range(10**6))
    lines = []
    romsync.run_plan(dev, plan, lib, "", manifest, policy=ANYWHERE, log=lines.append, clock=lambda: next(ticks) * 1.0)
    assert lines[0] == "copying g-1.pcm (16.0 MB)"
    assert any(line.startswith("  g-1.pcm: ") and " of 16.0 MB, " in line for line in lines)
    assert any(line.startswith("copied g-1.pcm (16.0 MB) in ") and line.endswith("not read back") for line in lines)
    assert lines[-1].startswith("copied g.sfc (2 KB) in ") and lines[-1].endswith("checked")


def test_stop_during_a_files_check_finishes_the_file(dev, tmp_path):
    # The apps send STOP as a STATUS arrives. Not heard before the read, it would land in it.
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.sfc").write_bytes(bytes(3 << 20))
    (lib / "b.sfc").write_bytes(bytes(1 << 20))
    dev.block_gap = 0
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, lib, "", "add", manifest)
    said = []
    romsync.run_plan(
        dev,
        plan,
        lib,
        "",
        manifest,
        policy=ANYWHERE,
        progress=lambda m, d=None, **kw: said.append(m),
        stop=lambda: said[-1].startswith("Checking"),
    )
    assert said == ["Copying 1 of 2", "Checking 1 of 2"]
    assert plan.stopped == 1
    assert manifest.get("a.sfc")["size"] == 3 << 20  # read back and on record
    assert not (dev.port.root / "b.sfc").exists()


def test_a_failed_copy_says_how_far_it_got(dev, tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.sfc").write_bytes(b"a")
    (lib / "b.sfc").write_bytes(b"b")
    dev.block_gap = 0
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, lib, "", "add", manifest)
    real = romsync.ops.verify
    calls = []

    def fail_second(*a, **k):
        calls.append(a)
        if len(calls) == 2:
            raise romsync.ops.VerifyError("readback differs")
        return real(*a, **k)

    monkeypatch.setattr(romsync.ops, "verify", fail_second)
    with pytest.raises(romsync.ops.VerifyError):
        romsync.run_plan(dev, plan, lib, "", manifest, policy=ANYWHERE)
    assert (plan.done, plan.doing) == (1, "b.sfc")


def test_each_copy_carries_its_estimate_and_the_time_left(dev, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.sfc").write_bytes(bytes(3 << 20))  # 3 MB at 220 KiB/s up, 670 back
    (lib / "b.sfc").write_bytes(bytes(1 << 20))
    (dev.port.root / "old.sfc").write_bytes(b"o")
    dev.block_gap = 0
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, lib, "", "strict", manifest)
    said = []
    romsync.run_plan(
        dev, plan, lib, "", manifest, policy=ANYWHERE, progress=lambda m, d=None, **kw: said.append((m, d, kw))
    )
    a = round(3072 / 220) + romsync.EXPECT_SLACK  # its read-back has its own status
    gone = len(said) - 3  # the card's old ROMs and their folder
    assert said[0][:2] == ("Copying 1 of 2", "a.sfc") and said[0][2]["expect"] == a
    assert said[0][2]["left"] > a  # b.sfc and the removals still to come
    assert said[1][:2] == ("Checking 1 of 2", "a.sfc") and said[1][2]["expect"] == round(3072 / 670)
    assert said[2][0] == "Copying 2 of 2"  # about 1.5 s to read back: not worth a status
    assert said[3][0] == f"Removing 1 of {gone}" and said[-1][0] == f"Removing {gone} of {gone}"
    assert said[-1][2]["left"] == 1


def test_stop_ends_a_sync_before_the_next_file(dev, card, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("a.sfc", "b.sfc", "c.sfc"):
        (lib / name).write_bytes(name.encode() * 100)
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, lib, "", "update", manifest)
    asked = []
    romsync.run_plan(dev, plan, lib, "", manifest, policy=ANYWHERE, stop=lambda: asked.append(1) or len(asked) == 2)
    assert plan.stopped == 1
    assert (card / "a.sfc").exists() and not (card / "b.sfc").exists()
    assert romsync.make_plan(dev, lib, "", "update", manifest).add == ["b.sfc", "c.sfc"]  # carries on from there


def test_a_copy_cut_short_is_never_trusted(dev, card, msu_library, tmp_path, monkeypatch):
    # The cart creates the file, then the copy dies: an empty .pcm stays (2026-11-04). MSU files
    # on the cart are taken by name, so the manifest must say this one never finished.
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, msu_library, "", "update", manifest, msu="msu1", msu_on=True)
    real = ops.upload

    def dies(dev_, f, path, **kw):
        if path.endswith("-2.pcm"):
            (card / path).write_bytes(b"")  # created, never filled
            raise ops.WedgedError("stalled")
        return real(dev_, f, path, **kw)

    monkeypatch.setattr(ops, "upload", dies)
    with pytest.raises(ops.WedgedError):
        romsync.run_plan(dev, plan, msu_library, "", manifest, policy=ANYWHERE)
    monkeypatch.setattr(ops, "upload", real)
    again = romsync.make_plan(dev, msu_library, "", "update", manifest, msu="msu1", msu_on=True)
    assert "msu1/Zelda MSU/zelda-2.pcm" in again.add + again.replace


def test_the_speeds_a_cart_shows_are_remembered(dev, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.sfc").write_bytes(bytes(2 << 20))
    dev.block_gap = 0
    path = tmp_path / "state" / "cart.json"
    manifest = romsync.Manifest(path)
    ticks = iter(range(10**6))
    plan = romsync.make_plan(dev, lib, "", "update", manifest)
    romsync.run_plan(dev, plan, lib, "", manifest, policy=ANYWHERE, clock=lambda: next(ticks) * 0.01)
    again = romsync.Speeds(dev, romsync.Manifest(path))  # the next sync, maybe days later
    assert again.saved["up"][0] == 2 << 20 and again.rate("up") != dev.upload_kib_s
    assert again.saved["around"][0] == 1


def test_a_plan_counts_its_msu_games(dev, card, msu_library, tmp_path):
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, msu_library, "", "update", manifest, msu="msu1", msu_on=True)
    assert plan.msu_games == 1
    assert romsync.make_plan(dev, msu_library, "", "update", manifest, msu="msu1").msu_games == 0


def test_library_checksums_are_kept_while_a_file_is_unchanged(dev, card, library, tmp_path, monkeypatch):
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    romsync.make_plan(dev, library, "SNES", "update", manifest)  # Zelda.sfc: on both, compared
    hashed = []
    real = ops.file_hashes
    monkeypatch.setattr(ops, "file_hashes", lambda p: hashed.append(p.name) or real(p))
    romsync.make_plan(dev, library, "SNES", "update", manifest)
    assert hashed == []  # remembered (cart-library.json beside the manifest)
    (library / "Zelda.sfc").write_bytes(b"z" * 1000)  # same size, new contents and time
    plan = romsync.make_plan(dev, library, "SNES", "update", manifest)
    assert hashed == ["Zelda.sfc"] and plan.replace == ["Zelda.sfc"]


# -- never mistake a missing library for an empty library, or a system folder for another -------


@pytest.mark.parametrize("mode", ["mirror", "strict"])
def test_an_empty_library_plans_no_removals(dev, tmp_path, mode):
    empty = tmp_path / "unmounted"
    empty.mkdir()
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    with pytest.raises(ValueError, match="Library empty/offline"):
        romsync.make_plan(dev, empty, "", mode, manifest)


def test_a_folder_that_cant_be_read_stops_the_plan(dev, library, tmp_path, monkeypatch):
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    real = romsync.os.walk

    def walk(root, **kw):
        kw["onerror"](PermissionError(13, "denied", str(root)))
        return real(root)

    monkeypatch.setattr(romsync.os, "walk", walk)
    with pytest.raises(PermissionError):
        romsync.make_plan(dev, library, "", "strict", manifest)


def test_a_library_gone_mid_sync_removes_nothing(dev, card, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.sfc").write_bytes(b"a")
    (card / "old.sfc").write_bytes(b"o")
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, lib, "", "strict", manifest)
    assert "old.sfc" in plan.remove
    plan.add.clear()
    (lib / "a.sfc").unlink()  # the share went away between the plan and the removals
    with pytest.raises(ValueError, match="none removed"):
        romsync.run_plan(dev, plan, lib, "", manifest, policy=ANYWHERE)
    assert (card / "old.sfc").exists()


@pytest.mark.parametrize("path", ["sd2snes./x.sfc", "SD2SNES /x.sfc", "EDN8./x.nes", "ed64../x.z64"])
def test_a_trailing_dot_cant_reach_a_system_folder(path):
    with pytest.raises(ops.WriteNotAllowed):
        romsync.guard(path)


def test_a_library_folder_that_lands_on_a_system_folder_is_skipped(dev, card, tmp_path):
    lib = tmp_path / "lib"
    (lib / "sd2snes.").mkdir(parents=True)
    (lib / "sd2snes." / "evil.sfc").write_bytes(b"x")
    (lib / "good.sfc").write_bytes(b"g")
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, lib, "", "update", manifest)
    assert plan.add == ["good.sfc"] and plan.skipped == ["sd2snes./evil.sfc"]


# -- moves: a file renamed in the library is renamed on the card, not copied again -----------


def sync_all(dev, lib, manifest, mode="strict"):
    plan = romsync.make_plan(dev, lib, "", mode, manifest)
    romsync.run_plan(dev, plan, lib, "", manifest, policy=ANYWHERE)
    return plan


def test_a_renamed_rom_is_renamed_on_the_card(dev, card, tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "profile", replace(dev.profile, move="rename"))
    lib = tmp_path / "lib"
    (lib / "SNES").mkdir(parents=True)
    (lib / "SNES" / "Zelda.sfc").write_bytes(b"Z" * 1000)
    (lib / "SNES" / "Mario.sfc").write_bytes(b"M" * 2000)
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    dev.block_gap = 0
    sync_all(dev, lib, manifest)
    (lib / "SNES" / "Mario.sfc").rename(lib / "SNES" / "Super Mario World (USA).sfc")
    dev.port.requests.clear()
    plan = romsync.make_plan(dev, lib, "", "strict", manifest)
    assert plan.moves == [("SNES/Mario.sfc", "SNES/Super Mario World (USA).sfc")]
    assert plan.add == [] and plan.remove == [] and "move 1" in plan.summary()
    romsync.run_plan(dev, plan, lib, "", manifest, policy=ANYWHERE)
    assert (card / "SNES" / "Super Mario World (USA).sfc").read_bytes() == b"M" * 2000
    assert not (card / "SNES" / "Mario.sfc").exists()
    assert not any(op == 1 for op, _ in dev.port.requests)  # no upload
    assert romsync.make_plan(dev, lib, "", "strict", manifest).empty


def test_the_fxpak_moves_only_within_a_folder(dev, card, tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "profile", replace(dev.profile, move="rename"))
    lib = tmp_path / "lib"
    (lib / "A").mkdir(parents=True)
    (lib / "A" / "game.sfc").write_bytes(b"G" * 3000)
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    dev.block_gap = 0
    sync_all(dev, lib, manifest)
    (lib / "B").mkdir()
    (lib / "A" / "game.sfc").rename(lib / "B" / "game.sfc")
    plan = romsync.make_plan(dev, lib, "", "strict", manifest)
    assert plan.moves == [] and plan.add == ["B/game.sfc"]  # copied: MV can't change folders


def test_no_moves_until_a_carts_rename_is_tried(dev, card, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.sfc").write_bytes(b"A" * 500)
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    dev.block_gap = 0
    sync_all(dev, lib, manifest)
    (lib / "a.sfc").rename(lib / "b.sfc")
    plan = romsync.make_plan(dev, lib, "", "strict", manifest)
    assert plan.moves == [] and plan.add == ["b.sfc"] and plan.remove == ["a.sfc"]


def test_a_file_the_box_never_read_is_never_taken_for_a_move(dev, card, tmp_path, monkeypatch):
    monkeypatch.setattr(dev, "profile", replace(dev.profile, move="rename"))
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "new.sfc").write_bytes(b"N" * 700)
    (card / "old.sfc").write_bytes(b"N" * 700)  # same contents, but copied by hand: no record
    manifest = romsync.Manifest(tmp_path / "state" / "cart.json")
    plan = romsync.make_plan(dev, lib, "", "strict", manifest)
    assert plan.moves == [] and "new.sfc" in plan.add


def test_a_long_verify_keeps_the_app_informed_except_during_an_fxpak_get(dev, card, tmp_path):
    from syncforever.n8pro import EverDrive
    from syncforever.sim import FakeN8Pro

    lib = tmp_path / "lib"
    lib.mkdir()
    big = bytes(1 << 20)
    (lib / "big.sfc").write_bytes(big)
    (lib / "big.nes").write_bytes(big)
    (card / "big.sfc").write_bytes(big)
    (card / "big.nes").write_bytes(big)
    ticks = iter(range(10**6))
    clock = lambda: next(ticks) * 3.0
    said = []
    progress = lambda m, d=None, left=None: said.append((m, d))
    romsync.make_plan(dev, lib, "", "update", romsync.Manifest(tmp_path / "a.json"), progress=progress, clock=clock)
    assert not any(d and d.endswith("%") for _, d in said)  # FXPAK: one GET, nothing mid-read
    said.clear()
    n8 = EverDrive(FakeN8Pro(card))
    n8.connect()
    romsync.make_plan(n8, lib, "", "update", romsync.Manifest(tmp_path / "b.json"), progress=progress, clock=clock)
    assert any(m == "Verifying 1/1" and d and d.startswith("big.nes ") and d.endswith("%") for m, d in said), said
