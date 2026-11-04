"""EverDrive-N8 PRO USB protocol: constants, framing and error types.

Every value here is sourced in docs/usb-protocol.md. Values marked UNVERIFIED there
have not yet been observed on our own cartridge.
"""

import struct
from enum import IntEnum

USB_VID = 0x38DF  # KRIKZZ-IT
USB_PID = 0x0017
# STMicroelectronics stock virtual COM port IDs, used by older N8 PRO firmware.
ST_VCP_VID = 0x0483
ST_VCP_PID = 0x5740

PROTOCOL_ID_N8 = 0x06
DEVICE_ID_N8_PRO = 0x17
STATUS2_KEY = 0x5A  # first byte of the CMD_STATUS2 reply
STATUS_KEY = 0xA5  # second byte of every CMD_STATUS reply

# The MCU discards bytes while it waits for '+', so this many zeros flush any
# half-sent header (longer than the longest header).
WAKE_ZEROS = 66

ACK_BLOCK = 1024  # F_FWR: the cart sends one ack byte before each block this size
READ_BLOCK = 4096  # F_FRD: the cart sends one status byte before each block this size
DIR_GET_MAX = 32  # F_DIR_GET answers ERR_OUT_OF_PAGE for more entries than this
NAME_MAX = 255

# F_DIR_LD options (edio/everdrive.h). The MCU has ONE loaded-directory slot, shared
# with the menu: a USB F_DIR_LD replaces the menu's folder and the menu shows Error:81
# (ERR_OUT_OF_DIR) on its next scroll. Reloading the menu's folder with the menu's own
# options (sorted + hide system, which hides EDN8) repairs it. Verified 2026-10-31.
DIR_OPT_SORTED = 0x01
DIR_OPT_HIDESYS = 0x02
MENU_DIR = ""
MENU_DIR_OPTS = DIR_OPT_SORTED | DIR_OPT_HIDESYS

SYS_INF_SIZE = 64

# Host -> menu FIFO in the cart's config space (not PRG/SRM). The ROM-select menu
# answers b"*t" with b"k"; a running game never answers. Verified 2026-10-31.
ADDR_FIFO = 0x1810000
# MapConfig (edio): 32 bytes of cheats, then map_idx @ +32 ... 42 bytes. The only area
# N8Pro.mem_rd reads: PRG/SRM share the running program's bus, and host DMA there crashes it.
ADDR_CFG = 0x1800000
CFG_SIZE = 42
CFG_MAP_IDX = 32  # 255 = the menu's own mapper
MENU_PING = b"*t"
MENU_PONG = b"k"
# Menu commands, same FIFO. Verified 2026-10-31 (8 Eyes): *n + string(path, no leading
# slash) -> u8 status, u16 mapper index after the menu loads the ROM (0.46 s; NESPC
# allows 30 s); *s then starts it. NEVER send *r (reboot): the console goes black.
MENU_INSTALL = b"*n"
MENU_START = b"*s"
INSTALL_TIMEOUT = 30.0


class Cmd(IntEnum):
    STATUS = 0x10
    RTC_GET = 0x14  # -> 6 BCD bytes: year-2000, month, day, hour, minute, second (edlink DeviceIO_V1)
    RTC_SET = 0x15  # <- the same 6 bytes; no reply
    MEM_RD = 0x19  # <- addr:u32, len:u32, exec:u8 (0); -> len bytes (edlink DeviceIO_V1)
    MEM_WR = 0x1A
    HOST_RST = 0x29  # <- u8 0 release, 1 soft (reset button), 2 hard (cold boot). Mega only so far
    SYS_INF = 0x26
    STATUS2 = 0x40
    F_DIR_LD = 0xC5
    F_DIR_SIZE = 0xC6
    F_DIR_GET = 0xC8
    F_FOPN = 0xC9
    F_FRD = 0xCA
    F_FWR = 0xCC
    F_FCLOSE = 0xCE
    F_FINFO = 0xD0
    F_DIR_MK = 0xD2
    F_DEL = 0xD3
    F_FCRC = 0xD1  # CRC of the open file on the cart: u32 len, u32 base -> u8 status, u32 crc
    F_FCP = 0xD6  # copy on the cart: u8 mode, str src, str dst (NESPC sends FA_WRITE | FA_CREATE_NEW)
    F_FMV = 0xD7  # move/rename: u8 mode, str src, str dst (edio's ed_cmd_file_move)


