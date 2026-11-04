# Sync Forever: design

**Date:** 2026-10-30, status updated 2026-11-04 · **Status:** built and working on four carts
(see Roadmap). Dated entries are kept as history.

Each decision below is marked **(decided)** if it was settled in conversation, or
**(default)** if it is a sensible starting choice that hasn't been discussed yet.

## Intent

A small box next to the console that keeps an EverDrive's SD card in sync with your own storage
(a disk on the box, or a network share such as a NAS), so the card never has to come out:

- Saves are backed up automatically, with history kept.
- ROMs flow from your library to the cart.

It's **set up once on the box (`sudo sfe setup`) and then used from the couch with the
controller**. Everyday use never requires anything else. **(decided)**

### Scope **(decided 2026-11-05)**

The product is two promises: your saves are backed up, and your library stays current. Saves go one
way, from the cart to your save folder. Further work goes to more consoles (the Turbo EverDrive PRO next, then the
EverDrive-64 PRO), hardening against random errors, speed, polish (nobody should have to ask what's
happening: anything long-running shows progress, and the full-screen sync views break the work down),
and less complexity, with as little per-cart customization as possible.

### Goals

- Automatic, versioned save backup to your save folder. Done.
- ROM sync from your library, one-way. Done: nothing is deleted unless you run Mirror or Mirror strict.
- Works with the owner's carts through per-cart adapters: N8 PRO, Mega EverDrive PRO,
  EverDrive-64 X7 and FXPAK PRO (all working), then the EverDrive-64 PRO once we have one, and
  the Turbo EverDrive PRO (`cartridge-compatibility.md`).
- No permanent modification to cart or console.

### Anti-goals: failure even if it "works"

- Corrupting the SD card, losing a save, or overwriting a save backup without history.
- Writing to the cart while a game is running.
- Requiring anything but the controller for everyday use.

## Pieces

### 1. Sync Forever box **(decided)**

A standalone box beside the console, connected to the cart by a USB cable, which serves other consoles and carts.
**Hardware: a Raspberry Pi** (the owner's is a Pi 4; any Pi, or any Linux with systemd and
Python 3.11+), so the tested Python code runs on it unchanged and a network share can be mounted
natively. It runs as the `syncforever` systemd service (`sfe serve`), installed and updated
with `deploy/get.sh`.

- Powered from its own USB supply. No power taken from the console (see
  `safety.md`). **(default)**
- Several carts through a USB hub: built. The box serves them all at once, one thread per cart.

### 2. Settings: split by how often they change **(decided)**

| Where | What |
|---|---|
| **The box** (`sudo sfe setup`, `/etc/syncforever/config.toml`) | One-time setup: each console's `library`, `saves`, `states`, `dest`, `sync_mode` and `msu`, and the user it runs as. `sudo sfe doctor` checks them; logs are in `journalctl` |
| **TV** (Sync Forever app, controller) | Everyday: choose the sync mode, preview, sync now, status |

A network share, if you use one, is mounted on the Pi by hand.

### 3. The Sync Forever NES app **(decided)**

