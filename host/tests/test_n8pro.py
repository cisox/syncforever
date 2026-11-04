import pytest

from syncforever.n8pro import N8Pro
from syncforever.protocol import (
    DIR_OPT_SORTED,
    FA_CREATE_NEW,
    FA_READ,
    FA_WRITE,
    MENU_DIR_OPTS,
    Cmd,
    DeviceError,
    LinkError,
    WedgedError,
    frame,
)
from syncforever.sim import FakeN8Pro


def test_connect_identifies_n8_pro(dev):
    assert (dev.protocol, dev.device) == (0x06, 0x17)


def test_connect_recovers_from_half_sent_header(sim):
    sim.write(frame(Cmd.F_FOPN)[:3])  # a previous run died mid-header
    dev = N8Pro(sim)
    dev.connect()
    assert dev.status() == 0


def test_connect_to_other_cart_reports_ids_and_raw_status(sd):
    dev = N8Pro(FakeN8Pro(sd, protocol=0x05))
    with pytest.raises(LinkError, match=r"protocol 0x05, device 0x17, status reply 00a5"):
        dev.connect()


def test_sys_info_parses_firmware_version(dev):
    info = dev.sys_info()
    assert (info.sw_ver, info.boot_ver, info.device_id) == (0x0103, 0x0102, 0x17)
    assert str(info.built) == "2025-09-30 16:36:14"
    assert info.pwr_sys and info.pwr_usb


def test_sys_info_decodes_bytes_captured_from_real_cart():
    raw = bytes.fromhex(
        "173d52edf12c5b24080043001950323758303820104e0500864e00005a0000000d000000"
        "3e5b878403010100020117000000000000010117255a080d00000000"
    )

    class Replay:  # answers any request with the captured SYS_INF reply
        def write(self, data):
            pass

        def read(self, n):
            return raw[:n]

    info = N8Pro(Replay()).sys_info()
    assert (info.sw_ver, info.hw_ver, info.boot_ver, info.device_id) == (0x0103, 0x0001, 0x0102, 0x17)
    assert info.serial == "00054E10.00004E86"
    assert (info.boot_ctr, info.game_ctr, info.cart_form) == (90, 13, "nes")
    assert str(info.built) == "2025-09-30 16:36:14"
    assert info.pwr_sys and info.pwr_usb


def test_status_is_the_sticky_result_of_the_last_command(dev):
    assert dev.status() == 0
    assert dev.delete("no/such/path.bin") is False
    assert dev.status() == 0x05  # FR_NO_PATH, and it stays
    assert dev.status() == 0x05
    dev.stat("readme.txt")
    assert dev.status() == 0


def test_menu_ping_answers_only_at_the_menu(dev, sd):
    assert dev.menu_ping() is True
    in_game = N8Pro(FakeN8Pro(sd, in_game=True))
    in_game.connect()
    assert in_game.menu_ping() is False
    assert in_game.t.fifo == bytearray(b"*t")  # left unread, as on the real cart
    assert in_game.status() == 0x05  # link still usable afterwards


def test_list_dir_sorts_folders_first(dev):
    names = [(e.name, e.is_dir) for e in dev.list_dir("/")]
    assert names == [("roms", True), ("saves", True), ("readme.txt", False)]


def test_list_dir_pages_in_blocks_of_32(dev, sim, sd):
    for i in range(70):
        (sd / "saves" / f"g{i:03}.sav").write_bytes(b"x")
    entries = dev.list_dir("saves", restore=None)
    assert [e.name for e in entries] == [f"g{i:03}.sav" for i in range(70)]
    assert sim.commands.count(Cmd.F_DIR_GET) == 3


def test_sim_starts_with_menu_root_loaded(sim):
    assert sim.loaded_dir == ("", MENU_DIR_OPTS)


def test_list_dir_restores_the_menus_folder(dev, sim):
    # The menu and USB share one loaded-directory slot; leaving ours there makes the
    # menu show Error:81 on its next scroll (seen 2026-10-31).
    dev.list_dir("saves")
    assert sim.loaded_dir == ("", MENU_DIR_OPTS)


def test_read_snapshot_shows_what_the_menu_shows(dev, sim):
    sim.menu_open("roms")
    assert [e.name for e in dev.read_snapshot()] == ["nes"]
    assert sim.loaded_dir == ("roms", MENU_DIR_OPTS)  # reading did not load anything


def test_list_dir_restores_the_subfolder_the_menu_shows(dev, sim):
    sim.menu_open("roms/nes")
    dev.list_dir("saves")
    assert sim.loaded_dir == ("roms/nes", MENU_DIR_OPTS)


def test_list_dir_restores_the_system_folder(dev, sim, sd):
    (sd / "EDN8" / "sysdata").mkdir(parents=True)
    (sd / "EDN8" / "nesos.nes").write_bytes(b"menu")
    sim.menu_open("EDN8")
    dev.list_dir("/")
    assert sim.loaded_dir == ("EDN8", MENU_DIR_OPTS)


def test_list_dir_of_the_menus_own_folder_restores_without_searching(dev, sim):
    sim.menu_open("saves")
    dev.list_dir("/saves/")
    assert sim.loaded_dir == ("saves", MENU_DIR_OPTS)
    assert sim.commands.count(Cmd.F_DIR_LD) == 2  # the listing, then the restore


def test_list_dir_tells_same_named_folders_apart_by_size(dev, sim, sd):
    for game, size in (("A.nes", 10), ("B.nes", 20)):
        (sd / "EDN8" / "gamedata" / game).mkdir(parents=True)
        (sd / "EDN8" / "gamedata" / game / "bram.srm").write_bytes(bytes(size))
    sim.menu_open("EDN8/gamedata/B.nes")
    dev.list_dir("/")
    assert sim.loaded_dir == ("EDN8/gamedata/B.nes", MENU_DIR_OPTS)


