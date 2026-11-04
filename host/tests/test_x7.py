"""x7.py against the fake X7 (x7sim.py): booting from the menu, the file service, and the shared
ops/saves code running on it unchanged."""

import io
import os
import struct

import pytest

from syncforever import ops, saves, x7
from syncforever.protocol import FA_READ, DeviceError, LinkError
from syncforever.x7sim import FakeX7

ROM = bytes(range(256)) * 700  # 175 KB, under the 1 MB + 4 KB the menu wants filled


@pytest.fixture(autouse=True)
def _no_settle(monkeypatch):
    monkeypatch.setattr(x7, "BOOT_SETTLE", 0)


@pytest.fixture
def card(tmp_path):
    root = tmp_path / "sd"
    (root / "ED64" / "gamedata").mkdir(parents=True)
    (root / "N64").mkdir()
    (root / "N64" / "Paper Mario (USA).z64").write_bytes(b"P" * 1000)
    (root / "ED64" / "gamedata" / "Paper Mario (USA).fla").write_bytes(b"F" * 131072)
    (root / "ED64" / "gamedata" / "Super Mario 64 (USA).eep").write_bytes(b"E" * 512)
    return root


@pytest.fixture
def app(card):
    return x7.connect(FakeX7(card, state="app"))


def test_boots_the_app_from_the_menu(card):
    fake = FakeX7(card)
    logs = []
    app = x7.connect(fake, ROM, log=logs.append)
    assert fake.booted[: len(ROM)] == ROM and len(fake.booted) % 512 == 0
    assert app.hello.version == "0.1"
    assert logs == ["booting the app from the menu", "the app started (v0.1)"]


def test_uses_the_app_already_running(card):
    fake = FakeX7(card, state="app")
    fake.out.clear()  # its startup HELLO went unread
    app = x7.connect(fake, ROM)
    assert fake.booted is None and app.hello.proto == x7.PROTO


def test_a_running_game_answers_nothing(card):
    with pytest.raises(LinkError, match="power-cycle"):
        x7.connect(FakeX7(card, state="game"), ROM)


def test_at_the_menu_without_a_rom(card):
    with pytest.raises(LinkError, match="no app ROM"):
        x7.connect(FakeX7(card), None)


def test_stat_and_list(app):
    e = app.stat("/ED64/gamedata/Paper Mario (USA).fla")
    assert (e.name, e.size, e.is_dir) == ("Paper Mario (USA).fla", 131072, False)
    assert e.mtime is not None
    assert app.stat("ED64/gamedata/nope.eep") is None
    assert app.stat("no/such/folder") is None
    names = [(e.name, e.is_dir) for e in app.list_dir("/")]
    assert names == [("ED64", True), ("N64", True)]


def test_list_pages_when_a_folder_outgrows_one_reply(app, card, monkeypatch):
    monkeypatch.setattr(x7, "MAX_DATA", 300)
    for i in range(40):
        (card / "N64" / f"Game {i:02d}.z64").write_bytes(b"x")
    names = [e.name for e in app.list_dir("N64")]
    assert len(names) == 41 and names == sorted(names)
    assert app.t.requests.count(x7.OP_LIST) > 3


def test_put_get_mkdir_rm_through_ops(app, card):
    policy = ops.WritePolicy(("N64",))
    data = os.urandom(300_000)  # several READ/WRITE chunks
    r = ops.upload(app, io.BytesIO(data), "/N64/new/Test.z64", policy=policy, total=len(data))
    assert (card / "N64" / "new" / "Test.z64").read_bytes() == data
    ops.verify(app, r.path, r.sha256)
    buf = io.BytesIO()
    ops.download(app, "/N64/new/Test.z64", buf)
    assert buf.getvalue() == data
    assert app.mkdir("N64/new") is False
    assert app.delete("N64/new/Test.z64") is True
    assert app.delete("N64/new/Test.z64") is False
    assert app.delete("N64/new") is True


def test_upload_refuses_to_replace_without_overwrite(app):
    from syncforever.protocol import DeviceError

    policy = ops.WritePolicy(("N64",))
    with pytest.raises(DeviceError, match="FR_EXIST"):
        ops.upload(app, io.BytesIO(b"x"), "N64/Paper Mario (USA).z64", policy=policy)


def test_reading_without_an_open_file_is_an_error(app):
    from syncforever.protocol import DeviceError

    with pytest.raises(DeviceError, match="FR_INVALID_OBJECT"):
        app.read(10)
    app.open("N64/Paper Mario (USA).z64", FA_READ)
    assert app.read(5000) == b"P" * 1000  # short at end of file
    app.close()