All four consoles have the app now (`nes/`, `md/`, `n64/`, `snes/`; the NES and SNES share `app/`), installed and updated on the
cart by the box (the X7's first copy with `sfe --x7 install`). The NES, SNES and Genesis apps have a
full-screen sync view with progress bars, time estimates and "B: stop after this file", also used for slow checks before a plan, and a sync's end says what it did. The box
plans again when the library changes while the app shows a plan.

The app is "Sync Forever.nes" and does sync, not browsing: the cart's menu picks and starts games.
The app opens on the sync preview (mode, add/replace/remove counts), runs it, and goes back to the
menu. Saves are backed up when the app opens (all of them) and a few seconds after the menu starts
any game (recent games' changed saves): both are moments the menu isn't running. At the menu the box
only stats `recent.bin` (pings and file opens upset a busy menu, `test-results.md` 2026-11-01). So a
save reaches your save folder when the next game or the app starts. Starting a file through the menu is
`sfe launch` (`make install` uses it) and `ops.launch` (the app's restart after an update). Look:
charcoal and red, header art, "Status:" line, rows 0-1 blank for overscan; A syncs, and a "Saves:"
line shows the backup made as the app opened.

**Look and feel: match the N8 menu (decided 2026-10-31).** Same font, colours, layout and list behaviour as the menu's file browser. Sources, no decompiling needed:
- Font and icons: `EDN8/nesos.nes` is iNES (8×16 KB PRG, 2×8 KB **CHR ROM**, mapper 255); render its 16 KB CHR to see every glyph.
- Layout and palette: edlink's `screen` = menu `*v` (2 KB nametable + 16 B palette) plus a read of the menu CHR (`ADDR_FCI_MENU_CHR`). Untested here; try once with the user ready to reset (cf. `*r`).
- Behaviour (scroll speed, wrap, paging, buttons): observe on the console; a phone video helps.
  - Observed 2026-11-01: the menu doesn't wrap (Down on the last entry stays put). Our app matches: Up/Down move, crossing a page edge flips the page, Left/Right jump a page (no flash: drawn into the hidden nametable, then flipped).
- The font is Krikzz's artwork, so it's never in this repo: the app ships with edio's MIT font, and the box patches in the font from the user's own card when it installs the app.
- **But never mistakable for the menu:** a different background colour (decided), a "Sync Forever" header, a status line, and a visible "returning to menu" moment so the jump back doesn't look like a crash.

### 4. Sync behavior

- **Saves (cart → save folder), `sfe backup --store DIR [--recent N | --all]`** (`saves.py`), read-only at
  the menu. Versions: `<store>/<ROM filename>/<file>/<NNNN>_<time>_<sha8><ext>`, a new file only when the
  save changed, never overwritten. **Automatic backup (built, `box.py`):** the box doesn't look
  for "game → menu". On the N8 and Mega it stats the recent list every 2 s, and a few seconds
  after its date changes (a game or our app started, so the menu isn't running) it backs up the
  recent games' changed saves. The app backs up every save when it opens; on the X7 that's the
  only backup. The FXPAK backs up at connect, a few seconds after the running ROM changes, and
  every 10 minutes.
- **ROM sync (decided 2026-11-01, built in `romsync.py`):** run from the app only, on A, never
  automatically (`sfe --fxpak sync` also runs it by hand). Library → cart into the **root**
  (subfolders mirrored; `dest` changes it), ROM files only, never the cart's system folder.
  Modes: Add only (default), Update (replace when size/SHA-256
  differ), Mirror (also delete ROMs sync itself copied that left the library: the box keeps a
  per-cart manifest), Mirror STRICT (delete every ROM not in the library, warned on screen). The
  plan ("Add 12 · Replace 2 · Remove 3") is always shown and confirmed first. With a console's
  `msu` setting on (off by default), MSU-1 (SNES) and MSU-MD (Genesis) games sync with their
  audio (`msu.py`).
- **Cart clock:** the host can set it (`CMD_RTC_SET`, edlink `rtcset`); `sfe clock [--set]`,
  and `sfe serve` sets it when an N8 or Mega connects (`[cart] set_clock`). Verified on the N8
  2026-11-01. Cart file timestamps are then meaningful, but sync still compares size + SHA-256.
- **Never while a game runs.** `sfe` commands by hand go through the menu check
  (`ops.require_menu`). The box's sync, firmware and app-update writes happen while our app runs;
  a first install is at the menu (`sfe install` on the N8 and Mega, the box itself on the FXPAK).
  During someone else's game the box only reads. `FxApp._quiet` keeps writes clear of the FXPAK's
  autosave.
- **Don't disturb the menu's folder (found 2026-10-31).** The menu caches its folder listing; removing an entry from the folder it shows causes `Error:81`, and the reboot command `*r` hangs the console, so the box can't force a refresh. Consequences: never delete or rename in the root while the menu is up; ROM sync only adds files (new ROMs appear after the user changes folder); consider syncing ROMs into a subfolder. Listings must restore the menu's folder (`list_dir` does). Adding entries is harmless: the menu reads a snapshot taken at its folder load, so new entries stay invisible until it reloads.
- **Count rule (verified 2026-10-31):** the menu re-reads names from the MCU snapshot but keeps its own entry count. After any change, refreshing the snapshot of the menu's folder with `0x03` is safe if the folder has at least as many entries as the menu loaded (equal shows the change exactly; more hides the last entries until reload; fewer → Error:81). This could allow menu-time changes (e.g. swap one ROM for another) if the box knows the menu's folder and count. Reading `F_DIR_SIZE`/`F_DIR_GET` without `F_DIR_LD` returns exactly what the menu shows (verified in the System Folder), so the box can learn the menu's count and, by matching names against its catalogue of the card, its folder. Only valid if every host listing restores the snapshot afterwards. `list_dir` does this: it reads the snapshot, lists, then finds the matching folder (listed folder, last found, root, EDN8, then breadth first) and reloads it; verified from the System Folder 2026-10-31. A full catalogue on the box could replace the search later.
- **Where each action runs:** at the menu the box only reads, and only what's safe there (a stat of the recent list); saves are read while a game or the app runs. Every write happens while our app runs, where the menu isn't running and re-reads everything on exit. To restart the app after an update, the box starts it itself (`*n` + `*s`, verified 2026-10-31). `EDN8/gamedata` is hidden from the normal list, but the main menu's System Folder entry browses it, so it can be the menu's folder.

### 5. Software layout

The Python package `syncforever` (CLI `sfe`) is the implementation:

| Layer | Module | Notes |
|---|---|---|
| Transport | `transport.py` | USB CDC serial |
| Cart protocol | `n8pro.py`, `x7.py`, `fxpak.py` | One adapter per kind of cart, all with the same file methods; `carts.py` holds what differs per cart |
| Safe operations | `ops.py` | Write roots, menu check, hashing, readback |
| Fake carts | `sim.py`, `x7sim.py`, `fxsim.py` | Test oracles |
| Box service | `box.py` | `sfe serve` |
| App protocol | `appserver.py`, `fxapp.py` | `app-protocol.md` |
| Sync, saves, firmware | `romsync.py`, `msu.py`, `saves.py`, `firmware.py` | Library and saves are plain folders to these |

## Roadmap

| Milestone | Content | Status |
|---|---|---|
| **A** | Laptop bench test (`bench-test.md`). Confirms the protocol assumptions and the menu-status signal. | Done. STATUS isn't a menu signal; the menu ping is. |
| **B** | Sync engine on the laptop: settings file, ROM sync, automatic save backup. | Done. |
| **C** | Pi box: service, `sfe setup`, `sfe doctor`. | Done, on any Pi. |
| **D** | Sync Forever NES app. | Done. |
| **E** | Mega EverDrive PRO adapter (byte-order profile, fake-cart variant, bench run), then the EverDrive-64 X7 (through our N64 app) and the FXPAK PRO; the ED64 PRO once we have one, and the Turbo PRO. | Mega, X7 and FXPAK done. Turbo PRO next: owned, waiting on an AV cable. ED64 PRO once we have one. |

**Genesis app look (2026-11-01):** our own branding, not the menu theme: dark charcoal
(`0x0222`), "SYNC FOR" + red "EVER" drawn 2x, three lines under it (dark red, red, red), all
generated by `md/tools/art.py`. Idea for next:
something with more depth, like the MD Mini theme's look (a dark gradient, a faint grid, a big
dim logo; see themes.arcade-tv.de MD_mini for the mood only) but ours: a dark-red grid that may
drift slowly, and a dim "SF" monogram, kept low-contrast behind the plan. The font may later
come from the cart's own theme (`theme.bgr`), patched in by the box like the NES font.

### 6. Firmware updates **(built 2026-11-02: `firmware.py`, `[box] firmware`)**

On the N8 PRO and Mega, Krikzz ships firmware as one `.efu` file that the cart's own menu
installs. With `[box] firmware = true` the box delivers it, and never flashes anything itself. The
X7's OS and the FXPAK's firmware are a whole folder copied from a computer, so the box only
compares versions and the app reports them.
- **Source:** `krikzz.com/pub/support/<mega-everdrive|everdrive-n8>/pro-series/firmware/`, a plain
  Apache index: `<cart>-fw-vYY.MMDD.efu` (`mega-pro-fw-v26.0925.efu`, `edn8-pro-fw-v25.1123.efu`),
  `changelog.txt` (newest first, one numbered list per version), `efu-install.txt`, `legacy/`,
  `recovery-fw.zip`. Fetched with an honest User-Agent (`SyncForever/<ver>`) from a home
  connection it answers 200; our agent tool's data-centre fetch got 403. Never fake a browser.
  Check at most daily.
- **Which firmware a cart runs:** SYS_INF's sw date tracks the version, give or take a day:
  the Mega reported 2024-12-13 on v24.1213, and 2026-09-24 13:02 after installing v26.0925.
  Compare with a few days' slack. The N8's date doesn't track it (2026-11-02), so the box compares
  the cart's menu file with the CRC32 in the `.efu`.
- **Delivery:** download, copy to the card root at a safe moment (box.StartWatch / the app),
  show a line under Saves in the app (decided 2026-11-01): `Firmware: v26.0925`, the version
  in green when the cart runs the latest, red when a newer one is waiting (the body palette's
  spare colour 2, set per state; the 12 version glyphs drawn in colour 2 fit the 15 free tiles). Install steps stay in `efu-install.txt` and the docs, not on screen. Once the cart reports the new date, remove the files the box delivered (`delivered.json` in the box's firmware folder), never a firmware file the owner put there (the first build removed one, 2026-11-02).
- **After an update, re-identify:** SYS_INF layouts have moved between firmware versions.
- A local `firmware/` folder can stand in for the download (offline boxes).
- **Open: is the box safe while the menu installs firmware?** The box stats the recent list every
  2 s at the menu (through the MCU that's flashing). Krikzz's efu-install.txt says nothing about
  USB. The first N8 install (2026-11-02) was done with the box stopped.

### 7. Cheats **(idea, 2026-11-02)**

The carts keep cheats as files next to the saves: the N8 PRO `EDN8/gamedata/<game>/cheats.txt`
(already in its backed-up save files), the Mega likewise under `MEGA/gamedata/`, the FXPAK
`sd2snes/cheats`, the X7 `ED64/gamedata/` (OS changelist: "saves and
cheats"). They're edited in the menu, so like saves they change on the cart.
To decide: only back them up with the saves (today, on the N8, Mega and FXPAK), or also sync cheat files from the
library to the carts (and which side wins when both changed).

## Open questions

1. **Who is it for?** Only your own setup, or something others build and use. This
   decides how much setup polish is needed and which network shares must be supported
   (another person's Synology versus your Proxmox).
2. **Network shares on the Pi:** settled: mount SMB/NFS directly, by hand. The box only uses the
   paths it's given.
3. **Electrical:** whether the N8 PRO's USB VBUS feeds back into the box, and whether the
   cart needs VBUS present to be detected (`safety.md`). Not measured; a Pi 4 has run on the carts with no
   damage seen (2026-11-04).
4. **Menu-status signal:** answered: idle STATUS isn't "at the menu"; it's sticky (verified on
   hardware). The write check is the menu ping, and the save trigger is the recent list's date.
5. **Save file locations and timing:** answered per cart (`cartridge-compatibility.md`).
6. **USB link during play:** moot for STATUS: the box doesn't poll it. It stats the recent list
   every 2 s (716 stats safe during heavy menu use, `test-results.md` 2026-11-01).

## Naming

**Sync Forever.** It hides "Sync for EverDrive" without looking like an official Krikzz
product. Code, repo and package: `syncforever`. CLI: `sfe`. App title: "Sync Forever".