def test_list_dir_falls_back_to_root_when_the_menus_folder_is_unknown(dev, sim, sd):
    (sd / "gone").mkdir()
    (sd / "gone" / "x.nes").write_bytes(b"x")
    sim.menu_open("gone")
    (sd / "gone" / "x.nes").unlink()
    (sd / "gone").rmdir()
    dev.list_dir("saves")
    assert sim.loaded_dir == ("", MENU_DIR_OPTS)


def test_list_dir_restores_a_given_folder(dev, sim):
    dev.list_dir("/", restore="roms")
    assert sim.loaded_dir == ("roms", MENU_DIR_OPTS)


def test_list_dir_restore_none_leaves_ours(dev, sim):
    dev.list_dir("saves", restore=None)
    assert sim.loaded_dir == ("saves", DIR_OPT_SORTED)


def test_list_dir_restores_after_error(dev, sim):
    with pytest.raises(DeviceError):
        dev.list_dir("nope")
    assert sim.loaded_dir == ("", MENU_DIR_OPTS)


def test_list_dir_shows_system_folder(dev, sd):
    (sd / "EDN8").mkdir()
    assert "EDN8" in [e.name for e in dev.list_dir("/")]


def test_sim_hidesys_hides_edn8_at_root(dev, sim, sd):
    (sd / "EDN8").mkdir()
    dev.list_dir("/", restore=None)  # loads with SORTED only
    dev._load_dir("", MENU_DIR_OPTS)
    dev._tx(frame(Cmd.F_DIR_SIZE))
    assert dev._rx16() == 3  # roms, saves, readme.txt


def test_list_dir_missing_folder_raises(dev):
    with pytest.raises(DeviceError, match="FR_NO_PATH"):
        dev.list_dir("nope")


def test_stat_existing_and_missing(dev):
    e = dev.stat("roms/nes/Zelda.nes")
    assert (e.name, e.size, e.is_dir) == ("Zelda.nes", 40960, False)
    assert e.mtime is not None
    assert dev.stat("roms/nes/Missing.nes") is None
    assert dev.stat("nowhere/Missing.nes") is None


@pytest.mark.parametrize("n", [1, 4095, 4096, 4097, 40960])
def test_read_spans_4k_status_blocks(dev, sd, n):
    dev.open("roms/nes/Zelda.nes", FA_READ)
    assert dev.read(n) == (sd / "roms/nes/Zelda.nes").read_bytes()[:n]
    dev.close()


@pytest.mark.parametrize("n", [1, 512, 1023, 1024, 1025, 1536, 5000])
def test_write_round_trips_across_ack_blocks(dev, sim, sd, n):
    data = bytes((i * 7) & 0xFF for i in range(n))
    dev.open("out.bin", FA_WRITE | FA_CREATE_NEW)
    sim.write_sizes.clear()
    dev.write(data)
    dev.close()
    assert (sd / "out.bin").read_bytes() == data
    assert 512 not in sim.write_sizes  # mirrors edlink's TxData quirk


def test_open_missing_file_raises_device_error(dev):
    with pytest.raises(DeviceError, match="FR_NO_FILE"):
        dev.open("missing.bin", FA_READ)


def test_create_new_refuses_existing(dev):
    with pytest.raises(DeviceError, match="FR_EXIST"):
        dev.open("readme.txt", FA_WRITE | FA_CREATE_NEW)


def test_mkdir_and_delete_report_whether_anything_changed(dev, sd):
    assert dev.mkdir("t") is True
    assert dev.mkdir("t") is False
    assert dev.delete("t") is True
    assert dev.delete("t") is False
    assert not (sd / "t").exists()


def test_delete_non_empty_folder_is_refused(dev):
    with pytest.raises(DeviceError, match="FR_DENIED"):
        dev.delete("roms")


def test_write_cut_short_wedges_and_refuses_further_commands(dev, sim, sd):
    dev.open("out.bin", FA_WRITE | FA_CREATE_NEW)
    sim.fail_writes_after = 4 + 4 + 1500  # header, length, then part of the payload
    with pytest.raises(WedgedError):
        dev.write(bytes(4096))
    with pytest.raises(WedgedError):
        dev.status()
    # Re-plug USB without power-cycling. The fake cart, like the real one, is
    # still waiting for payload: a fresh client's bytes are swallowed as file data.
    sim.fail_writes_after = None
    other = N8Pro(sim)
    with pytest.raises(LinkError):
        other.connect()


def test_read_timeout_resyncs_on_next_command(dev, sim, monkeypatch):
    real_read = sim.read
    monkeypatch.setattr(sim, "read", lambda n: b"")
    with pytest.raises(LinkError):
        dev.status()
    monkeypatch.setattr(sim, "read", real_read)
    assert dev.status() == 0


def _ines(mapper):
    return b"NES\x1a" + bytes([2, 1, (mapper & 0x0F) << 4, mapper & 0xF0]) + bytes(8) + bytes(0x8000)


def test_menu_install_returns_mapper_and_start_runs_the_game(dev, sim, sd):
    (sd / "Eyes.nes").write_bytes(_ines(4))
    assert dev.menu_install("Eyes.nes") == 4
    assert not sim.in_game
    dev.menu_start()
    assert sim.in_game and sim.running == "Eyes.nes"
    assert dev.menu_ping() is False


def test_menu_install_missing_rom_raises(dev, sim):
    with pytest.raises(DeviceError, match="install"):
        dev.menu_install("nope.nes")
    assert not sim.in_game
