# Sync Forever

A small box (a Raspberry Pi) that plugs into a flash cart's USB port (EverDrive-N8 PRO, Mega
EverDrive PRO, EverDrive-64 X7, FXPAK PRO) and keeps it in step with your own storage, without ever
taking the SD card out:

- **Save backup:** every game's saves (battery saves, save states, Sega CD memory) are copied
  to your save folder as versioned files that are never overwritten, save states in a folder of their
  own if you like. It happens a few seconds after
  you start a game, and whenever you open the Sync Forever app (on the X7, only when you open
  the app).
- **ROM sync:** the Sync Forever app on the TV previews what would change (Add only, Update,
  Mirror, Mirror strict) and syncs your ROM library to the card on A. If the library changes
  while the app is open, the box plans again. Files already on the card that the box has no
  record of (say, copied by card reader) are checked once: the Mega checksums them itself at
  about 4 MB/s; the other carts read them over USB. If that would take over 10 minutes, a spot
  check decides whether the rest can be trusted by name and size (`first_look`).
- **Moves, not copies:** a ROM renamed or moved in the library is renamed on the card instead of
  sent again: on the Mega the cart copies it to its new name and the box deletes the old (no data
  over USB). A file with no checksum on record that has the same name and size is checked once first. Not on the N8 (its move command answers success and moves nothing; its copy is
  untried); the FXPAK and X7 renames are still to try (`sfe mv`).
- **MSU games:** with the per-console `msu` setting (off by default), MSU-1 (SNES) and MSU-MD
  (Genesis) games sync with their audio.
- **Every cart at once:** plug several carts into the box through a USB hub; each is
  recognised and served at the same time, with its console's own ROM and save folders.
- **Hands off:** after one command to put the app on the cart (`sudo sfe install` at the menu,
  `sfe --x7 install` on the X7; the FXPAK needs none), the box keeps it up to date (it restarts
  itself after an update; on the X7 the new version runs the next time you open it), sets the
  clock on the N8 and Mega, and never disturbs the menu or writes to the cart during a game.
- **Safe with a missing library:** an empty or unreadable library stops the plan, so mirror and
  strict never delete your ROMs because a share wasn't mounted.

`docs/design.md` is the product design.

## Status (2026-11)

| Cart | Status |
|---|---|
| **EverDrive-N8 PRO** (NES) | Working on hardware: save backup, ROM sync, the NES app, app install and self-update, new firmware delivered to the card. Built, tested in the emulator, not yet run on the cart: the sync screen |
| **Mega EverDrive PRO** (Genesis) | Working on hardware: save backup (saves, save states, Sega CD memory), ROM sync of all six Sega systems, the Genesis app, app install and self-update, new firmware delivered to the card. Built, tested in the emulator, not yet run on the cart: the sync screen, MSU-MD games |
| **EverDrive-64 X7** (N64) | Working on hardware: everything through our N64 app (the menu has no USB file access): save backup, ROM sync of N64 ROMs and every system the card's emulators run, the copy panel, `sfe --x7 install`, app updates for the next start; OS version shown, not installed. Built, not yet run on the cart (no N64 emulator here): B to stop, the time left and a bar for the whole sync, "Library changed" |
| **FXPAK PRO** (SNES) | Working on hardware: save backup (saves, save states), ROM sync, the SNES app with its sync screen, app install and self-update; firmware shown, not installed. MSU-1 games on the card are recognised; a big audio upload hasn't yet run to the end on hardware (`docs/snes-plan.md`) |
| EverDrive-64 PRO (N64) | Planned, once we have the hardware: everything N64 so far is built and tested on the X7 |
| Turbo EverDrive PRO (PC Engine) | Next: owned, waiting on an AV cable for testing (`docs/cartridge-compatibility.md`) |

Firmware: with `[box] firmware = true`, the box copies new N8 and Mega firmware to the card for
you to install from the cart's menu. It never flashes anything. The X7's and FXPAK's are only
reported in the app.

Your library and saves are just folders: on a disk attached to the box, or on a network share you mount on it. The box only uses the paths it's given.

## Layout

| Folder | What |
|---|---|
| `host/` | The box software (Python): the `sfe` CLI, `sfe serve` (the box service), cart protocol, sync, save backup, test harnesses |
| `app/` | What the NES and SNES apps share (C, cc65): the screens' logic, the app protocol, the sync clock |
| `nes/` | The Sync Forever NES app: its screen, cart I/O and exit |
| `md/` | The Sync Forever Genesis app (C, m68k-elf-gcc) |
| `n64/` | The Sync Forever N64 app for the X7 (C, libdragon in Docker): file service and screen |
| `snes/` | The Sync Forever SNES app for the FXPAK PRO: its screen, link and exit |
| `deploy/` | Pi install and release scripts, systemd unit, example config |
| `docs/` | Design, protocols, hardware test log |

## The box software (`host/`)

