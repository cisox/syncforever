# Cartridge compatibility

**Scope:** the carts the owner has: the EverDrive-N8 PRO, the Mega EverDrive PRO, the
EverDrive-64 **X7** and the FXPAK PRO, all supported and tested on hardware (2026-11). The
EverDrive-64 PRO comes once we have one: nothing N64 has been tried on a PRO. Other carts are
listed at the end with the reason each is out of scope.

Evidence levels: **verified** = seen on our own cart; **source** = read in published
code; **inferred** = our reasoning from sources; **unknown**.

## Summary

| Cart | Status | USB on cart | Protocol family | Main open question |
|---|---|---|---|---|
| EverDrive-N8 PRO (NES) | **Supported**, tested on hardware | Micro-USB, top edge, centred (owner) | Krikzz, protocol `0x06`, little-endian | – |
| Mega EverDrive PRO (Genesis) | **Supported**, tested on hardware | Micro-USB, top edge (owner) | Krikzz, protocol `0x05`, device `0x18`, **big-endian (verified)** | – |
| EverDrive-64 X7 (N64) | **Supported**, tested on hardware: the card work runs in our N64 app (`n64-plan.md`) | USB on the cart | Our own app protocol over the X7's USB FIFO (`x7-protocol.md`) | – |
| FXPAK PRO (SNES) | **Supported**, tested on hardware (`snes-plan.md`) | Micro-USB (owner) | sd2snes firmware's USB file service (usb2snes) | – |
| EverDrive-64 PRO (N64) | Planned, once we have the hardware; not yet owned | "High-speed USB" per the product page; position unknown | Krikzz (edlink has an ED64 adapter) | Everything: no public dev repo found |
| Turbo EverDrive PRO (PC Engine) | Next; owned, waiting on an AV cable (2026-11-04) | Micro-USB, top (owner) | Krikzz (edlink has `DEV_TED`) | Everything: no public dev repo found |

## What the Krikzz PRO carts share

Edlink is described as "a single unified utility … for all EverDrive PRO and CORE
cartridges" (krikzz/edlink README, *source*). Its project file lists a separate adapter
per family: `DEV_EDN8`, `DEV_MEGA`, `DEV_ED64`, `DEV_TED`, `DEV_GBA`, `DEV_SMC`. The
adapters' sources are not published, only the file names.

Common to N8 and Mega (*source*: both `edio/everdrive.c`):

- Header `'+'`, `~'+'`, `cmd`, `~cmd`; the same command IDs (`0x10` STATUS, `0x26` SYS_INF,
  `0x40` STATUS2, `0xC5`–`0xD3` file commands); and the same 1024-byte write ack blocks.
- Status word `0xA5xx`, checked as `(status & 0xFF00) == 0xA500`.
- Strings sent as a 2-byte length followed by the bytes.
- The Gen2 identify handshake: STATUS2 + STATUS, key `0x5A`, then a protocol byte.
  NESPC/NESCD read `0x05` as Mega and `0x06` as N8 (*source*).

The Mega adds commands the N8 lacks: `0xD4` F_SEEK_IDX, `0xD8` F_SEEK_PAT, `0xD9`
F_DTEST (folder exists), `0xDA` F_FTEST (file exists). The Mega's `everdrive.c` also
has `ed_halt_app()`, a relative of the N8's `ed_halt_usb()`.

## Per cart

### EverDrive-N8 PRO

Covered in full by `usb-protocol.md`, results in `test-results.md`. USB VID `0x38DF`, PID
`0x0017` since firmware v25.1123 (`0483:5740` before), device ID `0x17` (*verified*).

### Mega EverDrive PRO

- **Source:** krikzz/mega-ed-pub (MIT): `edio/everdrive.c`, plus `usb-cmd/` and
  `edio-cmd/` sample scripts, which wrap `edlink.exe`.
