"""What differs between EverDrive PRO carts on the USB link (docs/cartridge-compatibility.md).

The command set and framing are shared. Each cart sends and expects numbers in its console
CPU's byte order (the N8's 6502 little-endian, the Genesis's 68000 big-endian), STATUS replies
`[code, 0xA5]` or `[0xA5, code]` to match, and lays out SYS_INF a little differently. The cart
says which it is in STATUS2's protocol and device bytes (EverDrive.connect).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class SysInfLayout:
    """Offsets into the 64-byte SYS_INF reply (edio SysInfoIO, packed)."""

    serial: int  # serial_g, serial_l, boot_ctr, game_ctr: four u32
    built: int  # FAT date, FAT time
    sw_ver: int
    hw_ver: int
    boot_ver: int
    device: int
    pwr_sys: int
    pwr_usb: int
    cart_form: int | None = None  # N8: famicom/nes


@dataclass(frozen=True)
class CartProfile:
    name: str
    console: str
    protocol: int
    device: int
    endian: str  # struct prefix: "<" little, ">" big
    system_dir: str  # the menu's folder on the SD card
    sysinf: SysInfLayout
    menu_ping: bool  # the menu answers `*t` with `k`
    menu_launch: bool  # the launch commands below + `*s` start a ROM (tested on this cart)
    writes: bool  # file writes tested on this cart (until then: refused)
    rom_suffixes: tuple[str, ...]
    # The MSU games this cart plays (msu.py: "msu1", "msu-md"; "" none), synced when the
    # console's `msu` setting is on.
    msu: str = ""
    save_states: bool = False  # it makes save states (the `states` setting: sfe setup asks)
    # How a sync moves a file instead of sending it again: "rename" (sfe mv), "copy" (the cart
    # copies it to the new name, then the box deletes the old: sfe cp), or "" (it sends it again).
    # Only what has worked on the cart. move_folders: a rename can change folders.
    move: str = ""
    move_folders: bool = True
    # The cart checksums its own files (F_FCRC, ops.cart_crc): a first look at a file needs no
    # read over USB. Only what has worked on the cart (sfe crc).
    crc: bool = False
    # Loading a ROM before `*s`: the command, whether the path keeps its leading slash, and
    # whether the reply carries a u16 mapper after the status byte.
    # The recent list (under system_dir), and the save files worth checking by name where
    # listing a game's folder isn't safe (saves.py).
    recent_file: str = "sysdata/recent.bin"
    save_files: tuple[str, ...] = ("bram.srm",)
    app_ext: str | None = None  # our app's file type on this cart (apps.py); None: no app
    key: str = ""  # the config section for this console's folders: [nes], [genesis]
    short: str = ""  # in log lines
    menu_reset: bool = False  # reset_to_menu (host reset + config reset, menu answers `r`) tested
    install_cmd: bytes = b"*n"
    install_slash: bool = False
    install_mapper: bool = True
    # Krikzz's firmware folder (krikzz.com/pub/support/<site>/pro-series/firmware/) and file
    # prefix (<prefix>vYY.MMDD.efu); "" = no firmware updates (firmware.py).
    firmware_site: str = ""
    firmware_prefix: str = ""
    # The menu file an .efu installs (firmware.py compares its CRC32 with the card's copy), and
    # whether SYS_INF's build date tracks the firmware (the Mega's does; the N8's stayed
    # 2025-09-30 across an update to v25.1123, 2026-11-02).
    firmware_marker: str = ""
    firmware_by_date: bool = False
    # Saves sit flat in gamedata as <ROM name>.<type> (the X7), not one folder per game.
    flat_saves: bool = False
    # The box keeps our app's file on the card up to date when the app connects (apps.py).
    app_install: bool = True
    # A version feed (firmware.FEEDS) the app's firmware line compares with what the cart runs:
    # "x7-os" (the X7's OS, from its menu file), "sd2snes" (the FXPAK's, from INFO).
    firmware_feed: str = ""
    # Where flat saves live, under system_dir (saves.backup_flat): the X7's gamedata; the
    # FXPAK's battery saves, save states and cheats.
    save_dirs: tuple[str, ...] = ("gamedata",)

    def pack(self, fmt: str, *values) -> bytes:
        return struct.pack(self.endian + fmt, *values)

    def unpack(self, fmt: str, data: bytes, offset: int = 0) -> tuple:
        return struct.unpack_from(self.endian + fmt, data, offset)

    def status_reply(self, code: int) -> bytes:
        """STATUS on the wire: the code and the 0xA5 key, in this cart's u16 order."""
        return bytes([code, 0xA5]) if self.endian == "<" else bytes([0xA5, code])

    def status_code(self, reply: bytes) -> int | None:
        """The code from a STATUS reply, or None if the key isn't where it should be."""
        code, key = (reply[0], reply[1]) if self.endian == "<" else (reply[1], reply[0])
        return code if key == 0xA5 else None


N8_PRO = CartProfile(
    name="EverDrive-N8 PRO",
    console="NES",
    protocol=0x06,
    device=0x17,
    endian="<",
    system_dir="EDN8",
    # Firmware built 2025-09-30: device_id at 46 (NESCD/NESPC read sw_ver at 44).
    sysinf=SysInfLayout(
        serial=20, built=36, sw_ver=40, hw_ver=42, boot_ver=44, device=46, pwr_sys=53, pwr_usb=54, cart_form=52
    ),
    menu_ping=True,
    menu_launch=True,
    writes=True,
    rom_suffixes=(".nes",),
    save_files=("bram.srm", "cheats.txt"),  # battery save, the Cheats Editor's codes (nesos.nes)
    app_ext=".nes",
    key="nes",
    short="N8 PRO",
    firmware_site="everdrive-n8",
    firmware_prefix="edn8-pro-fw-v",
    firmware_marker="EDN8/nesos.nes",
)

