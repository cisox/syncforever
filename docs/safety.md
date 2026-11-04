# Safety

## Guards built into syncforever

| Risk | Guard | Where |
|---|---|---|
| Writing over real ROMs/saves during testing | Writes, mkdir and rm are refused outside `/syncforever-test` unless `--write-root` or `--allow-write-anywhere` is given. The check runs before any byte is sent. | `ops.WritePolicy` |
| Sync touching the wrong files | ROM sync's writes stay under the configured cart folder. It touches ROM files only, never the system folder (`EDN8/`, `MEGA/`, `ED64/`, `sd2snes/`), and Mirror deletes only files it owns. | `box.policy_for`, `romsync.guard` |
| Writing while a game runs | Manual write commands on the N8 and Mega first ping the menu (`*t` into the host→menu FIFO). Only the ROM-select menu answers `k`; no answer within 1 s means refuse. Verified on hardware 2026-10-31. On the X7 the same check asks our app, and manual writes on the FXPAK need its menu. The box's ROM sync writes while our app runs, not at the menu. | `ops.require_menu`, `x7.X7App.menu_ping`, `cli._fxpak_at_menu` |
| Two programs on one cart | Cart ports open with an exclusive lock. A manual `sfe` run as root stops the box service for the command and starts it again. | `transport.open_port`, `cli.main` |
| Overwriting a save | The box never writes a cart's saves: backups only read them. | `saves.py` |
| Clobbering an existing file | Uploads open with `FA_CREATE_NEW`, so an existing file fails with `FR_EXIST`. `--overwrite` switches to `FA_CREATE_ALWAYS`. | `ops.upload` |
| Silent corruption | `put` reads the file back and compares SHA-256 (skip with `--no-verify`). | `cli.cmd_put` |
| Wedging the cart mid-write | Ctrl-C is held until the current `F_FWR` payload is fully sent, and each command carries at most 64 KiB. A link failure mid-payload marks the client wedged, and it refuses further commands until the console is power-cycled. `sfe serve` waits for writes in progress before it exits. | `n8pro.N8Pro.write`, `n8pro.stop_writes` |
| Half-written upload left behind | If an upload we created fails (not wedged), the partial file is closed and deleted. | `ops.upload` |

## Hardware rules

The full list, with evidence, is "Landmines" in `CLAUDE.md`, which wins if this differs. The main ones:

- Never send a partial `F_FWR` payload. Only a power cycle recovers the cart.
- Open cart ports only through `transport.open_port`.
- Never send `mem_wr` with exec `0xAA`, `CMD_HARD_RESET (0x12)` or the menu command `*r`.
- Don't talk to a menu that may be in use: only `F_FINFO` (stat) proved safe. Pings give
  Error:40/2A, and opening a file gives Error:09 and could corrupt a save the menu is writing.
- FXPAK PRO: never GET a missing file or cut a GET short, keep PUT data paced, and no PUT may
  meet the cart's autosave. Never send RESET or POWER_CYCLE.
- X7 ROM code: access the cart's registers only by PI DMA, never CPU loads and stores.
- Never overwrite a backup file. Never use cart mtimes for sync decisions.

## Assumptions the guards rested on

Both are settled.

1. ~~Idle STATUS = `0x00` means "at the menu".~~ **Disproved 2026-10-31:** STATUS is the
   sticky result of the last command, so it can read `0x00` mid-game. The gate now uses
   the menu ping. An unanswered ping leaves 2 bytes in the FIFO; a normal game never
   reads it, and our app skips bytes outside its frames (`app-protocol.md`).
2. **File commands work at the menu.** NESCD says SD file commands are "served by the cart's
   menu ROM" and gates them on the menu. **Settled 2026-11-01:** they also work while a game
   runs (reads, no glitch) and while our app runs (reads and writes). Writes during someone
   else's game weren't tried, and the gate refuses them.

## Electrical (not measured)

In use (owner, 2026-11-04): a Raspberry Pi 4 plugged into the carts, consoles on and off, with no
damage seen. That's experience, not a check: nobody has measured or inspected how a cart's
USB power (VBUS) meets the console's. Carts do show up on USB with their console off, from the
cable's power alone, and don't answer until it's on (a hub with the N8, Mega and FXPAK, one
console on at a time, 2026-11-04).

None of the references document the N8 PRO's USB VBUS input circuit. NESCD powers its
RP2350 host and the NES from one supply, which shares ground but leaves the VBUS path
unexamined. Until it is checked:

- For the laptop tests, a laptop on battery (unplugged from its charger) avoids a
  ground loop through mains earth.
- Before any embedded host, inspect the cart's USB port circuit (look for a diode or
  load switch on VBUS) or measure: with the NES on and USB unplugged, check for voltage
  on the cart's VBUS pin. With the laptop connected and the NES off, check whether the
  cart powers up from USB.
- `SYS_INF` includes `pwr_sys`/`pwr_usb` fields (N8 @53/@54, Mega @56/@57, `carts.py`). Both
  read 1 on the N8 with the NES on and USB connected. Compare `sfe info --raw` dumps across
  states to see what they tell us.
