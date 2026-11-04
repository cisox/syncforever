# EverDrive-N8 PRO USB protocol

What the host side of `syncforever` speaks, and where each fact comes from. **Status** says
whether we have seen it on our own cart (`verified`) or only in a reference
(`ref`). Update the column as `docs/test-results.md` fills in.

The Mega EverDrive PRO speaks the same protocol, big-endian: `cartridge-compatibility.md`,
`carts.py`. The other supported carts don't use it: the X7 is `x7-protocol.md`, the FXPAK PRO
`snes-plan.md` and `fxpak.py`. What our app and the box say to each other is `app-protocol.md`.

## Sources (read 2026-10-30)

| Key | Repo @ commit | License | Use |
|---|---|---|---|
| **edio** | krikzz/edn8-pro-pub @ 1772250, `edio/everdrive.{c,h}`, `errors.h` | MIT | Console-side command library. Same command set as USB, sent through a FIFO. |
| **edlink** | krikzz/edlink @ 44ed62d | MIT | Official host tool. **The device/link sources (`Device/*.cs`, `Link.cs`) are listed in the csproj but not published**, so the host-side framing comes from the two below. |
| **NESCD** | ThroatyMumbo/NESCD @ 437bcda, `firmware/src/edn8.{c,h}` | MIT | RP2350 USB host talking to an N8 PRO; tested on hardware. |
| **NESPC** | ThroatyMumbo/NESPC @ 13e6cd8, `edn8.py` | **GPL-3.0** | Python host. Facts only; **no code copied** (we are not GPL). |

## Transport

- USB CDC-ACM serial, VID `0x38DF` (KRIKZZ-IT), PID `0x0017`. *ref (NESCD, NESPC)*; *verified*
  on our N8 since firmware v25.1123. Before that it enumerated as `0483:5740` (ST VCP).
  `transport.is_everdrive` accepts both.
- Baud rate is ignored by CDC; both references open at 921600. *ref*
- Little-endian throughout (edlink runs the N8 with `SwapEndians=false`). *ref*; *verified*

## Framing

Every command is a 4-byte header: `'+'`, `'+' ^ 0xFF`, `cmd`, `cmd ^ 0xFF`. The MCU
discards bytes until a valid header, so **66 zero bytes** clear any half-sent header
(the "wake"). *ref (NESCD, NESPC)*

Strings: `u16 length` + bytes, no terminator. ASCII on send.

## Handshake

1. Wake (66 × `0x00`), wait ~50 ms, drop pending input.
2. Send `STATUS2 (0x40)` then `STATUS (0x10)` back to back.
3. Read 4 bytes: `[0x5A, protocol, device, x]`. N8 PRO = protocol `0x06`, device `0x17`.
4. Read 2 bytes: the reply to the `STATUS` sent in step 2.
5. `STATUS` again → `[status, 0xA5]`. Second byte must be `0xA5`.

*ref (NESCD `identify()`, NESPC `_identify`)*; *verified 2026-10-31*. Retry once on failure (edlink does).

## Commands used by syncforever

`check` = send `STATUS`, read `[code, 0xA5]`, nonzero code = error.

| ID | Name | Request after header | Reply | Status |
|---|---|---|---|---|
| `0x10` | STATUS | — | `[code, 0xA5]` | verified |
| `0x14` | RTC_GET | — | 6 BCD bytes: year-2000, month, day, hour, minute, second | verified |
| `0x15` | RTC_SET | the same 6 bytes | none | verified |
| `0x19` | MEM_RD | `u32 addr, u32 len, u8 exec (0)` | `len` bytes | verified (MapConfig only, `N8Pro.mem_rd`) |
| `0x1A` | MEM_WR | `u32 addr, u32 len, u8 exec (0)`, data | none | verified (FIFO, MapConfig) |
| `0x26` | SYS_INF | — | 64 bytes; see below | verified |
| `0x29` | HOST_RST | `u8` 0 release, 1 soft, 2 hard | none | verified on the Mega; not used on the N8 |
| `0x40` | STATUS2 | — | `[0x5A, proto, dev, x]` | verified |
| `0xC5` | F_DIR_LD | `u8 opts` (1 = sorted, 2 = hide system; the menu uses `0x03`), `str path` | check | verified |
| `0xC6` | F_DIR_SIZE | — | `u16 count` | verified |
| `0xC8` | F_DIR_GET | `u16 start, u16 count (≤32), u16 name_max` | per entry: `u8 code`, then FileInfo | verified |
| `0xC9` | F_FOPN | `u8 FatFs mode`, `str path` | check | verified |
| `0xCA` | F_FRD | `u32 len` | per 4096-byte block: `u8 code`, data | verified |
| `0xCC` | F_FWR | `u32 len`, then per 1024-byte block: **wait for `u8 ack`**, send block | check | verified |
| `0xCE` | F_FCLOSE | — | check | verified |
| `0xD0` | F_FINFO | `str path` | `u8 code`, then FileInfo if 0 | verified (the name comes back empty) |
| `0xD2` | F_DIR_MK | `str path` | check; `0x08` = already exists | verified; `0x08` ref |
| `0xD3` | F_DEL | `str path` | check; `0x04`/`0x05` = not there | verified (`0x05` seen) |

FileInfo = `u32 size, u16 FAT date, u16 FAT time, u8 attr (0x10 = dir), str name`.

**Correction to the project brief:** `0xC5` is *dir load* (not "open"), `0xC6` is *dir
size* (not "read"), and `0xC8` is *dir get* (not "close"). There is no dir-close command.

### SYS_INF (64 bytes)

