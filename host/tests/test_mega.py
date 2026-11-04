"""The client against a fake Mega EverDrive PRO: big-endian, STATUS [0xA5, code], its own
SYS_INF layout (docs/cartridge-compatibility.md, hardware 2026-11-01)."""

import io

import pytest

from syncforever import ops
from syncforever.carts import MEGA_PRO, N8_PRO
from syncforever.n8pro import EverDrive
from syncforever.protocol import Cmd, UnsafeStateError
from syncforever.sim import FakeN8Pro


@pytest.fixture
def mega(sd):
    (sd / "MEGA").mkdir()
    (sd / "Sonic.md").write_bytes(bytes(range(256)) * 2048)  # 512 KiB
    cart = FakeN8Pro(sd, profile=MEGA_PRO)
    d = EverDrive(cart)
    d.connect()
    return d, cart


def test_connect_picks_the_mega_profile(mega):
    d, _ = mega
    assert d.profile is MEGA_PRO
    assert (d.protocol, d.device) == (0x05, 0x18)


def test_status_and_numbers_are_big_endian(mega):
    d, cart = mega
    assert MEGA_PRO.status_reply(0) == b"\xa5\x00"
    assert d.status() == 0
    d.stat("/Sonic.md")
    # F_FINFO's path goes out with a big-endian length.
    assert MEGA_PRO.pack("H", 9) + b"/Sonic.md" in bytes(cart.sent)


def test_sys_info_uses_the_mega_layout(mega):
    d, _ = mega
    info = d.sys_info()
    assert info.device_id == 0x18
    assert info.serial == "12345678.9ABCDEF0"
    assert (info.boot_ctr, info.game_ctr) == (42, 7)
    assert info.cart_form == "genesis"


def test_hardware_sys_info_parses():
    """The 64 bytes our Mega EverDrive PRO sent (2026-11-01)."""
    raw = bytes.fromhex(
        "18befd2556fca928360073001451343036383237000527bd00003c8500000036"
        "000000265ab668f2598da6b30113000101031800020000000101170000000000"
    )
    at = MEGA_PRO.sysinf
    assert MEGA_PRO.unpack("IIII", raw, at.serial) == (0x000527BD, 0x00003C85, 54, 38)
    assert MEGA_PRO.unpack("H", raw, at.sw_ver) == (0x0113,)
    assert raw[at.device] == 0x18


def test_reads_work(mega):
    d, _ = mega
    names = [e.name for e in d.list_dir("/", restore=None)]
    assert "Sonic.md" in names and "MEGA" in names
    buf = io.BytesIO()
    r = ops.download(d, "Sonic.md", buf)
    assert r.size == 512 * 1024 and buf.getvalue() == bytes(range(256)) * 2048


def test_launch_uses_star_i_with_a_leading_slash(mega):
    d, cart = mega
    assert ops.launch(d, "Sonic.md") is None  # the Mega reports no mapper
    assert cart.running == "Sonic.md"
    assert b"*i" + MEGA_PRO.pack("H", 9) + b"/Sonic.md" in bytes(cart.sent)


def test_the_mega_menu_ignores_the_n8_install(mega):
    d, cart = mega
    d._fifo_wr(b"*n" + d._str("Sonic.md"))
    assert cart.read(1) == b""  # no reply, as on hardware


def test_writes_round_trip(mega):
    d, cart = mega
    assert d.menu_ping()
    data = bytes(range(256)) * 300
    r = ops.upload(d, io.BytesIO(data), "syncforever-test/x.md", policy=ops.WritePolicy(), total=len(data))
    assert (cart.root / "syncforever-test" / "x.md").read_bytes() == data
    ops.verify(d, "syncforever-test/x.md", r.sha256)
    assert d.delete("syncforever-test/x.md") and d.delete("syncforever-test")


