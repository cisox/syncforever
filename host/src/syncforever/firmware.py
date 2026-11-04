"""Firmware updates: Krikzz's newest .efu for a cart, delivered to its SD root (design.md §6).

The box never flashes anything: it puts the file on the card and the app shows
"Firmware: v26.0925", green when the cart runs it, red when it's waiting to be installed from
the cart's own menu (efu-install.txt). Which firmware a cart runs isn't reported as a version.
The .efu holds the files the install writes, each with its CRC32 (format below): the cart runs
it when its menu file (CartProfile.firmware_marker) matches. SYS_INF's build date is the fallback
where it tracks the firmware (the Mega's does; the N8's doesn't).

.efu format (Krikzz's, read from his files 2026-11-02; our own parser): little-endian. Header
0x40 bytes: u8 0, u8 0, u8 device id (0x17 N8, 0x18 Mega), u8 0xED, u16 FAT date of the version,
u16 time, ...; then entries from 0x40: u8 kind (1 folder, 0 file), u16 name length, u8, u16 date,
u16 time, u32 size, u32 CRC32, the name, then (files) the data.

Network: Krikzz's plain Apache index, checked at most once a day with an honest User-Agent,
in the background when a cart connects; the app session only uses what's already here.
"""

from __future__ import annotations

import json
import re
import ssl
import struct
import threading
import time
import urllib.request
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import certifi

from . import __version__

INDEX = "https://krikzz.com/pub/support/{site}/pro-series/firmware/"
# The EverDrive-64 X7's OS: OS-V3.11.zip and so on, installed by hand (a zip's contents on the
# card root), so the box only reads the index and compares versions.
OS_INDEX = "https://krikzz.com/pub/support/everdrive-64/x-series/OS/"
OS_CACHE = "everdrive-64-os.json"
# The FXPAK PRO's firmware (sd2snes): a whole sd2snes folder, installed by hand, so likewise the
# box only compares versions: GitHub's tag list (once a day, unauthenticated: well under the limit).
SD2SNES_TAGS = "https://api.github.com/repos/mrehkopf/sd2snes/tags?per_page=100"
SD2SNES_CACHE = "sd2snes.json"
USER_AGENT = f"SyncForever/{__version__}"
CHECK_EVERY = 24 * 3600.0  # seconds between index checks
TIMEOUT = 30.0
SLACK = timedelta(days=3)  # build date vs version date


@dataclass(frozen=True)
class Release:
    version: str  # "26.0925"
    file: str  # "mega-pro-fw-v26.0925.efu"

    @property
    def date(self) -> date:
        yy, mmdd = self.version.split(".")
        return date(2000 + int(yy), int(mmdd[:2]), int(mmdd[2:]))


@dataclass(frozen=True)
class Firmware:
    """What the app shows for one cart, and the file to deliver."""

    release: Release  # the newest
    current: bool  # the cart runs it
    path: Path | None  # the downloaded .efu (None until downloaded)
    delivered: frozenset[str] = frozenset()  # files the box copied to this cart's root
    marker: tuple[str, int, int] | None = None  # (menu file, size, CRC32) in the .efu


def parse_index(html: str, prefix: str) -> list[Release]:
    """Every <prefix>vYY.MMDD.efu linked from the index, oldest first."""
    pat = re.compile(r'href="(' + re.escape(prefix) + r'(\d{2}\.\d{4})\.efu)"')
    found = {m.group(1): Release(m.group(2), m.group(1)) for m in pat.finditer(html)}
    releases = []
    for r in found.values():
        try:
            r.date  # noqa: B018 - skip names that aren't dates
        except ValueError:
            continue
        releases.append(r)
    return sorted(releases, key=lambda r: r.date)


def parse_os_index(html: str | bytes) -> list[str]:
    """Every OS-V<major>.<minor>.zip linked from the X7's OS index, as "3.11", oldest first."""
    if isinstance(html, bytes):
        html = html.decode("utf-8", "replace")
    found = set(re.findall(r'href="OS-V(\d+\.\d{2})\.zip"', html))
    return sorted(found, key=os_key)


def version_key(version: str) -> tuple[int, ...]:
    """ "3.11" -> (3, 11), "1.11.2" -> (1, 11, 2): compares as versions."""
    return tuple(int(part) for part in version.split("."))


os_key = version_key