def test_flat_saves_back_up_by_rom_name(app, tmp_path, card):
    store = saves.SaveStore(tmp_path / "store")
    results = saves.backup_all(app, store)
    assert {(r.game, r.file, r.status) for r in results} == {
        ("Paper Mario (USA)", "Paper Mario (USA).fla", "saved"),
        ("Super Mario 64 (USA)", "Super Mario 64 (USA).eep", "saved"),
    }
    assert [r.status for r in saves.backup_all(app, store)] == ["unchanged", "unchanged"]
    (card / "ED64" / "gamedata" / "Super Mario 64 (USA).eep").write_bytes(b"N" * 512)
    changed = [r for r in saves.backup_all(app, store) if r.status == "saved"]
    assert [r.file for r in changed] == ["Super Mario 64 (USA).eep"]


def test_frames_to_the_app_are_padded_to_4_bytes():
    f = x7.frame(x7.TYPE_SFE, b"abcde")
    assert f[:4] == b"DMA@" and struct.unpack(">I", f[4:8])[0] == x7.TYPE_SFE << 24 | 5
    assert f[8:16] == b"abcde\0\0\0" and f[16:] == b"CMPH"


def test_the_reader_skips_padding_and_noise():
    class Bytes:
        def __init__(self, data):
            self.data = bytearray(data)

        def read(self, n):
            got = bytes(self.data[:n])
            del self.data[:n]
            return got

    body = b"hello"
    raw = b"\0" * 300 + b"junkDM" + b"DMA@" + struct.pack(">I", 0x53 << 24 | len(body)) + body + b"CMPH"
    reader = x7.FrameReader(Bytes(raw + b"\0" * 7))
    assert reader.next(1.0) == (0x53, body)
    assert reader.next(0.05) is None


# -- the box's X7 session (box.x7_server): the app protocol over the X7 link -----------------


def _plan(payload):
    """(mode, add, replace, remove, saves) from a PLAN."""
    n = payload[1]
    add, rep, rem = struct.unpack_from("<HHH", payload, 2 + n)
    m = payload[8 + n]
    return payload[0], add, rep, rem, payload[9 + n : 9 + n + m].decode()


def test_a_whole_session_backs_up_plans_and_syncs_with_copy_progress(card, tmp_path):
    from datetime import datetime

    from syncforever import appserver as a
    from syncforever.box import Box
    from syncforever.config import Config

    (card / "ED64" / "emu").mkdir()
    for emu in ("gb.v64", "sfc.v64"):
        (card / "ED64" / "emu" / emu).write_bytes(b"emu")
    lib = tmp_path / "lib"
    for rel, size in {"N64/Big.z64": 300_000, "GB/Tetris.gb": 32_768, "SFC/Zelda.sfc": 1000, "notes.txt": 5}.items():
        (lib / rel).parent.mkdir(parents=True, exist_ok=True)
        (lib / rel).write_bytes(os.urandom(size))
    (tmp_path / "saves").mkdir()
    log = []
    box = Box(
        Config(library=lib, saves=tmp_path / "saves", state=tmp_path / "state"),
        lambda log_: None,
        log=log.append,
        now=lambda: datetime(2026, 11, 2, 23, 0),
        sleep=lambda s: None,
    )
    fake = FakeX7(card, state="app")
    fake.out.clear()
    server = box.x7_server(fake)

    fake.app_says(a.HELLO, bytes([a.PROTO_VERSION]))
    server.poll(0.1)
    assert [k for k, _ in fake.ui_got] == [a.WELCOME]
    assert "emulators: .gb .sfc" in log

    fake.app_says(a.SYNC_PLAN, bytes([a.DEFAULT_MODE]))
    server.poll(0.1)
    kind, payload = fake.ui_got[-1]
    assert kind == a.PLAN
    assert _plan(payload) == (0, 3, 0, 0, "Backed up (2)")  # not notes.txt
    assert len([p for p in (tmp_path / "saves").rglob("*.fla") if p.is_file()]) == 1

    fake.ui_got.clear()
    assert payload.endswith(b"Updated: power off and on to use")  # the first plan says so
    fake.app_says(a.SYNC_RUN, bytes([0, server.plan_number]))
    server.poll(0.1)
    kinds = [k for k, _ in fake.ui_got]
    assert kinds[-1] == a.SYNC_DONE
    copies = [struct.unpack_from("<HHBII", p) for k, p in fake.ui_got if k == a.COPY]
    big = [c for c in copies if c[4] == 300_000]
    assert big[0][2] == 0 and {c[2] for c in big} == {0, 1}  # copying, then checking the readback
    assert big[-1][2:] == (1, 300_000, 300_000)  # the readback finished
    for rel in ("N64/Big.z64", "GB/Tetris.gb", "SFC/Zelda.sfc"):
        assert (card / rel).read_bytes() == (lib / rel).read_bytes()
    assert not (card / "notes.txt").exists()