- **Identified 2026-11-01 (read-only):** USB `0483:5740`, product "Mega EverDrive" (the
  ST VCP ID, like our N8; `38DF:0018` since firmware v26.0925). STATUS2 `5a 05 18 00`: protocol `0x05`, device `0x18`. STATUS
  `a5 00`. SYS_INF (64 bytes, mega-ed-pub `SysInfoIO` layout: device_id at offset 50, not 46
  as on the N8) parses only big-endian: serial `000527BD.00003C85`, boot_ctr 54, game_ctr 38,
  sw_ver `0x0113`, sw_date FAT `0x598D` = 2024-12-13. RTC_GET answers 6 BCD bytes like the
  N8 (`26 11 01 21 40 51`).
- **From the sources (mega-ed-pub `7064f45`, MIT; facts only):** system folder `/MEGA`
  (N8: `/EDN8`); memory map like the N8's: `ADDR_FIFO` `0x1810000`, `ADDR_CFG` `0x1800000`,
  battery RAM at `0x1080000`. **edapp** (firmware 4.08+): a file whose extension matches a
  folder in `MEGA/edapp` runs through that folder's `app.md`, which can opt out of the
  recently-played list. The sample `usb-cmd/` scripts reset the console over USB (`reset
  --mode soft`: the reset button via cart pin B27; `--mode hard`: cold boot via B2) and run
  a ROM (`run --file`), all through `edlink.exe`, whose wire commands aren't published
  (read from the exe later: "Launching" below). Don't guess them: on the N8,
  `CMD_HARD_RESET` `0x12` wedges the cart.
- **Byte order: big-endian (verified over USB, see above).** The Genesis's 68000 CPU stores numbers
  big-endian, and `everdrive.c` writes them with `ed_fifo_wr(&len, 4)` without
  swapping. The status check reads a native u16 against `0xA500`, so on the wire the
  reply is `[0xA5, code]`, the reverse of the N8's `[code, 0xA5]`. NESPC notes edlink
  runs the N8 with `SwapEndians=false`, which implies some device runs with it true.
  The USB side follows it (SYS_INF above).
- **Saves (verified 2026-11-01):** under `MEGA/gamedata/<rom>/`: `bram.srm` (battery save),
  `00.SAV` (save state, slot 00), `cd-bram.brm` (Sega CD backup RAM). `sfe backup --all` stored
  all three, SHA-256 identical to the card-reader backup.
- **Adapter (built 2026-11-01):** `carts.py` profiles (byte order, STATUS order, SYS_INF
  layout, system folder, ROM types), picked from STATUS2's IDs at connect; the fake cart
  takes the same profile (`tests/test_mega.py`). On the Mega, `sfe info` and `sfe stat`
  work (host requests big-endian too: `stat /MEGA` answered), and so do listings (no menu
  error after), the menu ping (`*t` -> `k`), launching (`*i` + `*s`) and writes (2026-11-01,
  after an SD backup to `~/Desktop/MDBackup`).
- **Save states (owner, 2026-11-01):** the Mega has them (MapConfig `ss_key_save/load`). They
  are stored in `gamedata/<rom>/` and backed up with the saves (above).