def parse_sd2snes_tags(data: bytes) -> list[str]:
    """The sd2snes/FXPAK firmware versions in GitHub's tag list ([{"name": "v1.11.2"}, ...]), as
    "1.11.2", oldest first. The repo has no GitHub releases; its firmware versions are tags of
    three numbers (its two-number v4.x tags are 2017 leftovers, not firmware)."""
    tags = json.loads(data)
    if not isinstance(tags, list):
        raise ValueError(f"unexpected answer from GitHub: {str(tags)[:80]}")  # noqa: TRY004 - refresh_versions logs it
    found = {m.group(1) for t in tags if (m := re.fullmatch(r"v(\d+\.\d+\.\d+)", str(t.get("name", ""))))}
    return sorted(found, key=version_key)


# The X7 menu's "OS Version" (ED64/OS64.v64, disassembled for facts 2026-11-02): it stores the
# version in BCD, u16 0x0311 for V3.11, with `li a0,0x0311` (24 04 03 11), two instructions before
# `sh a0,66(s0)` (a6 04 00 42) into its system info. (Read as decimal, 0x11 gave "3.17".)
_OS_STORE = bytes.fromhex("a6040042")


def _bcd(b: int) -> bool:
    return b >> 4 <= 9 and b & 15 <= 9


def os_version(data: bytes) -> str | None:
    """The OS version compiled into an X7 menu file, "3.11"; None if the pattern isn't there."""
    if data[:4] == bytes.fromhex("37804012"):  # .v64 byte order: swap each 16-bit word
        data = bytes(b for i in range(0, len(data) - 1, 2) for b in (data[i + 1], data[i]))
    at = data.find(_OS_STORE)
    while at >= 0:
        for back in range(4, 20, 4):
            word = data[at - back : at - back + 4]
            if len(word) == 4 and word[:2] == b"\x24\x04" and _bcd(word[2]) and _bcd(word[3]) and word[2]:
                return f"{word[2]:x}.{word[3]:02x}"
        at = data.find(_OS_STORE, at + 4)
    return None


def efu_files(data: bytes) -> dict[str, tuple[int, int, int]]:
    """name -> (size, CRC32, offset of the data) for every file in an .efu."""
    files, i = {}, 0x40
    while i + 16 <= len(data):
        kind, nlen = data[i], struct.unpack_from("<H", data, i + 1)[0]
        size, crc = struct.unpack_from("<II", data, i + 8)
        if kind not in (0, 1) or not 0 < nlen <= 255:
            break
        name = data[i + 16 : i + 16 + nlen].decode("latin-1")
        if kind == 0:
            files[name] = (size, crc, i + 16 + nlen)
        i += 16 + nlen + (size if kind == 0 else 0)
    return files


def check_efu(data: bytes, device: int) -> None:
    """Refuse a download that isn't a whole .efu for this cart: never deliver a bad one."""
    if len(data) < 0x40 or data[2] != device or data[3] != 0xED:
        raise ValueError(f"not an .efu for device 0x{device:02X}")
    files = efu_files(data)
    if not files:
        raise ValueError("no files in the .efu")
    for name, (size, crc, at) in files.items():
        body = data[at : at + size]
        if len(body) != size or zlib.crc32(body) != crc:
            raise ValueError(f"{name}: CRC mismatch (truncated or corrupt download)")


def is_current(release: Release, built: datetime | None) -> bool | None:
    """The cart runs release (or newer); None when it didn't report a build date."""
    if built is None:
        return None
    return built.date() >= release.date - SLACK


def _http_get(url: str) -> bytes:
    # certifi's CA bundle: python.org's macOS Python has no CA store of its own
    # (CERTIFICATE_VERIFY_FAILED, 2026-11-02); this way every box checks the same way.
    tls = ssl.create_default_context(cafile=certifi.where())
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT, context=tls) as r:
        return r.read()


# Version feeds: (url, cache file, parse(bytes) -> versions)
FEEDS = {
    "x7-os": (OS_INDEX, OS_CACHE, parse_os_index),
    "sd2snes": (SD2SNES_TAGS, SD2SNES_CACHE, parse_sd2snes_tags),
}