def test_the_emulator_scan_reads_extensions_from_v64_names():
    assert x7.emulated(["gb.v64", "GBC.V64", "smc.v64", "sfc.v64", ".hidden.v64", "readme.txt", "v64"]) == (
        ".gb",
        ".gbc",
        ".sfc",
        ".smc",
    )


@pytest.mark.parametrize("raw", ["../../etc/x", "a/b", "", "x" * 100, "név"])
def test_a_usb_serial_never_becomes_a_path(raw):
    from syncforever.box import safe_id

    out = safe_id(raw)
    assert out.replace("_", "").replace("-", "").isalnum() and out.isascii() and "/" not in out


def test_a_plain_usb_serial_is_kept():
    from syncforever.box import safe_id

    assert safe_id("AQ03BVGM") == "AQ03BVGM"


def test_sfe_install_boots_the_app_and_writes_it_to_the_card(card, capsys):
    from syncforever import apps, cli

    assert cli.main(["--x7", "--sim", str(card), "install"]) == 0
    assert (card / "Sync Forever.z64").read_bytes() == apps.packaged(".z64")
    assert "/Sync Forever.z64: installed" in capsys.readouterr().out


def test_the_session_updates_the_apps_file_and_carries_on(card, tmp_path):
    from datetime import datetime

    from syncforever import apps
    from syncforever import appserver as a
    from syncforever.box import Box
    from syncforever.config import Config

    (card / "Sync Forever.z64").write_bytes(b"an older app")
    (tmp_path / "lib").mkdir()

    def hello_once():
        log = []
        box = Box(
            Config(library=tmp_path / "lib", state=tmp_path / "state"),
            lambda log_: None,
            log=log.append,
            now=lambda: datetime(2026, 11, 2, 23, 0),
            sleep=lambda s: None,
        )
        fake = FakeX7(card, state="app")
        fake.out.clear()
        server = box.x7_server(fake)
        fake.app_says(a.HELLO, bytes([a.PROTO_VERSION]))
        server.poll(0.1)
        (kind, payload), *_ = fake.ui_got
        return kind, payload[1 : 1 + payload[0]].decode(), log

    kind, text, log = hello_once()
    assert (kind, text) == (a.WELCOME, "Updated for next start")
    assert (card / "Sync Forever.z64").read_bytes() == apps.packaged(".z64")
    assert any("runs from the next start" in line for line in log)
    assert hello_once()[:2] == (a.WELCOME, "Connected")  # the next session: nothing to do


@pytest.mark.parametrize(
    ("index", "card_version", "line"),
    [
        ("3.11", (0x03, 0x11), ("v3.11", 1)),  # the newest: green
        ("3.11", (0x03, 0x08), ("v3.08", 2)),  # a newer OS exists: red
        (None, (0x03, 0x11), ("v3.11", 3)),  # never checked: plain
    ],
)
def test_the_plan_carries_the_os_version(card, tmp_path, index, card_version, line):
    from datetime import datetime

    from syncforever import appserver as a
    from syncforever.box import Box
    from syncforever.config import Config
    from syncforever.firmware import OS_CACHE

    (card / "ED64" / "OS64.v64").write_bytes(bytes([0x24, 0x04, *card_version]) + bytes.fromhex("a6030044a6040042"))
    (tmp_path / "lib").mkdir()
    state = tmp_path / "state"
    if index:
        (state / "firmware").mkdir(parents=True)
        (state / "firmware" / OS_CACHE).write_text(f'{{"checked": 9e99, "versions": ["{index}"]}}')
    box = Box(
        Config(library=tmp_path / "lib", state=state, firmware=True, app_path=""),
        lambda log_: None,
        now=lambda: datetime(2026, 11, 2, 23, 0),
        sleep=lambda s: None,
    )
    box.firmware.get = lambda url: (_ for _ in ()).throw(OSError("offline in tests"))
    fake = FakeX7(card, state="app")
    fake.out.clear()
    server = box.x7_server(fake)
    fake.app_says(a.HELLO, bytes([a.PROTO_VERSION]))
    fake.app_says(a.SYNC_PLAN, bytes([a.DEFAULT_MODE]))
    server.poll(0.1)
    server.poll(0.1)
    payload = next(p for k, p in fake.ui_got if k == a.PLAN)
    n = payload[1]
    off = 2 + n + 6
    off += 1 + payload[off]  # saves
    fw_len = payload[off]
    assert (payload[off + 1 : off + 1 + fw_len].decode(), payload[off + 1 + fw_len]) == line


