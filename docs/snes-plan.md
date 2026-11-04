# FXPAK PRO (SNES) support: plan (2026-11-03)

Status (2026-11-04): built and working on the owner's FXPAK PRO (firmware 1.11.2): phases 1-5 are
done, marked below; `.gb`/`.gbc` for the Super Game Boy aren't. Facts below come from the cart's
firmware source (`mrehkopf/sd2snes`, GPL-2.0, v1.11.2 at commit b081ff9) and the QUsb2snes client (`Skarsnik/QUsb2snes`, GPL-3.0). Both are read for
facts only; no code is copied. Clones: `~/projects/everdrive-refs/`.

## How the FXPAK differs from the Krikzz carts

| | N8 / Mega PRO | X7 | FXPAK PRO |
|---|---|---|---|
| Box reads/writes the SD card | directly (`F_*`) | only through our N64 app | directly (`USBA` commands) |
| Who serves the commands | the cart's MCU, in step with the menu | our app | the cart's MCU, in its menu loop **and** its game loop |
| Safe while the menu runs | stat only (the menu owns the card) | no (passive) | yes: the MCU does all SD access itself, one request at a time |
| Saves | `<sys>/gamedata/<ROM>/` | `ED64/gamedata/<ROM>.<type>` | `sd2snes/saves/<ROM base>.srm` (flat, like the X7) |
| On-screen app needed for sync | yes (it shows the plan) | yes (it does the copying) | no for the copying (`sfe --fxpak sync`); `sfe serve` syncs through the on-screen app |

The big difference: no menu on the console is competing for the SD card. The SNES menu asks
the MCU for files the same way USB does, so the box can read at any time, even mid-game. Writes
are another matter while a ROM runs: they must not meet the cart's autosave (below).

## Facts

**USB device.** CDC ACM serial port (`/dev/ttyACM*` on Linux, `/dev/cu.usbmodem*` on macOS),
VID:PID `1209:5A22`. **Every FXPAK reports the same serial number, `DEMO00000000`**, so the
box can't tell two FXPAKs apart by serial (the manifest is keyed per cart). With one FXPAK
that's fine; see open questions.

**Framing.** Everything is 512-byte blocks.
- Request: `USBA`, opcode u8, space u8, flags u8, list flags u8 (LS only), zeros; size u32BE at
  252; path (NUL-terminated, at most 255) or address u32BE at 256.
- Response: `USBA`, `0x0F`, error u8 (nonzero = failed; FatFs results OR'd together, so not a
  reliable code), size u32BE at 252, then the data blocks.
- PUT: the response comes first, then the box sends `size` bytes padded to 512-byte blocks. The
  file is opened `FA_CREATE_ALWAYS`.
- GET (file): the response's size is the file's; the data follows in 512-byte blocks. A failed
  open still enters the data phase, so the box must stat first (LS) rather than GET blindly.

**Opcodes.** 0 GET, 1 PUT, 2 VGET, 3 VPUT, 4 LS, 5 MKDIR, 6 RM, 7 MV, 8 RESET, 9 BOOT,
10 POWER_CYCLE, 11 INFO, 12 MENU_RESET, 13 STREAM, 14 TIME. **Spaces:** 0 FILE, 1 SNES
(cart memory: ROM, SRAM at `0xE00000`, a WRAM snapshot at `0xF50000`), 2 MSU, 3 CMD, 4 CONFIG.
- LS: entries of type (0 folder, 1 file, `0xFF` end, 2 continue in next block) and a
  NUL-terminated name. With list flags (byte 7: 1 size, 2 date, 4 time, 8 attribute) newer
  firmware adds those fields. Older firmware ignores byte 7, and so does ours (below).