class FirmwareStore:
    """The box's copy of each cart family's index and newest .efu (in the state folder)."""

    def __init__(
        self,
        root: Path,
        *,
        get: Callable[[str], bytes] = _http_get,
        clock: Callable[[], float] = time.time,
        log: Callable[[str], None] = lambda msg: None,
    ):
        self.root = Path(root)
        self.get = get
        self.clock = clock
        self.log = log
        self.lock = threading.Lock()  # carts of the same family share files

    def _index_path(self, site: str) -> Path:
        return self.root / f"{site}.json"

    def _cached(self, site: str) -> dict | None:
        try:
            return json.loads(self._index_path(site).read_text())
        except (OSError, ValueError):
            return None

    def refresh(self, profile) -> Release | None:
        """Check the index if it's a day old, download the newest .efu if missing. Network
        errors are logged and the last good index stays. Returns the newest release."""
        site, prefix = profile.firmware_site, profile.firmware_prefix
        if not site:
            return None
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            cached = self._cached(site)
            if cached is None or self.clock() - cached.get("checked", 0) >= CHECK_EVERY:
                try:
                    html = self.get(INDEX.format(site=site)).decode("utf-8", "replace")
                    files = [r.file for r in parse_index(html, prefix)]
                    if not files:
                        raise ValueError("no firmware files in the index")
                    cached = {"checked": self.clock(), "files": files}
                    self._index_path(site).write_text(json.dumps(cached))
                except (OSError, ValueError) as e:
                    self.log(f"firmware check failed: {e}")
            releases = self._releases(cached, prefix)
            if not releases:
                return None
            newest = releases[-1]
            target = self.root / newest.file
            if not target.is_file():
                try:
                    data = self.get(INDEX.format(site=site) + newest.file)
                    check_efu(data, profile.device)
                    part = target.with_suffix(".part")
                    part.write_bytes(data)
                    part.replace(target)
                    self.log(f"firmware downloaded: {newest.file} ({len(data)} bytes)")
                except (OSError, ValueError) as e:
                    self.log(f"firmware download failed: {e}")
            return newest

    @staticmethod
    def _releases(cached: dict | None, prefix: str) -> list[Release]:
        if not cached:
            return []
        html = "".join(f'href="{f}"' for f in cached.get("files", []))
        return parse_index(html, prefix)

    def refresh_os(self) -> str | None:
        """The X7's OS index, checked if it's a day old; the newest version ("3.11")."""
        return self.refresh_versions("x7-os")

    def newest_os(self) -> str | None:
        return self.newest_version("x7-os")

    def refresh_versions(self, feed: str) -> str | None:
        """A version feed (FEEDS: the X7's OS, sd2snes), checked if it's a day old; network errors
        are logged and the last good list stays. The newest version ("3.11", "1.11.2")."""
        url, cache, parse = FEEDS[feed]
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self.root / cache
            try:
                cached = json.loads(path.read_text())
            except (OSError, ValueError):
                cached = None
            if cached is None or self.clock() - cached.get("checked", 0) >= CHECK_EVERY:
                try:
                    versions = parse(self.get(url))
                    if not versions:
                        raise ValueError(f"no versions in {url}")
                    cached = {"checked": self.clock(), "versions": versions}
                    path.write_text(json.dumps(cached))
                except (OSError, ValueError) as e:
                    self.log(f"firmware check failed: {e}")
            return self._newest(cached)

    def newest_version(self, feed: str) -> str | None:
        """From what's already here (no network)."""
        with self.lock:
            try:
                return self._newest(json.loads((self.root / FEEDS[feed][1]).read_text()))
            except (OSError, ValueError):
                return None

    @staticmethod
    def _newest(cached: dict | None) -> str | None:
        versions = sorted((cached or {}).get("versions", []), key=version_key)
        return versions[-1] if versions else None

    # Which .efu files the box put on which cart: only those are ever removed (a file the
    # owner copied there is theirs; the first version removed one, 2026-11-02).
    def _ledger(self) -> dict:
        try:
            return json.loads((self.root / "delivered.json").read_text())
        except (OSError, ValueError):
            return {}

    def record(self, serial: str, file: str, present: bool) -> None:
        with self.lock:
            ledger = self._ledger()
            files = set(ledger.get(serial, []))
            files.add(file) if present else files.discard(file)
            ledger[serial] = sorted(files)
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "delivered.json").write_text(json.dumps(ledger))

    def for_cart(self, profile, built: datetime | None, serial: str = "") -> Firmware | None:
        """From what's already here (no network): the newest release and whether the cart runs it."""
        if not profile.firmware_site:
            return None
        with self.lock:
            releases = self._releases(self._cached(profile.firmware_site), profile.firmware_prefix)
            if not releases:
                return None
            newest = releases[-1]
            path = self.root / newest.file
            marker = None
            if path.is_file() and profile.firmware_marker:
                entry = efu_files(path.read_bytes()).get(profile.firmware_marker)
                if entry:
                    marker = (profile.firmware_marker, entry[0], entry[1])
            # Without the menu file to compare, only a build date that tracks the firmware says.
            current = is_current(newest, built) if profile.firmware_by_date else None
            if marker is None and current is None:
                return None
            delivered = frozenset(self._ledger().get(serial, []))
        return Firmware(newest, bool(current), path if path.is_file() else None, delivered, marker)