Our N8 fits the plain edio `SysInfoIO` layout (`carts.py`, *verified*), not the offsets
NESCD and NESPC use (`sw_ver` @44): serial `u32` @20 and @24, boot counter @28, game
counter @32, build date @36, `sw_ver` @40, `hw_ver` @42, `boot_ver` @44, `device_id` @46,
cart form @52 (0 = NES), `pwr_sys` @53, `pwr_usb` @54. The Mega's layout differs (`carts.py`).
The build date didn't change with firmware v25.1123, so it can't tell the N8's firmware
(`firmware.py`). **Lead for the VBUS question:** dump `info --raw` and compare the power
fields.

## Quirks

- **Writes are all-or-wedge.** Once `F_FWR` declares a length, the MCU treats the next
  `len` bytes as payload. If the host stops short, every later byte (including the next
  wake and command headers) is consumed as file data, and only a power cycle recovers
  the cart. A nonzero ack means the cart abandoned the command, so nothing is owed.
  *ref (NESCD)*. syncforever defers Ctrl-C and SIGTERM during a write and caps each `F_FWR`
  at 64 KiB.
- **`F_DIR_GET` returns at most 32 entries per call.** Asking for 33 returns `0x82`
  (ERR_OUT_OF_PAGE). *ref (NESPC, "measured 2026-10-16")*
- **edlink never sends a write of exactly 512 bytes;** it sends 256 instead. The reason
  is unknown. We mirror it. *ref (NESPC comment on edlink `Link.TxData`)*
- `mem_wr` with exec byte `0xAA` wedges the parser, and `CMD_HARD_RESET (0x12)` is not
  valid on this cart. syncforever uses neither. *ref (NESCD)*
- `STATUS` is the sticky result of the last command, not a menu/game flag (hardware,
  2026-10-31; NESCD's `edn8.h` read it as one). The write gate is the menu ping `*t` -> `k`.
- **`F_DIR_LD` over USB replaces the menu's loaded folder**, and the menu's own entry count
  can then break it (`Error:81`). Route it through `N8Pro.list_dir` (`CLAUDE.md`,
  `test-results.md`). Don't ping or open files at a menu that may be in use (same places).

## Menu commands (FIFO `*x`), per edlink

From Krikzz's `edlink.exe` (mega-ed-pub copy, covers every family; decompiled for facts
only, 2026-11-01). Sent with `MEM_WR` to `ADDR_FIFO`, two bytes `*` + letter, read by the menu.

| Cart | Command | Meaning (edlink) | Our status |
|---|---|---|---|
| N8 | `*t` | test: reply `k` | verified, the write gate |
| N8 | `*n` + str | install ROM at path: u8 status, then u16 mapper | verified (path without the leading slash works; edlink sends it with one) |
| N8 | `*s` | start the installed ROM | verified |
| N8 | `*r` | reboot: edlink then reads one byte | **don't**: on our firmware the console goes black until a reset |
| N8 | `*h` | halt | defined, never used by edlink; untested |
| N8 | `*v` | VRAM dump: 2 KB nametable + 16 B palette; edlink then `MEM_RD`s 8 KB at `0xFE0000` (the menu's CHR) | untested |
| Mega | `*t` | test: reply `k` | verified |
| Mega | `*i` + str | install ROM at "/path": u8 status only | verified (Altered Beast) |
| Mega | `*s` | start | verified |
| Mega | `*v` | VRAM dump: u32 address back, then `MEM_RD` 64 KB VRAM + 128 B palette | untested |
| Mega | reset to menu | host reset (soft/hard pin), ConfigReset, release; the menu sends `r` when up | verified 2026-11-02 (`EverDrive.reset_to_menu`); edlink's Mega `run` does this before `*i` |

edlink's own N8 `run` is `*t`, `*n`, `*s`; its Mega `run` is reset-to-menu, `*i`, `*s`.

## Available but unused (candidates for sync)

From edio, never exercised over USB by any reference. Verify before relying on them.

| ID | Name | Request | Reply | Why it matters |
|---|---|---|---|---|
| `0xCF` | F_FPTR | `u32 offset` | check | Seek, for resumable transfers |
| `0xD1` | F_FCRC | `u32 len, u32 crc_base` | `u8 code, u32 crc` | On-cart CRC-32 of the next `len` bytes of the open file (`ops.cart_crc`, `sfe crc`); `crc_base` is the CRC so far. **Works on the Mega (2026-11-04):** matches zlib's CRC-32, ~4 MB/s. A plan's first look at a file uses it there instead of reading the file (`CartProfile.crc`). N8 untried |
| `0xD5` | F_AVB | — | `u64` | Bytes left in open file |
| `0xD6` | F_FCP | `u8 mode, str src, str dst` | check (the STATUS after it answers once the copy is done) | Copy on the cart (`N8Pro.copy`, `sfe cp`, mode `FA_WRITE \| FA_CREATE_NEW`). **Works on the Mega (2026-11-04)**, into another folder too: a sync's moves there are this copy plus a delete, no data over USB |
| `0xD7` | F_FMV | `u8 mode, str src, str dst` | check | Rename/move (`N8Pro.move`, `sfe mv`). **Doesn't work on the N8 (v25.1123, 2026-11-04):** modes 0, 2, 6 and 0x0A all answer success and move nothing, within a folder or across. The Mega the same, also with full paths or a folder as dst (2026-11-04). Syncs use F_FCP + delete there instead |

## Error codes

FatFs `FRESULT` 1–19 (`0x04` NO_FILE, `0x05` NO_PATH, `0x07` DENIED, `0x08` EXIST, …),
the cart's own `0x40`–`0x8D` (edio `errors.h`), and SD-card errors `0xC0`–`0xDA`.
`protocol.error_name()` maps them.