```sh
cd host
uv sync
uv run sfe --help
uv run sfe --sim /tmp/fake-sd ls /    # a simulated cart: no hardware needed
uv run sfe info                       # the real cart over USB
uv run sfe serve                      # every cart on USB, settings from the config file
uv run pytest                         # ~370 tests: simulated carts, the NES and Genesis apps emulated
```

`sfe serve` reads its settings from `/etc/syncforever/config.toml`, `~/.config/syncforever/config.toml`
or `$SFE_CONFIG` (`deploy/config.example.toml` explains each), with flags on top. On a Pi, install
or update it as the `syncforever` service with `curl -fsSL https://raw.githubusercontent.com/cisox/syncforever/main/deploy/get.sh | sudo bash`;
`sudo sfe setup` asks for the settings and `sudo sfe doctor` checks them (`deploy/README.md`).

| Module | Role |
|---|---|
| `carts.py` | What differs per cart (byte order, layouts, folders, ROM types, launch command), picked from the IDs it reports |
| `n8pro.py` | `EverDrive`: one method per USB command, no policy |
| `x7.py` | The X7: the box's side of our N64 app's file service |
| `fxpak.py`, `fxapp.py` | The FXPAK PRO: its USB file service, and talking to the SNES app through cart memory |
| `ops.py` | Write guard, menu check, streaming copy with SHA-256 and readback, launch |
| `box.py` | `sfe serve`: one thread per cart port; set each cart's clock, watch for game starts, run the app server |
| `appserver.py` | The box side of the app protocol (`docs/app-protocol.md`) |
| `romsync.py`, `msu.py` | Sync plans and runs, with a manifest of what sync copied; MSU games and their audio |
| `saves.py` | Versioned per-file save backup |
| `apps.py`, `chr.py` | Installing the apps, with the N8's own menu font patched into the NES app |
| `firmware.py` | Finding new firmware; delivering it to the N8 and Mega, reporting it on the X7 and FXPAK |
| `config.py` | The config file, with per-console `[nes]`/`[genesis]`/`[n64]`/`[snes]` settings |
| `setup.py`, `doctor.py` | `sfe setup` and `sfe doctor` |
| `sim.py`, `x7sim.py`, `fxsim.py` | Fake carts for tests |
| `nesemu.py`, `mdemu.py` | A headless NES and Genesis that run the apps in tests |

## The apps (`nes/`, `md/`, `n64/`, `snes/`)

```sh
cd nes && make && make package   # cc65; package = the copy the box ships
cd md && make && make install    # m68k-elf-gcc; install = put + launch on the cart via sfe
cd n64 && make && make package   # libdragon in Docker (docs/n64-plan.md)
cd snes && make && make package  # cc65
```

All four show the same screen: the sync plan for the chosen mode, the save backup made as the
app opened, A to sync, Left/Right for the mode, B back to the menu (the X7 has no way back: turn
the N64 off and on). While syncing, the NES, SNES and Genesis apps show a full-screen view with
progress bars, time estimates and "B: stop after this file", and slow checks before a sync use it
too; the N64 shows a copy panel with bytes done. When a sync ends, the app says how long it took and
what it did; when one fails, how far it got and on which file. The NES and Genesis apps use
Krikzz's MIT-licensed `edio` library for the cart's I/O.

The NES and SNES apps share `app/`, so a change there is one change, and the NES emulator tests
cover the SNES app's logic too; each console's `src/` holds only its screen, how bytes reach the
box, how it leaves, and a few settings (`plat_config.h`). The box installs and updates these on the
cart; you only build them to change them.

## Docs

- `docs/design.md`: the product design and its decisions
- `docs/app-protocol.md`: how the apps and the box talk
- `docs/usb-protocol.md`: the cart USB protocol and the menu's commands, with sources
- `docs/cartridge-compatibility.md`: per-cart facts (N8 PRO, Mega PRO, …)
- `docs/n64-plan.md`, `docs/x7-protocol.md`: the EverDrive-64 X7 plan and protocol
- `docs/snes-plan.md`: the FXPAK PRO facts and plan
- `docs/safety.md`, `docs/bench-test.md`, `docs/test-results.md`: guards, hardware runbook, results

## Licensing and sources

Protocol facts come from krikzz/edn8-pro-pub and krikzz/mega-ed-pub (MIT), ThroatyMumbo/NESCD
(MIT), ThroatyMumbo/NESPC (GPL-3.0, read for facts only, no code copied), and Krikzz's
`edlink.exe` (decompiled for facts only, no code copied). FXPAK facts come from mrehkopf/sd2snes
(GPL-2.0) and Skarsnik/QUsb2snes (GPL-3.0), read for facts only, no code copied. The NES and
Genesis apps vendor Krikzz's MIT `edio`.
The menus' fonts and themes are Krikzz's or their authors' artwork and are never committed or
shipped: the box patches a cart's own font into the app when it installs it.
