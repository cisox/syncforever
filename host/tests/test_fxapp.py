"""fxapp.py: the SNES app's link over cart memory, against the fake FXPAK and a model of the
app's side (fxsim.FakeSnesApp, the Python twin of snes/src/link_io.c)."""

import pytest

from syncforever import appserver, fxapp, ops, romsync
from syncforever.appserver import AppServer, Deframer, frame
from syncforever.fxapp import FxApp
from syncforever.fxpak import FxPak
from syncforever.fxsim import FakeFxPak, FakeSnesApp
from syncforever.protocol import LinkError


@pytest.fixture
def cart(tmp_path):
    root = tmp_path / "sd"
    (root / "sd2snes" / "saves").mkdir(parents=True)
    return FakeFxPak(root, rom="/Sync Forever.sfc")


class Clock:
    """Time that passes only when the box sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def box_for(cart):
    clock = Clock()
    return FxApp(FxPak(cart, timeout=0.1), clock=clock, sleep=clock.sleep)


def link(cart):
    app = FakeSnesApp(cart)
    app.start()
    return app, box_for(cart)


def messages(app):
    return Deframer().feed(app.received())


def test_frames_go_both_ways(cart):
    app, box = link(cart)
    app.send(frame(appserver.HELLO, b"\x07"))
    assert box.app_read() == frame(appserver.HELLO, b"\x07")
    assert box.app_read() == b""  # read once
    box.fifo_write(frame(appserver.WELCOME, b"\x02hi"))
    assert messages(app) == [(appserver.WELCOME, b"\x02hi")]


def test_rings_wrap(cart):
    app, box = link(cart)
    for i in range(40):  # 40 x 13 bytes: both rings go round twice
        app.send(frame(appserver.PING, bytes(8)))
        assert box.app_read() == frame(appserver.PING, bytes(8))
        box.fifo_write(frame(appserver.PONG, bytes([i]) * 8))
        assert messages(app) == [(appserver.PONG, bytes([i]) * 8)]


def test_a_box_restarted_mid_session_picks_up_where_the_cart_is(cart):
    app, box = link(cart)
    app.send(frame(appserver.PING))
    box.app_read()
    box.fifo_write(frame(appserver.PONG, b"\x01"))
    messages(app)
    again = box_for(cart)  # read its state from the cart
    app.send(frame(appserver.PING))
    assert again.app_read() == frame(appserver.PING)
    again.fifo_write(frame(appserver.PONG, b"\x00"))
    assert messages(app) == [(appserver.PONG, b"\x00")]


def test_the_app_restarting_skips_what_it_never_read(cart):
    app, box = link(cart)
    box.fifo_write(frame(appserver.STATUS, b"\x03old"))
    box.app_read()  # the box has seen this start
    app.start()  # reset: the old STATUS is gone, the box carries on
    box.fifo_write(frame(appserver.STATUS, b"\x03new"))
    box.app_read()
    assert messages(app) == [(appserver.STATUS, b"\x03new")]
    assert box.restarts == 1


def test_a_full_ring_waits_for_the_app_then_gives_up(cart):
    _app, box = link(cart)
    ticks = iter(range(10**6))
    box.clock = lambda: next(ticks) * 0.1
    with pytest.raises(LinkError):
        box.fifo_write(bytes(300))  # nobody reads


def test_app_server_over_the_rings(cart, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "Mario.sfc").write_bytes(b"M" * 2000)
    app, box = link(cart)
    server = AppServer(
        box,
        lib,
        dest="",
        policy=ops.WritePolicy(anywhere=True),
        manifest=romsync.Manifest(tmp_path / "m.json"),
        sync_mode="update",
    )
    app.send(frame(appserver.HELLO, bytes([appserver.PROTO_VERSION])))
    server.poll()
    assert messages(app) == [(appserver.WELCOME, appserver.text("Connected", appserver.TEXT_MAX))]
    app.send(frame(appserver.SYNC_PLAN, b"\xff"))
    server.poll()
    got = messages(app)
    assert got[-1][0] == appserver.PLAN
    app.send(frame(appserver.SYNC_RUN, bytes([got[-1][1][0], got[-1][1][-2]])))
    server.poll()
    assert messages(app)[-1][0] == appserver.SYNC_DONE
    assert (cart.root / "Mario.sfc").read_bytes() == b"M" * 2000


def server_for(box, tmp_path, **kw):
    return AppServer(
        box,
        None,
        dest="",
        policy=ops.WritePolicy(anywhere=True),
        manifest=romsync.Manifest(tmp_path / "m.json"),
        sleep=lambda s: None,
        **kw,
    )


def test_exit_resets_to_the_menu(cart, tmp_path):
    app, box = link(cart)
    server = server_for(box, tmp_path)
    app.send(frame(appserver.EXIT))
    server.poll()
    assert cart.rom == "/sd2snes/m3nu.bin"
    assert (12, "/") in cart.requests  # MENU_RESET


def test_an_updated_app_is_restarted_with_boot(cart, tmp_path):
    from syncforever import apps

    (cart.root / "Sync Forever.sfc").write_bytes(apps.packaged(".sfc"))
    app, box = link(cart)
    server = server_for(box, tmp_path, update_app=lambda: True, app_path="/Sync Forever.sfc")
    app.send(frame(appserver.HELLO, bytes([appserver.PROTO_VERSION])))
    server.poll()
    assert (9, "/Sync Forever.sfc") in cart.requests  # BOOT
    assert cart.rom == "/Sync Forever.sfc"
    # The reloaded ROM's inbox is zeros: the new copy starts, and the two sides agree again.
    app.start()
    app.send(frame(appserver.HELLO, bytes([appserver.PROTO_VERSION])))
    server.update_app = lambda: False
    server.poll()
    assert messages(app) == [(appserver.WELCOME, appserver.text("Connected", appserver.TEXT_MAX))]


@pytest.mark.parametrize(("newest", "state"), [("1.11.2", 1), ("1.12.0", 2), (None, 3)])
def test_the_fxpak_firmware_line(cart, tmp_path, newest, state):
    """The plan's firmware line: INFO's version, green (1) when it's sd2snes's newest tag, red (2)
    when there's newer, plain (3) when the newest isn't known."""
    from syncforever.box import Box
    from syncforever.config import Config

    box_ = Box(Config(state=tmp_path / "state", firmware=True))
    if newest:
        (tmp_path / "state" / "firmware").mkdir(parents=True)
        (tmp_path / "state" / "firmware" / "sd2snes.json").write_text(f'{{"checked": 9e99, "versions": ["{newest}"]}}')
    _app, box = link(cart)
    session = box_.cart(box.dev)
    session.fx_version = "1.11.2"
    assert session.sd2snes_line() == ("v1.11.2", state)


# -- the cart's autosave (fxapp's docstring): no PUT while one may be writing the SD card ----


def test_an_upload_waits_for_save_ram_to_settle(cart):
    app, box = link(cart)
    box.clock.sleep(fxapp.SETTLE)
    box.app_read()  # a first look
    app.send(frame(appserver.PING))
    box.app_read()  # seen: save RAM just changed
    seen = box.clock()
    assert seen < fxapp.SETTLE + fxapp.EARLY  # the ack went out at once
    box.dev.block_gap = 0
    box.open("big.sfc", ops.FA_WRITE | ops.FA_CREATE_ALWAYS, size=1 << 20)  # ~5 s: too long for EARLY
    assert box.clock() - seen >= fxapp.SETTLE
    box.dev.write(b"x" * (1 << 20))
    box.dev.close()
    assert (cart.root / "big.sfc").stat().st_size == 1 << 20


def test_a_message_right_after_a_change_goes_at_once(cart):
    app, box = link(cart)
    box.clock.sleep(fxapp.SETTLE)  # nothing pending from before
    app.send(frame(appserver.PING))
    box.app_read()
    before = box.clock()
    box.fifo_write(frame(appserver.PONG, b"\x00"))
    assert box.clock() - before < fxapp.EARLY
    assert messages(app) == [(appserver.PONG, b"\x00")]


def test_a_small_upload_fits_between_the_apps_hellos(cart):
    # Connecting, the app writes a HELLO every 2 s: save RAM is never still for SETTLE. The
    # app's own 32 KiB file still goes, in the second after a HELLO.
    app, box = link(cart)
    hellos = []

    def sleep(seconds):
        before = box.clock.now
        box.clock.now += seconds
        if int(box.clock.now / 2) != int(before / 2):  # every 2 s
            app.send(frame(appserver.HELLO, b"\x07"))
            hellos.append(box.clock.now)

    box.sleep = sleep
    box.open("Sync Forever.sfc", ops.FA_WRITE | ops.FA_CREATE_ALWAYS, size=32768)
    assert hellos and box.clock() - hellos[-1] < fxapp.EARLY - 32768 / fxapp.PUT_RATE
    box.dev.write(bytes(32768))
    box.dev.close()


def test_save_ram_that_never_settles_is_an_error_not_a_hang(cart):
    _app, box = link(cart)

    def sleep(seconds):
        box.clock.now += seconds
        cart.sram[100] ^= 1  # a change at every look

    box.sleep = sleep
    box.changed = (None, box.clock())
    with pytest.raises(LinkError, match="keeps changing"):
        box.open("big.sfc", ops.FA_WRITE | ops.FA_CREATE_ALWAYS, size=4 << 20)


def test_the_last_sessions_messages_in_save_ram_are_never_replayed(cart):
    # The cart reloads the app's .srm at start: the old ring is there until the app sets up.
    app = FakeSnesApp(cart)
    app.start()
    app.send(frame(appserver.SYNC_PLAN, b"\x03") + frame(appserver.SYNC_RUN, b"\x03"))  # last session
    cart.rom_mem[:] = bytes(len(cart.rom_mem))  # the ROM reloaded: the box's side is zeros
    box = box_for(cart)  # connects before the app has set up
    assert box.app_read() == b""
    app.start()  # the app sets up
    app.send(frame(appserver.HELLO, b"\x07"))
    assert box.app_read() == frame(appserver.HELLO, b"\x07")


def test_status_carries_how_long_a_big_copy_should_take(cart, tmp_path):
    import struct

    app, box = link(cart)
    server = AppServer(
        box, None, dest="", policy=ops.WritePolicy(anywhere=True), manifest=romsync.Manifest(tmp_path / "m.json")
    )
    server._progress("Copying 3 of 9", "zelda-3.pcm (44 MB)", expect=185, left=4000)
    ((kind, body),) = messages(app)
    assert kind == appserver.STATUS
    assert body.endswith(struct.pack("<HI", 185, 4000))


def test_stop_from_the_app_ends_the_sync_between_files(cart, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("a.sfc", "b.sfc", "c.sfc"):
        (lib / name).write_bytes(name.encode() * 50)
    app, box = link(cart)
    box.dev.block_gap = 0
    server = AppServer(
        box,
        lib,
        dest="",
        policy=ops.WritePolicy(anywhere=True),
        manifest=romsync.Manifest(tmp_path / "m.json"),
        sync_mode="update",
    )
    app.send(frame(appserver.SYNC_PLAN, b"\xff"))
    server.poll()
    plan = messages(app)[-1]
    assert plan[0] == appserver.PLAN and plan[1][-3] == appserver.MSU_OFF  # the FXPAK plays MSU-1
    statuses = []
    real = server._progress

    def progress(msg, detail=None, **kw):
        real(msg, detail, **kw)
        statuses.append(msg)
        if msg == "Copying 2 of 3":  # B was pressed during the first: STOP goes with this STATUS
            app.received()
            app.send(frame(appserver.STOP))

    server._progress = progress
    app.send(frame(appserver.SYNC_RUN, bytes([plan[1][0], plan[1][-2]])))
    server.poll()
    done = messages(app)
    assert any(k == appserver.SYNC_DONE and b"Stopped: 1/3 done" in body for k, body in done)
    assert (cart.root / "a.sfc").exists() and not (cart.root / "b.sfc").exists()


def test_the_apps_srm_moves_to_the_free_space_after_a_syncs_first_file(cart, tmp_path):
    # FatFs looks for free space from the place of the last file it overwrote: the autosave's
    # .srm. Deleted after the first copy, it's made again where the free space is (fxapp.py).
    lib = tmp_path / "lib"
    lib.mkdir()
    for name in ("a.sfc", "b.sfc"):
        (lib / name).write_bytes(name.encode() * 50)
    srm = cart.root / "sd2snes" / "saves" / "Sync Forever.srm"
    srm.write_bytes(b"SFE1" + bytes(2044))
    _app, box = link(cart)
    box.save_file = "sd2snes/saves/Sync Forever.srm"
    box.dev.block_gap = 0
    manifest = romsync.Manifest(tmp_path / "m.json")
    plan = romsync.make_plan(box, lib, "", "update", manifest)
    gone_after = []
    real = romsync.ops.verify

    def verify(dev, path, sha, **kw):
        gone_after.append((path, srm.exists()))
        return real(dev, path, sha, **kw)

    romsync.ops.verify, saved = verify, romsync.ops.verify
    try:
        romsync.run_plan(box, plan, lib, "", manifest, policy=ops.WritePolicy(anywhere=True))
    finally:
        romsync.ops.verify = saved
    assert gone_after == [("a.sfc", True), ("b.sfc", False)]  # deleted once, after the first file


def test_old_messages_arent_replayed_after_the_box_restarts_the_app(cart):
    (cart.root / "Sync Forever.sfc").write_bytes(bytes(0x8000))
    app, box = link(cart)
    app.send(frame(appserver.SYNC_PLAN, b"\x03") + frame(appserver.SYNC_RUN, b"\x03"))
    box.boot_app("Sync Forever.sfc")  # the ROM reloads; save RAM still holds the old ring
    assert box.app_read() == b""
    app.start()
    app.send(frame(appserver.HELLO, b"\x07"))
    assert box.app_read() == frame(appserver.HELLO, b"\x07")


def server_on(box, lib, tmp_path, mode="strict"):
    return AppServer(
        box,
        lib,
        dest="",
        policy=ops.WritePolicy(anywhere=True),
        manifest=romsync.Manifest(tmp_path / "m.json"),
        sync_mode=mode,
        clock=box.clock,
        sleep=box.sleep,
    )


def test_a_library_gone_while_the_app_is_open_never_plans_removals(cart, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.sfc").write_bytes(b"a")
    (cart.root / "b.sfc").write_bytes(b"b")
    app, box = link(cart)
    box.dev.block_gap = 0
    server = server_on(box, lib, tmp_path)
    app.send(frame(appserver.SYNC_PLAN, b"\xff"))
    server.poll()
    number = messages(app)[-1][1][-2]
    (lib / "a.sfc").unlink()  # the share drops off: an empty mount point
    (lib / "note").write_text("change the folder's time")
    (lib / "note").unlink()
    box.clock.sleep(appserver.LIBRARY_CHECK + 1)
    app.send(frame(appserver.PING))
    server.poll()
    got = messages(app)
    assert any(k == appserver.ERROR and b"Library empty/offline" in body for k, body in got), got
    assert not any(k == appserver.PLAN for k, _ in got)
    app.send(frame(appserver.SYNC_RUN, bytes([3, number])))  # A on the plan still on screen
    server.poll()
    assert any(k == appserver.ERROR and b"Library offline" in body for k, body in messages(app))
    assert (cart.root / "b.sfc").exists()
    (lib / "a.sfc").write_bytes(b"a")  # it's back
    box.clock.sleep(appserver.LIBRARY_CHECK + 1)
    app.send(frame(appserver.PING))
    server.poll()
    assert any(k == appserver.PLAN for k, _ in messages(app))


def test_a_sync_runs_only_the_plan_the_app_showed(cart, tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "a.sfc").write_bytes(b"a")
    app, box = link(cart)
    box.dev.block_gap = 0
    server = server_on(box, lib, tmp_path, mode="update")
    app.send(frame(appserver.SYNC_PLAN, b"\xff"))
    server.poll()
    plan = messages(app)[-1][1]
    app.send(frame(appserver.SYNC_RUN, bytes([plan[0], (plan[-2] + 1) & 0xFF])))  # an older plan
    server.poll()
    assert any(k == appserver.ERROR and b"Plan changed" in body for k, body in messages(app))
    assert not (cart.root / "a.sfc").exists()
    app.send(frame(appserver.SYNC_RUN, bytes([plan[0], plan[-2]])))
    server.poll()
    assert (cart.root / "a.sfc").exists()
