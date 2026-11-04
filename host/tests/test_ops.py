import hashlib
import io

import pytest

from syncforever import ops
from syncforever.n8pro import N8Pro
from syncforever.protocol import DeviceError, UnsafeStateError
from syncforever.sim import FakeN8Pro

POLICY = ops.WritePolicy()


def test_policy_allows_only_under_write_roots():
    assert POLICY.check("/syncforever-test/a/b.bin") == "syncforever-test/a/b.bin"
    assert POLICY.check("syncforever-test") == "syncforever-test"
    for bad in ("/roms/nes/x.nes", "syncforever-testing/x", "/"):
        with pytest.raises(ops.WriteNotAllowed):
            POLICY.check(bad)
    with pytest.raises(ValueError):
        POLICY.check("syncforever-test/../roms/x.nes")
    assert ops.WritePolicy(anywhere=True).check("/roms/x.nes") == "roms/x.nes"


def test_upload_creates_parents_and_matches_hash(dev, sd):
    data = b"N" * 70_000
    r = ops.upload(dev, io.BytesIO(data), "/syncforever-test/a/b/c.bin", policy=POLICY)
    assert r.sha256 == hashlib.sha256(data).hexdigest()
    assert (sd / "syncforever-test/a/b/c.bin").read_bytes() == data
    ops.verify(dev, r.path, r.sha256)


def test_upload_refuses_existing_file_unless_overwrite(dev, sd):
    ops.upload(dev, io.BytesIO(b"one"), "syncforever-test/x.bin", policy=POLICY)
    with pytest.raises(DeviceError, match="FR_EXIST"):
        ops.upload(dev, io.BytesIO(b"two"), "syncforever-test/x.bin", policy=POLICY)
    assert (sd / "syncforever-test/x.bin").read_bytes() == b"one"
    ops.upload(dev, io.BytesIO(b"two"), "syncforever-test/x.bin", policy=POLICY, overwrite=True)
    assert (sd / "syncforever-test/x.bin").read_bytes() == b"two"


def test_upload_refuses_while_a_game_runs(sd):
    dev = N8Pro(FakeN8Pro(sd, in_game=True))
    dev.connect()
    with pytest.raises(UnsafeStateError):
        ops.upload(dev, io.BytesIO(b"x"), "syncforever-test/x.bin", policy=POLICY)
    assert not (sd / "syncforever-test").exists()


def test_upload_outside_write_roots_touches_nothing(dev, sim):
    sim.commands.clear()
    with pytest.raises(ops.WriteNotAllowed):
        ops.upload(dev, io.BytesIO(b"x"), "roms/nes/new.nes", policy=POLICY)
    assert sim.commands == []


def test_download_hash_matches_original(dev, sd):
    out = io.BytesIO()
    r = ops.download(dev, "roms/nes/Zelda.nes", out, chunk=10_000)
    original = (sd / "roms/nes/Zelda.nes").read_bytes()
    assert out.getvalue() == original
    assert r.sha256 == hashlib.sha256(original).hexdigest()


def test_download_missing_file_raises(dev):
    with pytest.raises(DeviceError, match="FR_NO_FILE"):
        ops.download(dev, "missing.bin", io.BytesIO())


def test_verify_detects_mismatch(dev):
    with pytest.raises(ops.VerifyError):
        ops.verify(dev, "readme.txt", "0" * 64)


def test_bench_round_trips_and_cleans_up(dev, sd):
    results = ops.bench(dev, "syncforever-test/bench", [1, 32 * 1024, 100_000], policy=POLICY)
    assert [r.size for r in results] == [1, 32 * 1024, 100_000]
    assert all(r.match for r in results)
    assert list((sd / "syncforever-test/bench").iterdir()) == []


def test_launch_starts_a_rom(dev, sim):
    assert ops.launch(dev, "/roms/nes/Zelda.nes") == 0  # fixture has no iNES header
    assert sim.running == "roms/nes/Zelda.nes"


def test_launch_refused_in_game(sd):
    in_game = N8Pro(FakeN8Pro(sd, in_game=True))
    in_game.connect()
    with pytest.raises(UnsafeStateError, match="menu"):
        ops.launch(in_game, "roms/nes/Zelda.nes")


def test_launch_refuses_missing_rom_and_folders(dev, sim):
    for path in ("roms/nes/nope.nes", "roms"):
        with pytest.raises(ValueError, match="no ROM"):
            ops.launch(dev, path)
    assert not sim.in_game