def test_a_cart_without_writes_refuses_them(sd, tmp_path):
    import dataclasses

    cart = FakeN8Pro(sd, profile=MEGA_PRO)
    d = EverDrive(cart)
    d.connect()
    d.profile = dataclasses.replace(MEGA_PRO, writes=False)
    src = tmp_path / "x.md"
    src.write_bytes(b"x")
    with pytest.raises(UnsafeStateError, match="writing files isn't tested"), open(src, "rb") as f:
        ops.upload(d, f, "syncforever-test/x.md", policy=ops.WritePolicy())
    with pytest.raises(UnsafeStateError):
        d.delete("Sonic.md")


def test_the_n8_profile_is_unchanged(sd):
    d = EverDrive(FakeN8Pro(sd))
    d.connect()
    assert d.profile is N8_PRO
    assert N8_PRO.status_reply(0) == b"\x00\xa5"
    assert Cmd.STATUS in d.t.commands


def test_mega_sync_takes_every_sega_system(mega, tmp_path):
    from syncforever import romsync

    d, _cart = mega
    lib = tmp_path / "lib"
    files = [
        "megadrive/Sonic.md",
        "megadrive/Old.bin",
        "megadrive/Gen.gen",
        "megadrive/S.smd",
        "sega32x/Chaotix.32x",
        "mastersystem/Alex.sms",
        "gamegear/Shinobi.gg",
        "sg1000/Bank Panic.sg",
        "sg1000/Home.sc",
        "segacd/Sonic CD/Sonic CD.cue",
        "segacd/Sonic CD/Sonic CD (Track 1).bin",
        "segacd/Sonic CD/Sonic CD (Track 2).wav",
        "segacd/Lunar/Lunar.iso",
        "segacd/Snatcher/S.img",
        "nes/Zelda.nes",
        "notes.txt",
        "patch.ips",
    ]
    for f in files:
        (lib / f).parent.mkdir(parents=True, exist_ok=True)
        (lib / f).write_bytes(f.encode())
    plan = romsync.make_plan(d, lib, "syncforever-test", "add", romsync.Manifest(tmp_path / "m.json"))
    assert sorted(plan.add) == sorted(f for f in files if not f.endswith(".txt"))


def test_an_updated_app_is_restarted_by_resetting_into_the_menu(sd, tmp_path):
    """No pings into a booting menu (Error:2A/40 on hardware): reset, wait for `r`, launch."""
    from syncforever import appserver, romsync

    (sd / "syncforever-test").mkdir()
    (sd / "syncforever-test" / "Sync Forever.md").write_bytes(b"new app")
    cart = FakeN8Pro(sd, profile=MEGA_PRO, in_game=True)  # our (old) app is running
    d = EverDrive(cart)
    d.connect()
    now = [0.0]
    lib = tmp_path / "lib"
    lib.mkdir()
    server = appserver.AppServer(
        d,
        lib,
        dest="syncforever-test/sync",
        policy=ops.WritePolicy(),
        manifest=romsync.Manifest(tmp_path / "m.json"),
        update_app=lambda: True,
        app_path="syncforever-test/Sync Forever.md",
        clock=lambda: now[0],
        sleep=lambda s: None,
    )
    server.handle(appserver.HELLO, bytes([appserver.PROTO_VERSION]))
    assert server.pending and server.pending.reset
    pings_before = bytes(cart.sent).count(b"*t")
    now[0] = appserver.RESET_AFTER - 0.1
    server.poll(0)
    assert server.pending  # still showing "Updating..."
    now[0] = appserver.RESET_AFTER
    server.poll(0)
    assert server.pending is None and server.launched == ["syncforever-test/Sync Forever.md"]
    assert cart.running == "syncforever-test/Sync Forever.md"
    assert Cmd.HOST_RST in cart.commands
    assert bytes(cart.sent).count(b"*t") == pings_before + 1  # only ops.launch's check, after `r`


def test_reset_to_menu_needs_the_profile(sd):
    from syncforever.carts import N8_PRO

    d = EverDrive(FakeN8Pro(sd))
    d.connect()
    assert d.profile is N8_PRO
    with pytest.raises(UnsafeStateError, match="resetting to the menu isn't tested"):
        d.reset_to_menu()