def pak(serial: bytes, fill: int) -> bytes:
    image = bytearray([fill]) * x7.CPAK_SIZE
    image[0x20:0x40] = serial.ljust(32, b"\x01")
    return bytes(image)


def test_controller_paks_are_backed_up_by_their_id(app, tmp_path):
    app.t.paks = {0: pak(b"PAK-A", 1), 2: pak(b"PAK-B", 2)}
    store = saves.SaveStore(tmp_path / "store")
    paks = [r for r in saves.backup_all(app, store) if r.game == saves.PAKS]
    assert len(paks) == 2 and {r.status for r in paks} == {"saved"} and len({r.file for r in paks}) == 2
    app.t.paks = {1: pak(b"PAK-A", 1)}  # the same pak, another controller: nothing new
    again = [r for r in saves.backup_all(app, store) if r.game == saves.PAKS]
    assert [(r.file, r.status) for r in again] == [(paks[0].file, "unchanged")]
    app.t.paks = {0: pak(b"PAK-A", 9)}  # it changed: a new version of the same pak
    changed = [r for r in saves.backup_all(app, store) if r.game == saves.PAKS]
    assert [(r.file, r.status) for r in changed] == [(paks[0].file, "saved")]


def test_no_paks_no_entries(app, tmp_path):
    results = saves.backup_all(app, saves.SaveStore(tmp_path / "store"))
    assert all(r.game != saves.PAKS for r in results)


def test_rename_through_the_app(app, card):
    app.move("N64/Paper Mario (USA).z64", "N64/Paper Mario.z64")
    assert (card / "N64" / "Paper Mario.z64").read_bytes() == b"P" * 1000
    with pytest.raises(DeviceError):
        app.move("N64/nope.z64", "N64/x.z64")


def test_a_read_whose_reply_is_lost_is_asked_for_again(app, card, monkeypatch):
    # "X7 frame cut short (64088/65539 bytes)" while verifying (2026-11-05): part of the reply
    # never came. The app is fine and its file has moved on: go back to where that READ began.
    monkeypatch.setattr(x7, "FRAME_IDLE", 0.01)
    data = bytes(range(256)) * 1024  # three READs' worth at the small size below
    (card / "big.z64").write_bytes(data)
    monkeypatch.setattr(x7, "CHUNK", 100 * 1024)
    cart, lost = app.t, []
    real = cart.read

    def losing(n):
        got = real(n)
        # Part-way through the second reply, once: the rest of it never arrives.
        if app._position == 100 * 1024 and not lost and len(got) > 5000:
            lost.append(len(cart.out) + len(got))
            cart.out.clear()
            return got[:5000]
        return got

    monkeypatch.setattr(cart, "read", losing)
    app.open("big.z64", FA_READ)
    assert app.read(len(data)) == data
    assert lost
    app.close()


def test_a_read_that_keeps_failing_says_so(app, card, monkeypatch):
    monkeypatch.setattr(x7, "FRAME_IDLE", 0.01)
    (card / "big.z64").write_bytes(b"x" * 9000)
    app.open("big.z64", FA_READ)
    calls = []

    def never(op, payload=b"", timeout=None):
        calls.append(op)
        raise LinkError("X7 frame cut short")

    monkeypatch.setattr(app, "call", never)
    with pytest.raises(LinkError):
        app.read(9000)
    assert calls == [x7.OP_READ, x7.OP_SEEK, x7.OP_SEEK]  # asked again until the tries ran out


def test_every_app_speaks_the_boxs_protocol_version():
    # The N64 app was bumped alone (2026-11-05): "App too old (v8)", the box refusing a newer app.
    import re
    from pathlib import Path

    from syncforever.appserver import PROTO_VERSION

    root = Path(__file__).resolve().parents[2]
    for header, name in (
        ("n64/src/ui.h", "UI_VERSION"),
        ("md/src/link.h", "LINK_VERSION"),
        ("app/link.h", "LINK_VERSION"),
    ):
        found = re.search(rf"#define {name} (\d+)", (root / header).read_text())
        assert int(found.group(1)) == PROTO_VERSION, header