MEGA_PRO = CartProfile(
    name="Mega EverDrive PRO",
    console="Genesis",
    protocol=0x05,
    device=0x18,
    endian=">",
    system_dir="MEGA",
    # mega-ed-pub SysInfoIO: asm date/time at 36, sw date/time at 40 (seen 2026-11-01).
    sysinf=SysInfLayout(serial=20, built=40, sw_ver=44, hw_ver=46, boot_ver=48, device=50, pwr_sys=56, pwr_usb=57),
    menu_ping=True,  # answered `k` at an idle menu (2026-11-01)
    menu_launch=True,  # `*i` + `*s` started Altered Beast (2026-11-01)
    writes=True,  # mkdir, 200 KB upload + readback, delete: fine at the menu (2026-11-01)
    menu_reset=True,  # `r` 0.35 s after release, then *i/*s launched our app (2026-11-02)
    # What the owner syncs to it (2026-11-01): Mega Drive, 32X, Master System, Game Gear,
    # SG-1000, Sega CD (cue + bin/iso/img/wav tracks, one folder per disc; the manual: "bin+cue,
    # cue+wav, multi-bin", img/iso play as bin), NES (MEGA/edapp/nes plays it), and IPS patches
    # (the menu applies game.ips to the ROM of the same name next to it).
    rom_suffixes=(
        *(".md", ".bin", ".gen", ".smd", ".32x", ".sms", ".gg", ".sg", ".sc"),
        *(".cue", ".iso", ".img", ".wav", ".nes", ".ips"),
    ),
    # edlink (decompiled for facts): `*i` + "/path" -> status byte only.
    recent_file="sysdata/recent.dat",
    # Battery save, Sega CD backup RAM and cartridge RAM, save-state slots (00.SAV seen; 10 slots
    # assumed), cheats (the menu's Cheats Editor), per-game settings (names from megaos.dat).
    save_files=(
        *("bram.srm", "cd-bram.brm", "cd-cart.srm", "cd-cart128.srm"),
        *(f"{i:02d}.SAV" for i in range(10)),
        *("cheats.txt", "config.txt"),
    ),
    app_ext=".md",
    move="copy",  # F_FMV answers success and moves nothing; F_FCP copies (2026-11-04)
    crc=True,  # matches zlib's CRC-32, ~4 MB/s against ~700 KiB/s to read it (2026-11-04)
    save_states=True,
    msu="msu-md",  # the PRO plays MSU-MD (and MD+): Game.md + Game.cue + the cue's .bin
    key="genesis",
    short="Mega PRO",
    firmware_site="mega-everdrive",
    firmware_prefix="mega-pro-fw-v",
    firmware_marker="MEGA/megaos.dat",
    firmware_by_date=True,
    install_cmd=b"*i",
    install_slash=True,
    install_mapper=False,
)

# The EverDrive-64 X7 has no PRO-style USB protocol: its menu only boots ROMs, and our app serves
# the files (x7.py). Not in PROFILES, which STATUS identifies; the SYS_INF layout is unused.
X7 = CartProfile(
    name="EverDrive-64 X7",
    console="N64",
    protocol=0,
    device=0,
    endian=">",
    system_dir="ED64",
    sysinf=SysInfLayout(serial=0, built=0, sw_ver=0, hw_ver=0, boot_ver=0, device=0, pwr_sys=0, pwr_usb=0),
    menu_ping=False,
    menu_launch=False,
    writes=True,  # through our app (2026-11-02)
    # N64 ROMs; the emulated systems' extensions come from ED64/emu/*.v64 (docs/n64-plan.md).
    rom_suffixes=(".z64", ".v64", ".n64"),
    # 1025-byte path slots, not the PRO carts' 512 (the owner's card, 2026-11-02).
    recent_file="sysdata/recent.dat",
    save_files=(),
    app_ext=".z64",
    key="n64",
    short="X7",
    flat_saves=True,
    firmware_feed="x7-os",
)

FXPAK = CartProfile(
    name="FXPAK PRO",
    console="SNES",
    protocol=0,
    device=0,
    endian=">",
    system_dir="sd2snes",
    sysinf=SysInfLayout(serial=0, built=0, sw_ver=0, hw_ver=0, boot_ver=0, device=0, pwr_sys=0, pwr_usb=0),
    menu_ping=False,
    menu_launch=False,
    writes=True,  # the cart's own USB file service (fxpak.py)
    rom_suffixes=(".sfc", ".smc", ".swc", ".fig", ".bs"),
    msu="msu1",
    save_states=True,
    move_folders=False,  # its MV renames within a folder (usbinterface.c)
    save_files=(),
    app_ext=".sfc",  # snes/: talks through cart memory (fxapp.py)
    firmware_feed="sd2snes",
    key="snes",
    short="FXPAK",
    flat_saves=True,
    # .srm battery saves and .gtc Game Boy clocks; <ROM>NN.state save states (slot NN);
    # cheats (firmware 1.11.2 source, the owner's card 2026-11-03).
    save_dirs=("saves", "states", "cheats"),
)

PROFILES = {(p.protocol, p.device): p for p in (N8_PRO, MEGA_PRO)}

CONSOLES = tuple(dict.fromkeys(p.key for p in (*PROFILES.values(), X7, FXPAK)))  # config sections