- RM is `f_unlink`: a file or an empty folder. MV renames within a folder: the path at 256, the
  new name at byte 8 (`FxPak.move`; not yet tried on the cart, so syncs don't use it yet).
- INFO: firmware version string at 260, device name at 324 ("FXPAK PRO STM32"), the running
  ROM's path at 16, feature flags at 6.
- BOOT loads and starts a ROM from the card. RESET and MENU_RESET reset the console.
  POWER_CYCLE reboots the MCU. **The box never sends RESET or POWER_CYCLE.** It sends
  MENU_RESET (the app's B) and BOOT (restarting an updated app) only, on the owner's say-so
  (2026-11-03).
- Names on the wire are Windows-1252, not UTF-8: the firmware's FatFs is built with code page
  1252 and no Unicode (`src/ffconf.h`). A UTF-8 path for a name with "é" gets `FR_NO_FILE`; a name
  outside 1252 can't be stored, so sync skips it (`fxpak.name_ok`, `Plan.skipped`).

**Saves and other files on the card** (`/sd2snes` is the system folder):
- `sd2snes/saves/<ROM name, extension replaced>.srm`: battery saves. The firmware writes it when
  SRAM has stopped changing for a few checks (and periodically if it keeps changing), so the
  file trails the game by a few seconds. An older firmware kept `.srm` next to the ROM and moves
  it here on first load.
- `sd2snes/saves/<ROM>.gtc`: the Game Boy clock for Super Game Boy games.
- `sd2snes/states/<ROM>NN.state`: save states (slot NN), if save states are on.
- `sd2snes/cheats/`: cheat files. `sd2snes/config.yml`, `lastgame.cfg`, `favorites.cfg`.
- `sd2snes/firmware.stm` plus the `fpga_*` files: the firmware. The bootloader flashes a
  new `firmware.stm` when the cart starts. This is how a firmware update works: copy a release's
  `sd2snes` folder over the card's.

## Measured on the owner's cart (2026-11-03)

FXPAK PRO STM32, firmware **1.11.2**; INFO at the menu reports the running ROM as
`/sd2snes/m3nu.bin`.

- **LS returns no sizes on 1.11.2.** List flags (byte 7) are ignored: the reply has the plain
  layout, so a parser that expects the extra fields reads garbage. Names without a long
  filename come back lowercased (`GB` → `gb`).
- **A file's size comes from GET's response**, and a GET must then be read to its end.
  Cutting one short (dropping DTR) seems fine (the next command is answered) but **the
  firmware keeps its read position**: the next GET starts that far into its file and sends
  that much less (measured: a 4 MB file came back from byte 512, one block short). When the
  position is past the next file's end the MCU sends nothing, stays busy, and stops taking
  USB data. `fxpak.py` never cuts a GET short.
- **GET of a missing file hangs the MCU the same way** (its response repeats the previous
  file's size). **Never GET a path without first seeing it in an LS of its folder.**
- **GET of an empty file** sends no data block and leaves the MCU busy; dropping DTR idles it
  (safe: nothing was sent, so no read position is left).
- **Speeds:** reads ~700 KiB/s, writes ~300 KiB/s (a 4 MB ROM: 13 s up, 6 s back).
- **Save backups work at the menu and mid-game** (2026-11-03): 13 files in 3 s, identical to
  the SD backup; reads during Super Mario World went unnoticed by the player; a save made in
  A Link to the Past Redux was on the card (and backed up) 12 s after the return to the menu.
- **Never send TIME (opcode 14) to 1.11.2:** its handler lacks a `break` and falls through into
  MV (rename) with leftover arguments (fixed after the release).
- **Uploads stalled now and then until they were paced** (below). Two of about seven 4 MB PUTs stopped mid-file (after
  ~200 KiB once, ~3840 KiB once): the cart stopped taking USB data, the host's write timed out
  after 10 s, and the cart's USB then stayed dead. DTR, a USB replug and filler bytes didn't
  bring it back; a power cycle did. The SNES menu kept working (folders still opened), so the
  MCU was running. 1 MB uploads and every read so far were fine. No public reports of it,
  but QUsb2snes's docs say "SD2Snes devices may have issue on mac os X", and on macOS only it
  sleeps 10 ms before every write to the cart. **Pacing like that didn't help**: 10 ms per
  KiB (80 KiB/s) stalled on the second of eight 4 MB uploads (2026-11-03). In the firmware the
  USB interrupt reads each OUT packet and hands it on only if the server isn't busy (else it's
  dropped), and PUT data is written to the SD card inside that interrupt; a lost packet or an
  OUT endpoint left un-armed would leave the PUT waiting forever.
- **The stall, characterised (2026-11-03, macOS and the Pi):**
  - It happens on Linux too: the Pi stalled on its first 4 MB upload.
  - Density makes it worse. Sending the PUT request and its data back to back without waiting for
    the response (QUsb2snes's way) stalled 2 of 2. Waiting for the response, then writing the
    data in 64 KiB writes, stalled 2 of 2. The old code (one `write` per 512-byte block) stalled
    less often: 2 of 7 at first, then 1 of 13 on the Mac and 1 of 1 on the Pi.
  - It is a real hang, not a slow SD operation: with a 5-minute write timeout, it never resumed.
  - The MCU is still alive: the menu starts a game from the card. Only USB is dead.
  - The SNES reset button doesn't bring USB back; after it, a 512-byte write is accepted (into
    the OS's buffer) but nothing is answered. Only a power cycle helps.
  - Likely cause (from the source; not proven): the STM32 driver re-arms the OUT endpoint after
    each 64-byte packet by setting `EPENA|CNAK` *before* writing `DOEPTSIZ`
    (`stm32f4xx/usbhw.c`, the RXFLVL `STS_XFER_COMP` branch). A packet that arrives in that window
    can leave the endpoint shut.
- **With a ROM running, the autosave is a second, bigger cause** (2026-11-03, the Pi): 20 of
  20 4 MB uploads clean at the menu, then a stall on the 12th with our app up, and a sync with
  the app up stalled after ~30 MB. The firmware's main loop takes a CRC32 of save RAM every
  ~260 ms and writes it to the .srm once it has changed and then stayed the same for 5 passes
  (`snes.c` snes_main_loop, `memory.c` save_sram); PUT data is written inside the USB interrupt
  (`usbinterface.c` usbint_recv_block), so the two meet on the SD card. Our app's ring is in save
  RAM, so every message sets off a save ~1.3–3 s later. `FxApp._quiet` gates every PUT while
  the app runs: within 1 s of a change the box has just seen, or after 3 s of stillness. So the
  app writes no save RAM on its own during a sync (no timeout, no HELLO; STOP only just after a
  STATUS, when the box is listening). During a game the box doesn't write at all: it installs the
  app only at the menu.
- **Fix: pace the blocks.** After each 512-byte data block, wait until the OS has sent it
  (`flush`), then 1 ms (`FxPak.block_gap`). That ran 40 of 40 clean 4 MB uploads, each read
  back and compared (~13.3 s each, against ~12.4 s unpaced), and `sfe --fxpak bench` still
  uploads at ~315 KiB/s. `sfe --fxpak bench --gap MS` changes the gap; 0
  sends back to back.
- **Uploads can start slowly** (2026-11-04). FatFs looks for free space from the last file it
  overwrote, inside the USB interrupt. With the app's `.srm` (rewritten by every autosave) behind
  46 GB a computer copied on, each new file took ~11 s to start, more than the old 10 s write
  timeout, and giving up left things worse. Writes now wait up to 120 s (`fxpak.WRITE_TIMEOUT`),
  and `FxApp.after_first_copy` deletes the `.srm` once per sync, after its first copy, so the cart
  recreates it at the free space. It holds only the app's rings.

## Plan

**Phase 1: talk to it (box only). Done** (`fxsim.py` fakes the cart for tests and `sfe --fxpak
--sim DIR`). A `fxpak.py` device with the same file interface as the
others (stat via LS, list, read, write, delete, mkdir), plus `info`. `sfe --fxpak info / ls /
get / put / rm` against the real cart, at the menu and with a game running. Measure speed.
Read the firmware version and check whether LS returns sizes. Back up the card first.

**Phase 2: saves and sync without an app. Done.**
- The cart profile: key `snes`, system folder `sd2snes`, flat saves, ROM types
  `.sfc .smc .swc .fig .bs` (`.gb .gbc` for the Super Game Boy files: not done).
- Backup on connect, 10 s after the running ROM changes, and every 10 minutes
  (`box.fxpak_session`): every file in `sd2snes/saves`, `states` and `cheats` (`.state` files go
  where the `states` setting says: by default with the saves).
- Sync from the CLI (`sfe --fxpak sync`) using the existing planner. This works, but there's no
  confirmation on screen. **Done (2026-11-03):** it prints the plan, asks (or `--yes`), and
  shares the box's settings (`[snes]`) and records (`cart-<serial>.json` under `[box] state`).
  LS has no sizes, so the planner trusts the manifest's size and hash for every cart ROM it
  knows, and reads an unknown one once (size and hash in one GET). A ROM changed on the card
  behind sync's back (an SD reader) therefore isn't noticed until the manifest forgets it.
  On the cart: 6 ROMs (10 MB) added in 47 s, read back and checked; a re-plan read nothing;
  replace and mirror removal worked.

**Phase 3: the on-screen app. Done.** A SNES ROM with the same screens as the NES app (plan, mode,
Sync, copying, Sync done). It talks to the box through cart memory: the box writes requests
into a mailbox the app polls, and reads the app's replies from SRAM. The box does all the file
work directly. Toolchain: cc65 (below).

**Phase 3, measured (2026-11-03): the channel works.** `snes/poc/` (ca65, LoROM 32 KiB with 2 KiB
SRAM, the edio font) shows its inbox and a frame counter. With it running (INFO's ROM is it):
- `FxPak.read_mem(SRAM_BASE, 16)` (GET, SNES space) reads its SRAM live: 2-3 ms each.
- `FxPak.write_mem(0x7000, ...)` (PUT, SNES space) writes its ROM's inbox (`$00:F000`, file
  offset 0x7000) in 5 ms; the ROM saw it on the next frame and echoed it into SRAM.
- **Only write SNES space while INFO says our ROM is running**: at the menu, address 0 is the
  menu's own ROM.
- The menu leaves PPU state behind (rows below the ones we wrote showed garbage): the app must
  reset every PPU register, HDMA included, at start. Keep text 2 tiles in from every edge for
  overscan (owner's request).

**Phase 3, built (2026-11-03): `snes/`** (since 2026-11-06 it shares `app/` with the NES app). The NES app's `main.c` with a SNES screen layer
(`screen.c`: a WRAM copy of the tilemap sent by DMA each vblank, chr.py's font and art as 2bpp
tiles), cc65's 6502 code running with the 65816 in emulation mode (`crt0.s` resets the PPU,
HDMA included, first), and `link.c` on two rings: box -> app 256 bytes at `$00:F000` (head
`$F100`, the box's read position of the app's ring `$F101`); app -> box in SRAM ("SFE1", the
app's read position, its head, a start counter, then 256 bytes). The box side is `fxapp.py`
(app_read/fifo_write for the unchanged AppServer); `box.fxpak_session` installs the app at
connect and serves it while INFO says it runs. On the console: plan, sync, re-plan all work.
- The app's SRAM is saved by the firmware as `sd2snes/saves/Sync Forever.srm`; backups skip
  any .srm starting "SFE1". The box deletes it once per sync (`FxApp.after_first_copy`, above).
- **The sync screen** (`app/clock.c`): while a sync runs the app shows the mode, the file's
  name over 3 rows, "This file: 0:30 / 2:28" ("(slow)" past twice that) with its bar, "Time left:
  about 42 min" and a bar for the whole sync. The box can't speak during an upload, so the app
  counts from each STATUS's `expect` and `left`. B shows "Stopping after this file..." and sends
  STOP when the next STATUS arrives, between files.
- **B exits through the box**: EXIT -> `FxPak.menu_reset` (MENU_RESET, answered at once,
  menu up within 1 s). **An updated app is restarted by the box**: RESTART, then
  `FxPak.boot` (BOOT, ~0.9 s for 32 KiB; the reloaded ROM's inbox is zeros, so `FxApp`
  rereads its side). Both tested by hand first with the owner watching. If the box doesn't
  act within 3 s the app asks for RESET; the reset button returns to the menu and USB stays
  up (2026-11-03).
- Colours: the NES palette converted as is looked washed out on the owner's RGB-modded SNES:
  mid-tones darkened (gamma 1.5), the backdrop set by hand. The title's stripes got their own
  tile with 2-pixel gaps and dimmer thin lines (a CRT bled the NES tile's 1-pixel gaps).
- Once, after resetting out of the channel test ROM (which rewrote SRAM every frame), USB
  stopped answering until a power cycle; not seen with the app. Unexplained.

**Phase 4: app install and launch. Done.** The box writes `/Sync Forever.sfc` directly, the same
way as on the PRO carts: at the menu, or at the app's HELLO through `FxApp`'s gate. You launch it
from the menu.

**Phase 5: firmware, reported only (owner's call, 2026-11-03). Done.** An sd2snes release is a whole
`sd2snes` folder (controller firmware per hardware, an FPGA core per enhancement chip, the menu,
support files) that must match, installed by copying it over the card's; the bootloader then
flashes `firmware.stm` by itself at the next power-on. Like the X7's OS, the box never installs
it: the app's firmware line shows INFO's version, green when it's the newest sd2snes version,
red when there's newer. The repo has no GitHub releases; its firmware versions are the
three-number tags (`v1.11.2`), read from GitHub's tag list once a day (`firmware.FEEDS`,
`CartSession.sd2snes_line`). Newest on 2026-11-03: v1.11.2 (2026-05-19), which the owner runs.
If the newest isn't known, the app shows the version in red too (it has only the two colours).

**MSU-1. Done.** The `msu` setting under `[snes]`, off by default (`config.py`, `msu.py`). Off, MSU
games are left out of the sync and their copies on the cart left alone. On, a game's `.msu` and
`-N.pcm` files sync before its ROM and aren't read back; a copy on the cart the manifest doesn't
know is taken as the library's. PLAN carries the setting; the app shows "MSU on" or "MSU off".

## Open questions

- Is reading `sd2snes/saves` while a game runs ever unsafe (SRAM writeback mid-read)? A save
  read could compare two reads, or wait for the file's size and date to settle.
- MSU-1 games stream audio from the SD card: does a long USB copy make the audio stutter?
- Two FXPAKs on one box (they share a serial): key by USB port, or a small ID file on the card.
