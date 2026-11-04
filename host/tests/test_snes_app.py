"""The SNES app (snes/): its link code in cc65's 6502 simulator, and the packaged copy.
There's no SNES emulator here: screens and input are checked on the console."""

import shutil
import subprocess
from pathlib import Path

import pytest

from syncforever import apps

SNES_DIR = Path(__file__).resolve().parents[2] / "snes"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("cl65", "sim65", "make")), reason="needs cc65 (cl65, sim65)"
)


def test_link_parses_and_writes_frames_in_the_simulator(tmp_path):
    r = subprocess.run(
        ["make", "-C", str(SNES_DIR), "-s", f"B={tmp_path}", "test"], check=False, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "link_test: ok" in r.stdout


def test_packaged_app_is_up_to_date(tmp_path):
    """data/sync-forever.sfc must be rebuilt (`make -C snes package`) whenever the app changes."""
    subprocess.run(["make", "-C", str(SNES_DIR), "-s", f"B={tmp_path}"], check=True, capture_output=True)
    assert (tmp_path / "sync-forever.sfc").read_bytes() == apps.packaged(".sfc"), "run: make -C snes package"
