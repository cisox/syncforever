"""firmware.py: Krikzz's index, the daily check, the download, and which firmware a cart runs."""

import json
import zlib
from datetime import datetime
from pathlib import Path

import pytest
from conftest import make_efu

from syncforever import firmware
from syncforever.carts import MEGA_PRO, N8_PRO
from syncforever.firmware import FirmwareStore, Release, check_efu, efu_files, is_current, parse_index

# The shape of the Apache index at krikzz.com/pub/support/mega-everdrive/pro-series/firmware/
INDEX = """<html><body><h1>Index of /pub/support/mega-everdrive/pro-series/firmware</h1><table>
<tr><td><a href="?C=N;O=D">Name</a></td></tr>
<tr><td><a href="/pub/support/mega-everdrive/pro-series/">Parent Directory</a></td></tr>
<tr><td><a href="legacy/">legacy/</a></td></tr>
<tr><td><a href="changelog.txt">changelog.txt</a></td></tr>
<tr><td><a href="mega-pro-fw-v24.1213.efu">mega-pro-fw-v24.1213.efu</a></td></tr>
<tr><td><a href="mega-pro-fw-v26.0925.efu">mega-pro-fw-v26.0925.efu</a></td></tr>
<tr><td><a href="mega-pro-fw-v26.0608.efu">mega-pro-fw-v26.0608.efu</a></td></tr>
<tr><td><a href="mega-pro-fw-v26.9999.efu">a name that isn't a date</a></td></tr>
<tr><td><a href="edn8-pro-fw-v27.0101.efu">another cart's</a></td></tr>
<tr><td><a href="recovery-fw.zip">recovery-fw.zip</a></td></tr>
</table></body></html>"""


def test_parse_index_finds_this_carts_releases_oldest_first():
    releases = parse_index(INDEX, "mega-pro-fw-v")
    assert [r.version for r in releases] == ["24.1213", "26.0608", "26.0925"]
    assert releases[-1].file == "mega-pro-fw-v26.0925.efu"
    assert releases[-1].date.isoformat() == "2026-09-25"


MEGA_NEWEST = Release("26.0925", "mega-pro-fw-v26.0925.efu")
N8_NEWEST = Release("25.1123", "edn8-pro-fw-v25.1123.efu")


@pytest.mark.parametrize(
    ("newest", "built", "current"),
    [
        (MEGA_NEWEST, datetime(2026, 9, 24, 13, 2), True),  # the Mega on v26.0925 (hardware)
        (MEGA_NEWEST, datetime(2026, 9, 22), True),  # within the slack
        (MEGA_NEWEST, datetime(2026, 12, 1), True),  # a newer (beta) build
        (MEGA_NEWEST, datetime(2024, 12, 13), False),  # v24.1213
        (N8_NEWEST, datetime(2025, 9, 30), False),  # the N8 PRO here
        (MEGA_NEWEST, None, None),
    ],
)
def test_is_current_reads_the_build_date(newest, built, current):
    assert is_current(newest, built) is current


MEGA_OS = bytes(range(256)) * 64
MEGA_EFU = make_efu(0x18, {"MEGA/megaos.dat": MEGA_OS, "MEGA/themes/readme.txt": b"themes"})


class Net:
    """A fake krikzz.com: counts requests, can fail."""

    def __init__(self, index=INDEX):
        self.index = index
        self.urls: list[str] = []
        self.down = False

    def get(self, url):
        self.urls.append(url)
        if self.down:
            raise OSError("no route to host")
        if url.endswith("/"):
            return self.index.encode()
        return MEGA_EFU


@pytest.fixture
def net():
    return Net()


def store(tmp_path, net, now):
    return FirmwareStore(tmp_path / "fw", get=net.get, clock=lambda: now[0])