# FatFs open modes, passed straight through by the cart.
FA_READ = 0x01
FA_WRITE = 0x02
FA_OPEN_EXISTING = 0x00
FA_CREATE_NEW = 0x04
FA_CREATE_ALWAYS = 0x08

AT_DIR = 0x10

# STATUS reports the result of the last command and keeps it until the next
# one (verified 2026-10-31). It does NOT say whether the menu or a game is up.

# FatFs FRESULT codes 1-19, then the cart's own codes from edio/errors.h.
_ERROR_NAMES = {
    0x01: "FR_DISK_ERR",
    0x02: "FR_INT_ERR",
    0x03: "FR_NOT_READY",
    0x04: "FR_NO_FILE",
    0x05: "FR_NO_PATH",
    0x06: "FR_INVALID_NAME",
    0x07: "FR_DENIED",
    0x08: "FR_EXIST",
    0x09: "FR_INVALID_OBJECT",
    0x0A: "FR_WRITE_PROTECTED",
    0x0B: "FR_INVALID_DRIVE",
    0x0C: "FR_NOT_ENABLED",
    0x0D: "FR_NO_FILESYSTEM",
    0x0F: "FR_TIMEOUT",
    0x10: "FR_LOCKED",
    0x11: "FR_NOT_ENOUGH_CORE",
    0x12: "FR_TOO_MANY_OPEN_FILES",
    0x13: "FR_INVALID_PARAMETER",
    0x40: "ERR_UNXP_STAT",
    0x42: "ERR_PATH_SIZE",
    0x43: "ERR_NAME_SIZE",
    0x44: "ERR_OUT_OF_MEMORY",
    0x4A: "ERR_NULL_PATH",
    0x80: "ERR_STR_SIZE",
    0x81: "ERR_OUT_OF_DIR",
    0x82: "ERR_OUT_OF_PAGE",
    0x86: "ERR_LINK_TOUT",
}
FR_NO_FILE = 0x04
FR_NO_PATH = 0x05
FR_EXIST = 0x08
ERR_OUT_OF_DIR = 0x81
ERR_OUT_OF_PAGE = 0x82


def error_name(code):
    if 0xC0 <= code < 0xD0:
        return f"DISK_ERR_INIT_{code:02X}"
    if 0xD0 <= code < 0xE0:
        return f"DISK_ERR_IO_{code:02X}"
    return _ERROR_NAMES.get(code, f"UNKNOWN_{code:02X}")


class EverDriveError(Exception):
    """Base for everything the link can raise."""


class LinkError(EverDriveError):
    """Timeout, short read, or a reply that does not fit the protocol."""


class DeviceError(EverDriveError):
    """The cart answered with a nonzero status code."""

    def __init__(self, op, code):
        self.op = op
        self.code = code
        super().__init__(f"{op}: 0x{code:02X} {error_name(code)}")


class WedgedError(EverDriveError):
    """A declared payload was cut short. The cart's parser is now consuming
    our later bytes as payload; only a power cycle recovers it."""


class UnsafeStateError(EverDriveError):
    """The cart is not in a state where writes are allowed."""


def frame(cmd):
    return bytes([ord("+"), ord("+") ^ 0xFF, cmd, cmd ^ 0xFF])


def u8(v):
    return struct.pack("<B", v)


def u16(v):
    return struct.pack("<H", v)


def u32(v):
    return struct.pack("<I", v)


def string(s):
    b = s.encode("ascii")
    return u16(len(b)) + b
