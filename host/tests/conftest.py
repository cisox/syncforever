import struct
import zlib

import pytest

from syncforever import n8pro
from syncforever.n8pro import N8Pro
from syncforever.sim import FakeN8Pro


def pytest_addoption(parser):
    parser.addoption("--hardware", action="store_true", help="run tests against a real N8 PRO on USB")
    parser.addoption("--port", help="serial port for --hardware (default: auto-detect)")
    parser.addoption("--sd-backup", help="folder holding a card-reader copy of the SD card, for hash checks")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--hardware"):
        return
    skip = pytest.mark.skip(reason="needs --hardware")
    for item in items:
        if "hardware" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _writes_allowed():
    """A serve test that stops refuses later writes process-wide (n8pro.stop_writes)."""
    n8pro.allow_writes()
    yield
    n8pro.allow_writes()


@pytest.fixture
def sd(tmp_path):
    root = tmp_path / "sd"
    (root / "roms" / "nes").mkdir(parents=True)
    (root / "saves").mkdir()
    (root / "roms" / "nes" / "Zelda.nes").write_bytes(bytes(range(256)) * 160)  # 40 KiB
    (root / "readme.txt").write_bytes(b"hello\n")
    return root


@pytest.fixture
def sim(sd):
    return FakeN8Pro(sd)


@pytest.fixture
def dev(sim):
    d = N8Pro(sim)
    d.connect()
    return d


def make_efu(device: int, files: dict[str, bytes], version_date: int = 0x5B77) -> bytes:
    """A minimal .efu in Krikzz's layout (firmware.py docstring): header, folders, files."""
    out = bytearray(0x40)
    out[2], out[3] = device, 0xED
    struct.pack_into("<H", out, 4, version_date)
    folders = sorted({name.rsplit("/", 1)[0] for name in files if "/" in name})
    for folder in folders:
        raw = folder.encode()
        out += struct.pack("<BHBHHII", 1, len(raw), 0, 0x5AAE, 0, 0, 0) + raw
    for name, body in files.items():
        raw = name.encode()
        out += struct.pack("<BHBHHII", 0, len(raw), 0, 0x5AAE, 0, len(body), zlib.crc32(body)) + raw + body
    return bytes(out)
