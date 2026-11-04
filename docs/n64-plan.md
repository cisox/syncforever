# EverDrive-64 X7 support: plan (2026-11-02)

Status (2026-11-04): built and working on the owner's X7. Phases 1-4 and 6 are done (results
below); phase 5, an emulator harness, isn't built (`x7sim.py`, a protocol-level fake, does the
testing). Also not built: syncing cheats from the library. The app's B-to-stop, time left and whole-sync bar, "Library changed" and the plan number on SYNC_RUN were added 2026-11-04; built, not yet run on the X7.
The Design section is the plan as corrected by what got built.

The owner has an X7 (micro-USB) on OS V3.11 (the newest on
Krikzz's site, 2026-11-02); its card is backed up at `~/Desktop/N64Bak` (before that update). Facts below come from Krikzz's reference source (the `srmeier/ed64` mirror of
krikzz/ED64, GPL-3.0: read for facts only, no code copied), UNFLoader (`buu342/N64-UNFLoader`,
WTFPL), libdragon's docs, and the card backup.

## How the X7 differs from the PRO carts

The PRO carts give the host file access over USB; the X7 doesn't. Its menu answers a handful of
memory commands, so **everything on the SD card goes through our N64 app**, and the box feeds it.

| | PRO carts (N8, Mega) | X7 |
|---|---|---|
| Box reads/writes the SD card | directly over USB (`F_*` commands) | only through our app |
| Save backup | after a game starts, and when the app opens | when the app opens |
| ROM sync | the box copies files | the box streams ROMs to the app, which writes them |
| App install/update | the box writes the app file | the box writes the app file through the app (`sfe --x7 install` boots it over USB first) |
| Firmware | the box delivers the `.efu` | manual (zip contents to the card root, on a computer); the app shows a status line |

## Facts

**USB device.** FTDI FT245R, description "FT245R USB FIFO", `0403:6001`. macOS and Linux present it
as a serial port (`/dev/cu.usbserial-*`, `/dev/ttyUSB*`), so pyserial and `transport.py` carry over.
UNFLoader supports the X7 from OS 3.04.

**Menu protocol (host → cart).** 16-byte packets: `c m d <letter>`, then big-endian u32 address,
u32 length in 512-byte blocks, u32 argument.

| Letter | Does |
|---|---|
| `t` | test; the menu answers a 16-byte packet starting `cmdr` (old OSes: `cmdk`) |
| `W` / `R` | write / read cart ROM memory (`0x10000000` = the ROM's start) |
| `r` | read cart RAM |
| `c` | fill ROM space (used before an upload for the CRC area) |
| `s` | start the loaded ROM |
| `f` | FPGA write |

On the owner's X7 (OS V3.11, 2026-11-02) the menu's reply to `t` is
`cmdr 00000000 ffffffff 80090000` (the last 12 bytes' meaning unknown). While a game (or an
emulator) runs, nothing answers, and **the X7 has no in-game reset**: getting back to the menu
takes a power cycle (the owner may add a hardware mod later). So the box can't tell the user to
"go to the menu", and a session starts only when someone opens our app.

Nothing lists, reads or writes files. Krikzz's notes: no way to read the OS version over USB or from
a ROM, nor the hardware revision over USB at the menu.

**From a running ROM.** Registers at `0x1F800000`: write `0xAA55` to `REG_KEY` (`+0x8004`) first;
`REG_VERSION` (`+0x14`) reads `0xED640013` on an X7; `USBCFG` (`+0x04`) with status bits for
read-ready, write-ready, USB powered, busy; a 512-byte buffer at `USBDAT` (`+0x400`). Reads are
≥16 bytes, ≤512 per chunk; DMA targets 8-byte aligned. UNFLoader's ROM library (`usb.c`, in
libdragon) wraps this. Its host framing: `DMA@`, type u8, size u24, data, `CMPH`.

**SD card from a ROM.** libdragon mounts the X7's card: `debug_init_sdfs("sd:/", -1)`, then plain
`fopen("sd:/...")` (FatFs underneath: FAT16, FAT32 or exFAT). Measured ~1.0 MB/s (phase 1).

**Card layout** (the owner's backup):
- ROMs at the root (`*.z64`), 8 to 64 MB each.
- `ED64/OS64.v64`: the menu (512 KB). `ED64/emu/*.v64` (NES, GB, GBC emulators), `ED64/save_db.txt`,
  `ED64/syscore/`, `ED64/bgr/`.
- **Saves:** `ED64/gamedata/<ROM name>.<type>`, flat: `.eep` (EEPROM, 512 B / 2 KB), `.srm` (SRAM,
  32 KB), `.fla` (FlashRAM, 128 KB); cheats and Controller Pak dumps live there too (below).
- **Recent list:** `ED64/sysdata/recent.dat`, 1025-byte slots, newest first, each a NUL-padded path
  (`/Paper Mario (USA).z64`). The same idea as the PRO carts' `recent.bin`.
- Firmware updates: a zip from Krikzz whose contents are copied to the card root.
- **Emulators:** `ED64/emu/<extension>.v64` runs ROMs with that extension (OS readme; the OS
  changelist: "Emulators support for any file extensions"). OS V3.07 added an FPGA NES core and
  the GB/GBC emulators. The owner's card (OS V3.11, 2026-11-02) has `gb`, `gbc`, `gg`, `nes`, `sg`,
  `smc`, `sms`: Game Boy/Color, Game Gear, NES, SG-1000, SNES (`.smc`), Master System. Other
  add-on emulators (ScummVM?) to investigate.
- **Patches:** the menu applies `.ips`/`.aps` files in a ROM's folder whose name matches the ROM's,
  in cart RAM (the ROM file stays unchanged).

## Design

**The app does the file work.** An app in `n64/` (C, libdragon), with the same screen as the NES
and Genesis apps (plan, mode, Saves line, A to sync; "Power off to exit" instead of B), speaking the
same app protocol over UNFLoader's framing. Beside v7 it has a file service the PRO apps never
needed, because there the box reads the card itself: `x7-protocol.md` (its own frame type, status
bytes as FatFs `FRESULT` codes).

On the box, an `X7App` object offers the methods `romsync` and `saves` already call on an
`EverDrive` (`list_dir`, `stat`, `open`/`read`/`write`, `delete`, `mkdir`), carried by those
messages. Sync plans, Mirror's manifest and the versioned save store then work unchanged.

**Starting the app.** At the menu `sfe --x7` can boot the app straight from USB (`W` the ROM to
`0x10000000`, then `s`). `sfe --x7 install` writes `/Sync Forever.z64` through it so it shows in the
menu; afterwards the user starts it from the menu like the PRO apps. Updates: the box replaces the
file when the app connects, and the new copy runs from the next start (phase 4). `sfe serve` never
boots the app or talks to the menu: whether pinging it with `t` while someone uses it disturbs it
(as pinging the PRO menus does) stays open.

**What syncs.** N64 ROMs (`.z64`, `.v64`, `.n64`), plus every extension the card has an emulator
for: the box lists `ED64/emu/` when the app connects (`X7App.scan_emulators`), so the set follows
what's installed (no fixed list as for the PRO carts' `rom_suffixes`). Plus `.ips`/`.aps` patches,
as on the Mega.

**SNES: `.sfc` needs no mapping.** The emulator is Sodium64, installed as `ED64/emu/smc.v64`;
its docs say "SNES ROMs must be in headerless .smc format to work this way". A copy of it named
`ED64/emu/sfc.v64` runs `.sfc` files as they are (tested 2026-11-02 on the owner's X7, OS V3.11:
the game started; Sodium64's accuracy is its own matter). So `.sfc` is an ordinary scanned
extension once an owner adds `sfc.v64` (the setup notes don't say so yet), and sync never renames
or strips headers. Saves of emulated games live in `ED64/gamedata/` too: to confirm their names.

**Saves.** When the app opens: back up `ED64/gamedata/` like the PRO apps' full backup (per file,
versioned, never overwritten). No backups while a game runs (the box can't read the card then);
the app's opening backup covers what changed.

**Copying large ROMs.** N64 ROMs are 8-64 MB, against tens or hundreds of KB on the NES, so a sync
has a copy panel: the file name, `Copying 1 of 2`, the size done (`2.3 / 5.7 MB`) and a bar for the
file, updated a few times a second from COPY frames (phase 3). No overall bar. Speeds: phase 1.

**Firmware.** Not delivered by the box (a zip's contents on the card root is a computer job). The
box reads the OS version from `ED64/OS64.v64` and compares it with Krikzz's OS index; the app shows
the usual green/red line (phase 6).

## Phases

1. **Toolchain and proof of concept (done).** libdragon's Docker toolchain (Docker is installed). A ROM
   that mounts the card, lists `ED64/gamedata/`, and sends the listing over USB. The box boots it
   (`W` + `s`) and prints the listing. Proves the build, the USB boot path, and both directions of
   the link on the owner's X7. Measure USB and SD write speed.
2. **File messages (done)** in the app and `X7App` on the box; `sfe --x7 ls/get/put` against the
   running app; the save backup.
3. **The app screen (done)**: the shared plan view, sync with the copy panel, the Saves line.
4. **Install and update (done, differently)**: the box writes the app through its file service; no
   relaunch, the new copy runs from the next start.
5. **Tests (emulator harness not built)**: an emulator harness like `nesemu`/`mdemu` (MIPS, the X7
   registers, a fake SD) is possible but sizeable. A host-side fake of the app (protocol-level,
   `x7sim.py`) does the testing for now.
6. **The firmware status line (done).**

## Phase 1 results (2026-11-02)

Toolchain: `ghcr.io/dragonminded/libdragon` no longer allows anonymous pulls; libdragon moved to
`n64brew` and `ghcr.io/n64brew/libdragon:preview` (amd64 only, so `--platform linux/amd64` on Apple
Silicon) has the compiler (GCC 16.2) but not libdragon. libdragon (public domain) is built from a
clone into a local image, `sfe-libdragon:preview`. The test ROM and its host script are `n64/poc/`
(`x7boot.py PORT ROM boot frames tests`).

Measured on the owner's X7 (OS V3.11):

| | |
|---|---|
| USB boot (`t`, `c`, `W`, `s`) | 196 KB in 0.47 s, starts at once |
| cart → box | 547-590 KB/s (128 KB frames) |
| box → cart | 452 KB/s (1 MB, checksum matches) |
| SD read | ~1.0 MB/s (8 MB in 8.2 s) |
| SD write | ~1.0 MB/s (4 MB in 3.8-4.3 s) |

So box → cart USB is the limit for ROM sync: written one after the other, about 300 KB/s, i.e. a
32 MB ROM in ~1¾ min and 64 MB in ~3½ min (overlapping USB and SD would approach 450 KB/s). The
copy panel with MB done is needed from V1; the progress bar is cheap to add on top.

**The freeze, and its fix.** With libdragon's `usb_write`, streaming cart → box froze the N64 (no
timeout fires: the CPU waits on the PI bus) after a few blocks sent back to back. Krikzz's menu
(disassembled, facts only) accesses the X7's registers *only by 4-byte PI DMA*, never by CPU
loads/stores as libdragon's `io_read`/`io_write` do. Our sender doing the same streams cleanly at
every frame size. Our sender also pads each frame to whole 512-byte blocks (harmless: the box skips
to the next `DMA@`). libdragon's receive path (`usb_poll`/`usb_read`, CPU reads) worked for 1 MB.

**A lost reply (2026-11-05).** While verifying the owner's card, a READ's reply stopped at 64,088
of 65,539 bytes: `X7 frame cut short`. The cause isn't proven. A pause on the USB side outlasting the
sender's 100 ms per block fits (each block given up loses its bytes); so does the app stopping. A
later log settled it: the app answered the box's next request straight away, so it hadn't stopped.
Now a block of a file reply waits 2 s (an app message 100 ms: nobody may be reading), and a frame
is dropped at the first block given up. The box gives up on a frame after 5 s of silence, and
goes back to the READ's start with SEEK and asks again, twice (`X7App._read_chunk`). The retry has run on the X7 ("asking again", then
verifying carried on); the 2 s wait for a reply's first block hasn't yet.

**The owner's card is laid out by system** (the owner's choice, after adding ROMs for the
emulators; sync must follow whatever folders the owner uses): the root holds `ED64/` and one folder per extension (`N64`,
`GB`, `GBC`, `GG`, `NES`, `SFC`, `SG`, `SMC`, `SMS`), 96 ROMs in `N64/`. Listing `sd:/` works
(`d_type` 1 file, 2 folder); `sd:` (no slash) doesn't.

## Phase 2 results (2026-11-02)

The app (`n64/`, `make` builds in Docker, `make package` copies it to the box's data) serves the
card over the protocol in `x7-protocol.md`: its own USB link (registers by DMA both ways), FatFs
for the files. The box's `X7App` (`x7.py`) has `N8Pro`'s file methods, so `ops` and `saves` run
on it unchanged; `x7sim.py` fakes the menu and the app for tests. `sfe --x7` boots the app from
the menu (or uses it if it's running) and then `ls`, `stat`, `get`, `put`, `mkdir`, `rm`, `backup`
and `info` work as on the PRO carts.

On the owner's X7: `ls` of the root and `ED64/gamedata` correct; `backup` saved the three saves,
byte-identical to the card backup; a 3 MB `put` at 443 KiB/s, readback verified at 459 KiB/s;
`rm` of the file and folder. Saves are stored as `<ROM name>/<ROM name>.<type>/` versions
(`saves.backup_flat`).

Not yet at that point: the box's `serve` for the X7 (phase 3, with the screen), ROM sync (`romsync`
on `X7App`; the emulator-extension scan), installing the app to the card (phase 4). All done since.

## Phase 3 results (2026-11-02)

`sfe serve` finds X7 ports (FTDI `0403:6001`, "FT245R USB FIFO") next to the PRO carts and
**only listens** on them: nothing goes to the cart until our app says hello (pinging the menu
while it's in use stays untested and unneeded). The app protocol (`app-protocol.md`, v7, the PRO
apps' own) travels in UNFLoader frames of type `0x55` beside the file service (`0x53`); the app
answers file requests at all times, also while it waits for the box's reply, since the box plans
and syncs through them. `AppServer` runs unchanged on `X7App` (`app_read`/`fifo_write`), plus:
`on_connect` (the X7 scans `ED64/emu/*.v64` and syncs those extensions, `.ips`/`.aps` and N64
ROMs) and COPY (`n:u16 total:u16 stage:u8 done:u32 size:u32 name:str`, little-endian, at most 4 a
second; stage 0 copying, 1 checking the readback), which only the X7 app gets. The cart's USB
serial number names its records (`cart-AQ03BVGM.json`), made file-name-safe (`box.safe_id`).

The screen (libdragon 2D, 320x240, 8x8 font): the Genesis app's layout and states, the title at
1.5x, everything inside columns 3-36 and rows 2-25 (the owner's TV crops the edges), and the copy
panel: `Copying 1 of 2`, the file name, `2.3 / 5.7 MB`, a bar (blue copying, green checking).
"Power off to exit": the X7 has no way back to the menu from a ROM.

On the owner's X7: connected, scanned 8 emulators, backed up the 3 saves, planned in every mode,
synced a 6 MB `.z64` and a 32 KB `.gb` into `/syncforever-test` in 25 s with readback (the copy
matched the library's SHA-256). The owner: "That progress bar is really slick."

Next at that point: phase 4, a config example with `[n64]`, the deploy notes. All done since
(`deploy/config.example.toml`, `deploy/README.md`).

## Phase 4 results (2026-11-02)

`sfe install` (any cart; `--x7` boots the app from the menu first) writes `/Sync Forever.z64`
through the app's file service; run again it reports `current`. Under `serve` the box checks the
file whenever the app connects (`apps.ensure_app`, as on the PRO carts) and replaces it if it
differs. The X7 can't relaunch: the box can't start a ROM from its menu (only boot one over USB,
which needs the menu and so a power cycle), and a ROM can't get back to the menu. So the running
copy carries on ("Updated for next start", logged) and the new file runs next time.

On the owner's X7: installed, then started from the menu after a power cycle; the box (only
listening) connected to it by itself: emulator scan, save check, plan.

## Phase 6 results (2026-11-02)

The firmware line: the X7's OS version is compiled into `ED64/OS64.v64`, in BCD (`li a0,0x0311`
for V3.11, just before the menu stores it in its system info; disassembled for facts). The box
reads the file through our app when it connects (`CartSession.os_line`, `firmware.os_version`)
and compares it with the newest `OS-V*.zip` in Krikzz's OS index
(`krikzz.com/pub/support/everdrive-64/x-series/OS/`, checked at most daily, `[box] firmware`).
PLAN carries "v3.11" and green (newest), red (a newer one exists: install it from a computer) or
plain (index never fetched). No downloads: the update stays manual.

On the owner's X7: "Firmware: v3.11" in green. The first try showed v3.17 (BCD read as decimal).

Left for later: phase 5 (an emulator harness, if the protocol-level fake proves too little) and
the open questions below.

## Cheats and Controller Paks (2026-11-02)

From the menus' own strings and code (facts only): the X7 keeps cheats as
`ED64/gamedata/<ROM>.txt` and its CPak manager's dumps as `ED64/gamedata/<name>.mpk` (the last
played game, or `controller-1` to `-4`); the N8 and Mega keep `<system>/gamedata/<game>/cheats.txt`
beside the saves. Every backup that copies a whole game folder (all of `gamedata` on the X7)
already takes them; the PRO carts' quick after-start backup now knows `cheats.txt` by name (and
the Mega's `cd-cart.srm`, `cd-cart128.srm`, `config.txt`, missing until now). Syncing cheats from
the library: not done (`.txt` is too common a type to sync blindly).

Controller Paks: our app reads the pak in each controller (CPAK, libdragon's `cpak_read`) and the
box backs each one up when the app opens, as `Controller Pak/<id>.mpk` versions, id from the pak's
ID block, so paks never mix. Restoring one: copy the `.mpk` to the card and use the X7 menu's
"File to CPak". Not yet tried with a pak on hardware.

## Open questions

- Is `t` safe while someone uses the menu? (The PRO menus misread pings as their own replies.)
  Untested; `sfe serve` doesn't need it, since it only listens.
- The full list of add-on emulators that work (Game Gear, SNES, ScummVM?) and their save files.
- Does the X7 OS write a save to the card immediately (it advertises saving without a reset), so
  that opening the app after playing always finds the latest save?