- **Launching (from edlink.exe, decompiled for facts, 2026-11-01):** the menu ignores the
  N8's `*n`. edlink's Mega `run`: (1) reset to the menu: host reset soft/hard, ConfigReset,
  release; the menu sends `r` (0x72) when it's up; (2) `*i` + string (u16 length in the cart's
  byte order + ASCII path **with** its leading slash) -> one status byte, 0 = OK (no mapper,
  unlike the N8's `*n`); (3) `*s` starts it. ROMs from the host are first copied to
  `usb-games/`. Other menu letters: `*v` VRAM dump (u32 address back, then MEM_RD 64 KB VRAM
  + 128 B palette), `*t` ping. Device IDs: 0x18 PRO, 0x25 CORE. edlink shows the "mcu core"
  version as SYS_INF's sw date read as a version (e.g. 26.0924).
- **Menu themes (`MEGA/sysdata/theme.bgr`, the active one; more in `MEGA/themes/`), decoded
  2026-11-01 from the owner's backup:** big-endian words. Header: magic `0xED18`, file size,
  palettes offset (`0x80`: 4 x 16 CRAM colours, `0x0BGR`), tiles offset (`0x100`: 256 tiles of
  32 bytes, 4bpp; the font is tiles 0-95 = characters 32-127, one ink colour), map offset
  (`0x2100`: 64 x 28 words, a plane-B layout: priority, palette, flips, tile index), then more
  tiles to the end (the background art; map indices count across both tile blocks, roughly).
  Rest of the header (`0x38`...) looks like layout. The font is a candidate for the Genesis
  app (the box would patch it in from the user's own card, as on the NES); the art is the
  user's theme and stays out of our app, which has its own look (decided 2026-11-01).
- **App ideas (owner, 2026-11-01):** the Mega menu has a scrolling background; it could be
  the Genesis app's look. Its font has to be worked out like the N8's (`MEGA/megaos.dat`?).
  The Genesis app keeps its own font for now (`apps.py`).

### EverDrive-64 X7

Supported through our N64 app, because the menu has no USB file access: `n64-plan.md`,
`x7-protocol.md`, `x7.py` (`sfe --x7`). USB is an FT245R serial port, `0403:6001`. System
folder `ED64`.

### FXPAK PRO

Supported through the sd2snes firmware's USB file service: `snes-plan.md`, `fxpak.py` (`sfe
--fxpak`). System folder `sd2snes`. Our SNES app talks to the box through cart memory
(`fxapp.py`). Its hardware rules are in `CLAUDE.md`.

### EverDrive-64 PRO (planned: not owned yet)

- Edlink ships `DEV_ED64` adapters (including `MenuCmd`/`MenuImage`). Its help text
  shows an ED64-specific example (`dscmd --cmd cicupd`), so the PRO model is supported
  by the shared tool (*source*). No `ed64-pro-pub` repo was found as of 2026-10-30.
- The N64's CPU is big-endian like the Genesis, so expect the Mega's byte-order rule
  (*inferred*).
- **Before buying:** look for photos showing where the USB port is and whether it
  stays reachable with the cart inserted. The N64 is top-loading, so the cart's top
  edge should be exposed.
- N64 saves (EEPROM, SRAM, FlashRAM, Controller Pak) vary per game. Find out when the
  cart writes each type to the SD card.

### Turbo EverDrive PRO

- Edlink ships `DEV_TED` (`DeviceCmd`, `DeviceIO`, `MenuCmd`, …), so it's in the same
  family (*source*). No public dev repo found.
- The PC Engine's CPU (HuC6280, a 6502 variant) is little-endian, so expect N8-like
  byte order (*inferred*).
- The micro-USB at the top is confirmed by the owner. The HuCard slot is front-loading,
  so check that the cable can be routed with the cart inserted.

## Out of scope for now

| Cart | Why |
|---|---|
| CORE-series carts | edlink supports them, but they are not owned or planned. |

## Adding a cart

1. Find its sources (Krikzz `*-pub` repos, community hosts) and record the commit and license here.
2. Run read-only `sfe info --raw` to record the protocol ID, device ID, firmware and
   USB VID/PID. Note which byte order makes the STATUS reply parse.
3. Add a cart profile (byte order, IDs, quirks) and the matching fake-cart variant, and
   run the shared test suite against both.
4. Run `bench-test.md` on the cart, including where and when saves are written to the SD card.
5. Update the summary table.

## Firmware files (.efu), read 2026-11-02

Krikzz's `.efu` (the menu installs it) is a plain archive, little-endian. Header 0x40 bytes:
`00 00 <device id> ED` (0x17 N8 PRO, 0x18 Mega PRO), then the version's FAT date (v25.1123:
`0x5B77` = 2025-11-23) and time. Entries from 0x40: u8 kind (1 folder, 0 file), u16 name length,
u8, u16 date, u16 time, u32 size, u32 CRC32, the name, then a file's data. The N8 one holds
`EDN8/` (nesos.nes, MAPS/*.RBF, syscore/, sysdata/readme.txt); the Mega one `MEGA/` (megaos.dat,
syscore/, edapp/, themes/, mappers/). Neither holds a save or the recent list.