def test_refresh_downloads_the_newest_once_and_checks_daily(tmp_path, net):
    now = [1000.0]
    s = store(tmp_path, net, now)
    assert s.refresh(MEGA_PRO).version == "26.0925"
    assert net.urls == [
        "https://krikzz.com/pub/support/mega-everdrive/pro-series/firmware/",
        "https://krikzz.com/pub/support/mega-everdrive/pro-series/firmware/mega-pro-fw-v26.0925.efu",
    ]
    assert (tmp_path / "fw" / "mega-pro-fw-v26.0925.efu").read_bytes() == MEGA_EFU
    now[0] += 3600
    s.refresh(MEGA_PRO)
    assert len(net.urls) == 2  # an hour later: nothing fetched
    now[0] += firmware.CHECK_EVERY
    s.refresh(MEGA_PRO)
    assert len(net.urls) == 3  # a day later: the index again, the file is already here


def test_a_failed_check_keeps_the_last_index(tmp_path, net):
    now = [1000.0]
    logs = []
    s = store(tmp_path, net, now)
    s.log = logs.append
    s.refresh(MEGA_PRO)
    net.down = True
    now[0] += firmware.CHECK_EVERY
    assert s.refresh(MEGA_PRO).version == "26.0925"
    assert any("firmware check failed" in line for line in logs)


def test_no_network_and_no_cache_means_no_firmware_line(tmp_path, net):
    net.down = True
    s = store(tmp_path, net, [0.0])
    assert s.refresh(MEGA_PRO) is None
    assert s.for_cart(MEGA_PRO, datetime(2026, 9, 24)) is None


def test_for_cart_reports_the_newest_and_whether_the_cart_runs_it(tmp_path, net):
    s = store(tmp_path, net, [0.0])
    s.refresh(MEGA_PRO)
    fw = s.for_cart(MEGA_PRO, datetime(2026, 9, 24))
    assert (fw.release.version, fw.current) == ("26.0925", True)
    assert fw.path == tmp_path / "fw" / "mega-pro-fw-v26.0925.efu"
    assert fw.delivered == frozenset()
    assert fw.marker == ("MEGA/megaos.dat", len(MEGA_OS), zlib.crc32(MEGA_OS))
    assert s.for_cart(MEGA_PRO, datetime(2024, 12, 13)).current is False
    assert s.for_cart(MEGA_PRO, None).marker is not None  # the menu file decides, not the date
    assert s.for_cart(N8_PRO, datetime(2025, 9, 30)) is None  # its family wasn't checked


def test_the_check_is_honest_about_who_asks():
    assert firmware.USER_AGENT.startswith("SyncForever/")
    assert "Mozilla" not in firmware.USER_AGENT


def test_index_cache_is_plain_json(tmp_path, net):
    s = store(tmp_path, net, [5.0])
    s.refresh(MEGA_PRO)
    cached = json.loads(Path(tmp_path / "fw" / "mega-everdrive.json").read_text())
    assert cached["checked"] == 5.0 and cached["files"][-1] == "mega-pro-fw-v26.0925.efu"


def test_the_ledger_remembers_what_the_box_delivered_per_cart(tmp_path, net):
    s = store(tmp_path, net, [0.0])
    s.refresh(MEGA_PRO)
    s.record("SERIAL-A", "mega-pro-fw-v26.0925.efu", True)
    assert s.for_cart(MEGA_PRO, datetime(2024, 12, 13), "SERIAL-A").delivered == {"mega-pro-fw-v26.0925.efu"}
    assert s.for_cart(MEGA_PRO, datetime(2024, 12, 13), "SERIAL-B").delivered == frozenset()
    s.record("SERIAL-A", "mega-pro-fw-v26.0925.efu", False)
    assert s.for_cart(MEGA_PRO, datetime(2026, 9, 24), "SERIAL-A").delivered == frozenset()


def test_efu_files_lists_files_with_size_and_crc():
    files = efu_files(MEGA_EFU)
    assert set(files) == {"MEGA/megaos.dat", "MEGA/themes/readme.txt"}
    assert files["MEGA/megaos.dat"][:2] == (len(MEGA_OS), zlib.crc32(MEGA_OS))


