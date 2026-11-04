"""Milestone A checks against a real N8 PRO. Run: uv run pytest --hardware [--port P] [--sd-backup DIR] -s

Start with the NES on and sitting at the N8 PRO menu. Writes stay under /syncforever-test.
"""

import hashlib
import io
import os
import time
from pathlib import Path

import pytest

from syncforever import ops
from syncforever.n8pro import N8Pro
from syncforever.transport import SerialTransport

pytestmark = pytest.mark.hardware


@pytest.fixture(scope="module")
def cart(request):
    port = request.config.getoption("--port")
    transport = SerialTransport(port) if port else SerialTransport.auto()
    dev = N8Pro(transport)
    dev.connect()
    yield dev
    transport.close()


def test_identify(cart):
    info = cart.sys_info()
    print(f"\nfirmware os {info.sw_ver:04X} hw {info.hw_ver:04X} boot {info.boot_ver:04X}, serial {info.serial}")
    print(f"sys_inf raw: {info.raw.hex()}")


def test_menu_answers_ping(cart):
    # The write gate depends on this. Start these tests at the N8 menu.
    assert cart.menu_ping()


def test_list_root(cart):
    entries = cart.list_dir("/")
    for e in entries:
        print(f"  {'<dir>' if e.is_dir else e.size:>10}  {e.name}")
    assert entries


def _small_backup_file(backup):
    for dirpath, _, files in sorted(os.walk(backup)):
        for name in sorted(files):
            p = Path(dirpath) / name
            rel = p.relative_to(backup).as_posix()
            if (
                not name.startswith(".")
                and 0 < p.stat().st_size <= 256 * 1024
                and not rel.startswith("syncforever-test")
            ):
                return p, rel
    return None, None


def test_download_matches_card_reader_backup(cart, request):
    backup = request.config.getoption("--sd-backup")
    if not backup:
        pytest.skip("pass --sd-backup DIR to compare against a card-reader copy")
    local, rel = _small_backup_file(Path(backup))
    if local is None:
        pytest.skip("no file of 1 B to 256 KiB in the backup")
    entry = cart.stat(rel)
    assert entry is not None and entry.size == local.stat().st_size
    r = ops.download(cart, rel, io.BytesIO())
    assert r.sha256 == hashlib.sha256(local.read_bytes()).hexdigest()
    print(f"\n{rel}: {r.size} B at {r.kib_per_s:.0f} KiB/s")


def test_upload_new_file_and_read_back(cart):
    data = os.urandom(10_000)
    path = f"{ops.DEFAULT_WRITE_ROOT}/hw-{time.strftime('%Y%m%d-%H%M%S')}.bin"
    up = ops.upload(cart, io.BytesIO(data), path, policy=ops.WritePolicy())
    try:
        ops.verify(cart, path, up.sha256)
    finally:
        cart.delete(path)