@pytest.mark.parametrize(
    ("data", "error"),
    [
        (MEGA_EFU[:-10], "CRC mismatch"),  # truncated download
        (MEGA_EFU[:200] + b"\x00" + MEGA_EFU[201:], "CRC mismatch"),  # corrupt
        (make_efu(0x17, {"EDN8/nesos.nes": b"n8"}), "not an .efu for device 0x18"),  # another cart's
        (b"<html>Not Found</html>" * 4, "not an .efu"),
    ],
)
def test_a_bad_download_is_never_kept(data, error, tmp_path):
    with pytest.raises(ValueError, match=error):
        check_efu(data, 0x18)
    net = Net()
    net.get = lambda url: INDEX.encode() if url.endswith("/") else data
    logs = []
    s = FirmwareStore(tmp_path / "fw", get=net.get, clock=lambda: 0.0, log=logs.append)
    s.refresh(MEGA_PRO)
    assert not (tmp_path / "fw" / "mega-pro-fw-v26.0925.efu").exists()
    assert any("firmware download failed" in line for line in logs)


# -- the EverDrive-64 X7's OS: a version in the menu file against Krikzz's OS index --------------

OS_INDEX_HTML = """<a href="/pub/support/everdrive-64/x-series/">Parent</a><a href="changelist.txt">c</a>
<a href="OS-V3.09.zip">OS-V3.09.zip</a><a href="OS-V3.11.zip">OS-V3.11.zip</a><a href="OS-V3.10.zip">x</a>"""


def menu_with_version(major, minor, filler=b"\0" * 64):
    """Bytes shaped like the X7 menu around its version store (li a0,v; sh v1,68(s0); sh a0,66(s0))."""
    return filler + bytes([0x24, 0x04, major, minor]) + bytes.fromhex("a6030044a6040042") + filler


def test_parse_os_index_sorts_by_number():
    assert firmware.parse_os_index(OS_INDEX_HTML) == ["3.09", "3.10", "3.11"]


def test_os_version_reads_the_menus_constant():
    assert firmware.os_version(menu_with_version(0x03, 0x11)) == "3.11"  # BCD (the owner's V3.11)
    assert firmware.os_version(menu_with_version(0x03, 0x08)) == "3.08"
    assert firmware.os_version(menu_with_version(0x03, 0x1A)) is None  # not BCD
    swapped = menu_with_version(0x03, 0x11, filler=bytes.fromhex("80371240") + b"\0" * 60)
    v64 = bytes(b for i in range(0, len(swapped), 2) for b in (swapped[i + 1], swapped[i]))
    assert firmware.os_version(v64) == "3.11"  # a .v64 (16-bit swapped) menu file
    assert firmware.os_version(b"\0" * 256) is None
    assert firmware.os_version(bytes.fromhex("a6040042") + b"\0" * 8) is None  # nothing before it


def test_refresh_os_checks_daily_and_keeps_the_last_index(tmp_path):
    now, urls = [1000.0], []

    def get(url):
        urls.append(url)
        if len(urls) > 1:
            raise OSError("offline")
        return OS_INDEX_HTML.encode()

    s = FirmwareStore(tmp_path / "fw", get=get, clock=lambda: now[0])
    assert s.refresh_os() == "3.11" and urls == [firmware.OS_INDEX]
    assert s.refresh_os() == "3.11" and len(urls) == 1  # within the day
    now[0] += firmware.CHECK_EVERY
    assert s.refresh_os() == "3.11" and len(urls) == 2  # offline: the last index stays
    assert s.newest_os() == "3.11"


def test_sd2snes_versions_come_from_three_number_tags():
    tags = b'[{"name": "v4.2"}, {"name": "v1.11.2"}, {"name": "v1.9.0"}, {"name": "v1.11.10"}, {"name": "beta"}]'
    assert firmware.parse_sd2snes_tags(tags) == ["1.9.0", "1.11.2", "1.11.10"]


def test_refresh_versions_reads_the_sd2snes_tags_daily(tmp_path):
    urls, now = [], [1000.0]

    def get(url):
        urls.append(url)
        return b'[{"name": "v1.11.1"}, {"name": "v1.11.2"}]'

    s = firmware.FirmwareStore(tmp_path, get=get, clock=lambda: now[0])
    assert s.refresh_versions("sd2snes") == "1.11.2" and urls == [firmware.SD2SNES_TAGS]
    assert s.refresh_versions("sd2snes") == "1.11.2" and len(urls) == 1
    assert s.newest_version("sd2snes") == "1.11.2"
