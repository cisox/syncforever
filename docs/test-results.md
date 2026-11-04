# Test results

Dated results, oldest first, for the N8 PRO and the Mega EverDrive PRO. They are history: a later row can change an earlier one. Raw benchmark lines go to `docs/results/bench.jsonl`. X7 results are in `n64-plan.md`; FXPAK PRO findings are in `CLAUDE.md` (Landmines) and `snes-plan.md`.

## Cart

The N8 PRO as first seen, 2026-10-31.

| Field | Value |
|---|---|
| USB ID | **`0483:5740`** (STMicroelectronics stock VCP), product "EverDrive N8", serial `00000000001A`. *Not* the `38DF:0017` NESCD/NESPC report. (`38DF:0017` since firmware v25.1123, 2026-11-02.) |
| Port | `/dev/cu.usbmodem00000000001A1` (macOS, no driver needed) |
| USB | Full speed (12 Mb/s), requests 100 mA |
| Identify | protocol `0x06`, device `0x17` (matches references) |
| SYS_INF raw | `173d52edf12c5b24080043001950323758303820104e0500864e00005a0000000d0000003e5b878403010100020117000000000000010117255a080d00000000` |
| Firmware build (SYS_INF @36) | 2025-09-30 16:36 |
| Versions (edio layout) | mcu `0x0103`, hw `0x0001`, boot `0x0102`. **Menu screen shows OS 25.0106**, which matches `EDN8/nesos.nes`'s date (2025-01-06), so the menu version isn't in SYS_INF |
| Serial | `00054E10.00004E86` |
| Power flags (edio layout @53/@54) | `pwr_sys` = 1, `pwr_usb` = 1 |
| SD card | `edn8-pro-fw-v25.1109.efu` sits in the root, **not applied** (menu still 25.0106). Updating may change the USB ID and SYS_INF layout; finish the bench test first. (Later: v25.1123 installed 2026-11-02; the USB ID changed, the SYS_INF layout didn't.) |
| Host | macOS, 10 ft micro-USB cable |

## SD layout (observed)

- ROMs: flat in the root (~240 `.nes` files).
- System: `EDN8/` → `gamedata/`, `MAPS/`, `syscore/`, `sysdata/`, `nesos.nes` (menu), `mappers.png`.
- **Saves: `EDN8/gamedata/<ROM filename>/bram.srm`** (8192 B each). The folder is named
  after the full ROM filename including `.nes`. `gamedata/readme.txt`: "All game data stored
  here (saves, cheats, disp-switch settings etc)".
- `EDN8/sysdata/`: `recent.bin`, `registry.bin`, `fm-state.bin`.

## Protocol assumptions

| Assumption (`usb-protocol.md`, `safety.md`) | Observed | Date |
|---|---|---|
| USB VID `0x38DF` | **No.** `0483:5740` on this firmware; detection now accepts both | 2026-10-31 |
| Identify handshake (STATUS2 + STATUS) | Yes | 2026-10-31 |
| SYS_INF offsets as NESCD/NESPC parse them (sw_ver @44) | **Doubtful.** The plain edio struct layout fits: `device_id` @46 = `0x17`, build date @36 decodes sanely | 2026-10-31 |
| Idle STATUS = menu/game flag | **No. STATUS is the sticky result of the last command.** A failed delete at the menu → `0x05` (FR_NO_PATH), repeated reads stay `0x05`, and a successful `stat` → `0x00`. In-game values (8 Eyes `0x05`, Zelda `0x04`) fit the menu's last lookup at launch (no gamedata folder vs no file) | 2026-10-31 |
| Menu ping (`*t` → FIFO `0x1810000`, reply `k`) | **Yes.** At the menu: `k` in 11 ms. In 8 Eyes: no reply in 2 s, link fine afterwards. Leftover bytes produced no late reply across the in-game reset. **This is now the write gate** | 2026-10-31 |
| Polling STATUS during play | 60 polls over 30 s in Zelda: no visible or audible glitch, every reply < 50 ms | 2026-10-31 |
| Save written to SD by the time the menu is back | **Yes.** Zelda: died → Save → in-game reset; `bram.srm` differed in 1,606 bytes once at the menu | 2026-10-31 |
| Cart clock | Save mtime read 2026-03-09 15:13 when written 2026-10-31 15:58, so the RTC is wrong. Don't trust cart timestamps for sync | 2026-10-31 |
| `F_FINFO` returns the name | **No**, an empty name. `stat` falls back to the path's basename | 2026-10-31 |
| File commands respond mid-game | **Yes**, answered by later rows: "Host file reads during a game", and "Host file commands while our app runs" for writes in our own app. Writes during someone else's game weren't tried; the write gate refuses them | 2026-11-01 |
| `F_DIR_GET` paging in blocks of 32 | Works: ~245-entry root listed correctly | 2026-10-31 |
| `F_FRD` 4 KiB status-byte blocks | Works: 768 KiB ROM read with intact iNES header | 2026-10-31 |
| Replug without power cycle recovers | Yes. USB unplugged and replugged with the NES on; the next commands worked with no special handling | 2026-10-31 |
| Writes at the menu (bench step 4) | **Yes.** mkdir, put + readback verify, second put refused with `0x08 FR_EXIST`, rm. Full `pytest --hardware` passed (5/5 with `--sd-backup`) | 2026-10-31 |
| Card reads match a card-reader backup | **Yes.** All 232 root files (57.1 MiB) SHA-256-identical to `~/Desktop/NESBak`, 754 KiB/s | 2026-10-31 |
| USB directory listing leaves the menu alone | **No.** The MCU holds one loaded directory shared by the menu and USB. After `sfe ls /syncforever-test` (1 entry), scrolling the menu gave `Error:81 Unexcpected error` (sic; 0x81 = `ERR_OUT_OF_DIR`) immediately. Reproduced on demand | 2026-10-31 |
| Reloading the menu's directory repairs it | **Yes.** `F_DIR_LD` of the root afterwards: no error. With options `0x01` the menu then showed `EDN8`; with `0x03` (SORTED\|HIDESYS, `edio/everdrive.h`) it matched the menu again (233 vs 234 entries), so **the menu loads with `0x03`**. Folders are drawn orange in the menu (a second new folder was orange too). `syncforever-test` showed non-orange only before the repair, so the menu drew it from mismatched entries during the clash | 2026-10-31 |
| A file read leaves the menu alone | **Yes.** `sfe get` of one file (stat, open, read, close), then scrolling: no error | 2026-10-31 |
| `list_dir` with the root restore leaves the menu alone | **Yes.** `sfe ls /syncforever-test` with the restore, then scrolling a page and back: no error | 2026-10-31 |
| Changing the menu's folder contents leaves the menu alone | **No.** Deleting `/syncforever-test` (a root entry) at the menu, then scrolling up past the top: `Error:81`. The menu keeps its own entry count. An earlier unexplained Error:81 followed deleting `/syncforever-test2` the same way | 2026-10-31 |
| Menu command `*r` (reboot, edlink `reset`) refreshes the menu | **No, it hangs it.** Ack `00` in 20–30 ms, then no ping reply and a black screen until console reset. Same from the error screen and from a healthy menu. `boot_ctr` counts power-ups, not resets | 2026-10-31 |
| Folders drawn orange | Every folder (new, empty, plain `0x10` attributes) is orange; a card-reader-made folder not yet checked | 2026-10-31 |
| Adding an entry to the menu's folder leaves the menu alone | **Yes.** `mkdir /syncforever-test` at the root while the menu showed it, then scrolling bottom and back: no error, and the folder never appeared (it would sort first). Deleting it again, then scrolling: no error. So the cart serves the menu a **snapshot** taken at the menu's folder load; additions are invisible until the next load, deleting a snapshotted entry gives Error:81 | 2026-10-31 |
| The host can refresh the menu's snapshot | **Yes, but the menu keeps its own entry count.** `mkdir /syncforever-test`, then `F_DIR_LD ""` with `0x03`: the folder appeared at the top as the menu paged, entries shifted down one, and the true last entry (Zombie Nation) was cut off (menu still at 232 of 233). Then `rm` + refresh back to 232: folder gone, Zombie Nation back, no error. So the menu re-reads names from the MCU snapshot but keeps its count from its own load; any change is safe if the refreshed snapshot has **at least** the menu's count (equal = exact view) | 2026-10-31 |
| The host can read the menu's snapshot without loading | **Yes.** User in the System Folder; `F_DIR_SIZE` + `F_DIR_GET` with no `F_DIR_LD` returned 6 entries (`gamedata/ MAPS/ syscore/ sysdata/ mappers.png nesos.nes`) matching the screen and its order. Gives the menu's folder contents and count, not its path | 2026-10-31 |
| `list_dir` restores the folder the menu actually shows | **Yes.** Menu in the System Folder; `sfe ls /` (233 entries) took 0.53 s including the search, and the snapshot afterwards was EDN8's 6 entries. Moving through the System Folder, then back to the root and scrolling: no error | 2026-10-31 |
| Launch with menu `*n` + `*s` | **Yes.** At the menu: `*n` + u16 LE length + path (no leading slash, `8 Eyes (USA).nes`) in one `MEM_WR` to the FIFO → status `00` after 0.46 s, then u16 mapper `4` (MMC3); `*s` → 8 Eyes running normally, menu ping silent afterwards | 2026-10-31 |
| `sfe launch` | **Yes.** `sfe launch "/Legend of Zelda, The (USA) (Rev 1).nes"` → mapper 1 (MMC1), Zelda running, 0.7 s end to end. While 8 Eyes ran, `sfe launch` refused (menu ping silent) | 2026-10-31 |
| Launch loads/writes back the battery save like a menu launch | **Loads: yes.** Zelda launched by `sfe launch` showed the existing save on file select. Write-back after save + in-game reset: not yet checked (later yes: the `sfe serve --saves` row, 2026-11-01) | 2026-10-31 |
| Our NES app, first version runs (mapper 0 NROM-256, `make install` = `sfe put` + `sfe launch`) | **Yes.** Menu font (from `nesos.nes` CHR) matches the menu, though it looks a little smaller on screen; the edio cart check (`REG_MSTAT` high nibble `$A`) passes on the cart | 2026-11-01 |
| Two 32-byte nametable rows from a cc65 C loop fit in polled vblank | **No.** Cursor moves (2 rows) scattered garbage tiles; 1 row (A) was fine. An asm copy (`scr_copy32`, ~16 cycles/byte) fixed it | 2026-11-01 |
| edio's `ed_exit_game()` returns to the menu | **No, as written.** "Returning to menu..." then a hang; the in-game reset no longer worked, power cycle needed. Writing `map_idx=255` maps the menu's ROM over ours at once (`fpga/base_sv/everdrive.sv`), so code running from our ROM is lost. Sending the same `CMD_MEM_WR` from zero page and then `jmp ($FFFC)` (`nes/src/fast.s` `exit_to_menu`) works: menu back, ping `k` | 2026-11-01 |
| Flash-free paging (draw next page into the hidden nametable, 3 rows per vblank in asm, then flip `PPU_CTRL`) | **Yes.** Left/Right pages, Up/Down across page edges, held repeat and a partial last page all clean, no flash | 2026-11-01 |
| Cart clock `CMD_RTC_GET` (0x14) / `CMD_RTC_SET` (0x15) | **Yes.** Read 2026-03-10 14:02 (months off); `sfe clock --set` at the menu (no reply, as edlink) then reads back within 0 s; files written afterwards carry the right mtime | 2026-11-01 |
| `sfe backup --all` | **Yes.** 4 saves in 0.6 s, second run all "unchanged"; Crystalis and Maniac Mansion byte-identical to the SD backup (Zelda differs: saved since) | 2026-11-01 |
| App protocol v1 both ways (`CMD_USB_WR` app→box, `MEM_WR` to the FIFO box→app, PAGE ~550 bytes) | **Yes.** WELCOME, list, open folder, back (cursor restored) | 2026-11-01 |
| Host file commands while our app runs | **Yes.** DuckTales (128 KiB) copied with readback verify in < 1 s while our NES app idled; sync "add 2" (Contra + Bugs Bunny) in ~1 s; then launch | 2026-11-01 |
| Launching at the menu's first ping answer after our app exits | **No.** With pings every 0.5 s from 0.5 s after LAUNCHING (app exits ~0.75 s after it), the menu showed Error:40 (`ERR_UNXP_STAT`), Error:48 (`ERR_MAP_NOT_FOUND`) or Error:2A across runs, sometimes the game still started. First ping 1.5 s after LAUNCHING, launch 0.3 s after the first answer: game in 2.3 s, no error. (3.0 s + 2.0 s also worked; 1.5 s + 0.3 s was the setting then, tested a few times only. It failed on 2026-11-02, see "N8 relaunch after an app update"; the box went back to the slower timing) | 2026-11-01 |
| The cart drops off USB when the menu starts / leaves a program | **Only with no box talking to it.** With the port closed, or opened by a listener that never sends, the cart vanished from IOKit for ~0.2 s at app start and at app exit (open handle dead afterwards: `ENXIO`). With `sfe serve` holding the port, a watcher saw no drop through a sync, app exit and game start. A reset drops it for longer. `sfe serve` reopens the port on a dead handle | 2026-11-01 |
| Menu resolves `*n` paths from the root even when showing a subfolder | **Yes.** Menu inside `/syncforever-test`, `sfe launch "/syncforever-test/sync/DuckTales (USA).nes"` started it | 2026-11-01 |
| Sync screen mode change without blanking (rows rewritten in place via the vblank queue) | **Yes.** No flash; box no longer sends "Reading library/cart" per plan | 2026-11-01 |
| `recent.bin` records games started with `*n`/`*s` | **Yes.** DuckTales, only ever started by the box, sat in slot 1 behind our NES app (started from the menu). Entries repeat (our NES app in slots 0 and 2) and stale ones stay (the deleted `remote-launch.nes`); `saves.recent_games` dedupes | 2026-11-01 |
| `CMD_MEM_RD` (0x19) of the MapConfig (`ADDR_CFG`, 42 bytes) during a game | **Works, harmless, < 1 ms**: DuckTales showed no glitch; `map_idx` (+32) = 2 (its mapper). At the menu after our app's exit: 255 (we write that). **But the in-game reset leaves it at 2**: the menu was up (ping `k`) with `map_idx` still 2, so it can't detect a game exit | 2026-11-01 |
| The cart drops off USB on the in-game reset | **No.** Port closed, IOKit watched every 10 ms through DuckTales → in-game reset → menu: no drop. (It does drop when the menu starts a program and when our app exits) | 2026-11-01 |
| Menu pings flooded into a running game's FIFO (1100 `*t`, 2200 bytes, DuckTales) | **Harmless; the menu answers again right after the reset.** The game ran normally. Run 1: the next `MEM_RD` timed out (link stuck until the reset), afterwards connect/ping/`map_idx` all fine. Run 2: `MEM_RD` still answered; pings every ~2.7 s returned no reply (no error) during play, and the first ping after the in-game reset got `k`. Neither run echoed a backlog of `k`s (the menu clears the FIFO). So slow pings can detect a game exit | 2026-11-01 |
| `sfe serve --saves`: save backup when a box-started game exits | **Works.** Zelda (renamed copy, fresh save) launched from our NES app, saved in game, in-game reset: the log said "backed up" (8192 bytes) and the stored version's SHA-256 matched the cart's `bram.srm`. Startup check of all 5 saves: none changed, none stored | 2026-11-01 |
| Menu pings every 3 s while the user uses the menu (`box.MenuWatch`) | **No: Error:40 ("Unexcpected io status").** After leaving the app (menu answered 18:52:59), one ping went unanswered (18:53:03, nothing started: the menu was busy) and the menu answered again 18:53:06, when the box read `recent.bin`. Then Error:40 on the TV. Likely a host command (ping or file read) landing during the menu's own SD work, whose status check then sees ours (STATUS is sticky). Box stopped; MenuWatch must not talk to a menu that's in use (`box.StartWatch` replaced it, below) | 2026-11-01 |
| The cart drops off USB when the menu starts a game (port held open by a silent listener) | **No.** Zelda started from the menu: no drop. The drops seen before come from our app's start and exit (its USB init), so they're no game-start signal | 2026-11-01 |
| Host file reads during a game (`recent.bin` 10 KB, a save 8 KB) | **Fine:** 0.07 s and 0.02 s, no glitch in Zelda. (The ping during play returned no answer, as expected) | 2026-11-01 |
| Why pings upset a busy menu | edio (`everdrive.c`) reads the MCU's replies to the menu's own commands from the same FIFO host `MEM_WR`s write into (`ed_fifo_rd` after `ed_cmd_tx`). A ping during menu I/O is read as the reply: Error:40 (`ERR_UNXP_STAT`), and Error:2A is `*`, the ping's first byte | 2026-11-01 |
| Save backup when the Sync Forever app opens (`AppServer`, all saves, no pings at the menu) | **Works.** Zelda saved, in-game reset, console reset, app opened: "checked 6 saves, backed up 1: Zelda (SFE test).nes" (version 0002 next to 0001). No menu errors: the box sent nothing while the menu was in use | 2026-11-01 |
| Host `F_FINFO` (stat) every 0.5 s while the user starts games, saves, resets, opens folders (3 min) | **Safe:** 716 stats, 0 failures, no menu errors. Mostly 3-4 ms; 88 ms once while the menu started a game (the MCU queued ours). Dates are right (clock set): the save's date changes ~immediately after the in-game reset, `recent.bin`'s when a game starts | 2026-11-01 |
| Host open + read + close of `recent.bin` every 0.5 s while the user uses the menu | **No: Error:09 (`FR_INVALID_OBJECT`) while starting DuckTales**, ~1 min in. The MCU has one file object shared by USB and the menu: our open/close invalidated the ROM the menu was loading. During a menu write (a save after the in-game reset) this could corrupt the file | 2026-11-01 |
| `box.StartWatch`: stat `recent.bin` every 2 s at the menu; 5 s after a start, back up the recent games' changed saves | **Works, no menu errors** over ~2 min of starts, saves and in-game resets: every start detected (2 s), each new Zelda save backed up 5 s into the next game (3 new versions); unchanged saves skipped | 2026-11-01 |
| App self-update: box replaces the app's file at its first HELLO, sends RESTART, starts the new copy via the menu (`MENU_FIRST_PING` 1.5 s, `MENU_SETTLE` 0.3 s) | **Works, no errors:** "Updating...", menu, new copy started 3.83 s after RESTART and connected 1 s later. Older apps (pre-v6) get "Updated: B, then reopen" instead (also worked) | 2026-11-01 |
| Mega EverDrive PRO identify (STATUS2, STATUS, SYS_INF, RTC_GET; read-only) | **Answers like the N8 PRO, big-endian:** protocol `0x05`, device `0x18`, STATUS `a500`, SYS_INF serial/counters/firmware sensible only big-endian (`cartridge-compatibility.md`) | 2026-11-01 |
| Mega: `sfe info --raw`, `sfe stat /MEGA` and `/MEGA/megaos.dat` through the profile client | **Work:** host-sent numbers are big-endian too (the F_FINFO path length). megaos.dat 256 KiB, 2024-12-13 20:59 | 2026-11-01 |
| Mega: folder listing at the menu (`sfe ls /MEGA/sysdata`, `/MEGA/gamedata`; the menu's folder restored as on the N8) | **Safe:** no error on scrolling or opening folders afterwards. Layout like the N8 under `/MEGA`: `sysdata/recent.dat` (10752 bytes, the N8 `recent.bin` size), `gamedata/<rom>/`, `sysdata/theme.bgr` (the menu background?), `fm-state.dat`, `registry.dat` | 2026-11-01 |
| Mega: menu ping `*t` at an idle menu (MEM_WR to `0x1810000`, big-endian) | **`k`**, like the N8; nothing on the TV, STATUS 0 after | 2026-11-01 |
| Mega after the v26.0925 firmware update: `sfe info --raw` | **Same layout**, built 2026-09-24 13:02 (was 2024-12-13: v24.1213); mcu 1.13 and boot 1.03 unchanged | 2026-11-01 |
| Mega: N8 launch `*n` + path (big-endian length) at an idle menu | **Ignored:** no reply in 5 s, no late bytes, menu responsive, ping and STATUS fine after. `*s` not sent. The Mega's `edlink.exe` (mega-ed-pub) knows `*a *f *g *i *r *s *t *v`, no `*n`: `*i` ("app instalation error") then `*s` for RunUSB; `*f`/`*r`/`*g`/`*a` likely RunSDC. Don't guess: `*r` blacked out the N8 | 2026-11-01 |
| Mega: launch with `*i` + "/megadrive/Altered Beast (USA, Europe) (Rev 2).md" (big-endian length), then `*s`, at an idle menu (no reset first) | **Works:** `*i` answered 0 in 0.38 s; after `*s` the game ran, no errors; the menu stopped answering pings | 2026-11-01 |
| Mega: `sfe launch "/megadrive/Altered Beast (USA, Europe) (Rev 2).md"` (profile client: ping, stat, `*i`, `*s`) | **Works:** the game ran, no errors | 2026-11-01 |
| Mega writes at the menu: mkdir `/syncforever-test`, upload 200 KB + readback, delete file and folder | **Works:** 425 KiB/s, SHA-256 matched; menu fine after (folders browsed in and out; the root holds only folders) | 2026-11-01 |
| Mega `MEGA/gamedata/<rom>/` contents | Castlevania Bloodlines: `00.SAV` 212992 B (save state, slot 00); Sonic 3 & Knuckles: `bram.srm` 8 KB (battery save); Sonic CD: `cd-bram.brm` 8 KB (Sega CD backup RAM). Save backup must take every file in the folder, not only `bram.srm` | 2026-11-01 |
| Mega: `sfe backup --all` (per-file, listing each game folder at the menu) | **Works:** 00.SAV (save state), bram.srm, cd-bram.brm stored; all three SHA-256 identical to the card-reader backup (`~/Desktop/MDBackup`). `recent.dat` has the N8's 512-byte-slot format, and its date changes when a game starts (22:30:50 after `sfe launch`) | 2026-11-01 |
| Krikzz's Mega `edio` sample built with m68k-elf-gcc 16.2 (Homebrew), uploaded and started by `sfe put` + `sfe launch` | First build: magenta screen (exception: GCC 16 placed `ed_halt_app_end` before `ed_halt_app`, the RAM copy overran; found in a machine68k trace). With `-fno-reorder-functions -fno-toplevel-reorder`: **runs**; device id and rtc work; its exit returns to the menu | 2026-11-01 |
| Genesis Sync Forever app v0 (`md/`, edio text console) with `sfe serve` on the Mega | **Works end to end:** HELLO, the opening save backup (3 files), plan, all four modes, sync (4 files to `/syncforever-test/sync` in ~2 s), check again ("Nothing to do"). Same structure as the NES app; plain edio font for now | 2026-11-01 |
| Mega app self-update via the N8 relaunch (RESTART, then pings from 1.5 s) | **No:** the app's exit (`ed_cmd_reboot`) dropped USB briefly; the box's 5-s save check opened files while the menu booted (`FR_INVALID_OBJECT`) and its pings met the booting menu: **Error:2A, an unread one, Error:40** on the TV; gave up after 15 s | 2026-11-02 |
| Mega: a silent listener (port held, nothing sent) while the app starts and exits to the menu | No bytes and no USB drop: the menu sends no ready signal after an app's exit | 2026-11-02 |
| Mega: edlink's reset to menu (`HOST_RST` 0x29 soft, `MEM_WR` 256 zeros at `ADDR_CFG`, release) | **Works:** the menu sent `r` 0.35 s after the release; ping OK; `*i`/`*s` launched our app at 0.68 s | 2026-11-02 |
| Mega USB ID after firmware v26.0925 | Now Krikzz's `38DF:0018` (was ST's `0483:5740` on v24.1213); `transport.is_everdrive` accepts both | 2026-11-02 |
| Mega app self-update with the reset relaunch (`sfe serve`: outdated app opened) | **Works, no errors on the TV:** replaced at HELLO, RESTART, reset into the menu, `r`, launched the new copy 1.99 s after RESTART; it connected at once (N8: 3.8 s) | 2026-11-02 |
| Multi-cart `sfe serve` (no port: a thread per EverDrive) with the Mega | Found and identified it, log tagged `[Mega PRO 3C85]`; per-console `[genesis]` folders used; SIGINT and SIGTERM stop it cleanly | 2026-11-02 |
| v7 app heartbeat, first try | **Bug:** the start watch's recent-games save check ran 5 s after the app opened; an app PING landed inside a stat reply: `0x53 UNKNOWN_53` (`'S'`). Fixed: while the app has been heard in the last 3 s the box only answers it (`AppServer.app_active`) | 2026-11-02 |
| v7 app, box stopped while it idles on the plan | "Status: Disconnected" in red within ~3 s, plan cleared; the app kept sending HELLOs: the Mega's `CMD_USB_WR` doesn't block with nobody reading | 2026-11-02 |
| v7 app, box started again | Reconnected within 1 s of the box starting (next HELLO), saves checked, fresh plan, no errors | 2026-11-02 |
| Firmware line on the Mega (v26.0925, built 2026-09-24) | Index fetched (honest UA; certifi CA bundle after `CERTIFICATE_VERIFY_FAILED` on python.org's macOS Python), current: **green**. The first build also removed an `.efu` the owner had put at the root; now only files the box delivered (`delivered.json`) | 2026-11-02 |
| Firmware line on the N8 PRO (built 2025-09-30; newest v25.1123) | `edn8-pro-fw-v25.1123.efu` (3.6 MB) copied to the root and verified in 12 s ("Copying firmware"), recorded as delivered; version shown, but **white, not red** | 2026-11-02 |
| N8: why the version wasn't red (diagnostic builds) | Tiles right (the cart's file byte-identical to the emulated image), attributes right (a blue start colour showed), CPU side right (on screen: `fw_col` 26, state 02, `scr_pal[2]` 26). **Palette entry 2 never changed on the PPU, entries 14-15 did:** early palette bytes in a vblank are lost. Pushing the palette twice per vblank: **red**. nesemu now models it (`PAL_SETTLE`) | 2026-11-02 |
| N8 PRO firmware v25.1123 installed from the menu (box stopped) | USB ID now `38DF:0017` KRIKZZ-IT (was `0483:5740`); SYS_INF unchanged: built 2025-09-30 16:36, mcu 1.03, same layout. So the build date can't tell the N8's firmware: the box now compares `EDN8/nesos.nes` with the .efu's CRC32 | 2026-11-02 |
| N8 after the v25.1123 install, app opened (box running the menu-file check) | **Green**: the card's `EDN8/nesos.nes` matched the .efu's CRC32. The owner had deleted the delivered .efu; the box found nothing to remove and cleared its record. Update relaunch with "Updating..." then "Restarting...": 6.97 s, no errors | 2026-11-02 |
| N8 cable unplugged and replugged, app open | Quick replug: "USB blinked; port open again", same serial, session kept. Slower one (over CART_GONE, 3 s): "cart gone", a new session 1 s later; the open app got PONG unknown, said hello, fresh plan | 2026-11-02 |
| N8 relaunch after an app update, 1.5 s + 0.3 s | **Error:40 once** (2.81 s); the box then opened save files while the menu showed the error (start watch). Now 3.0 s + 2.0 s: four relaunches at 5.5-6.0 s; one showed a brief Error:02 that recovered itself | 2026-11-02 |
| USB hub with N8, Mega and FXPAK, one console on at a time, Pi 4 | The console that's on connects. The carts whose consoles are off still show a port and time out on the box's first questions, every few seconds. The box now logs that once per port and keeps trying | 2026-11-04 |
| N8 `F_FMV` (0xD7) rename over USB, at the menu: `sfe mv` with mode 0, 2, 6 (`FA_WRITE \| FA_CREATE_NEW`) and 0x0A | **No:** every one answers status 0 and the file stays where it was (`ls`). So the N8 never moves files in a sync; it copies | 2026-11-04 |
| Mega `F_FMV` (as the N8, plus full paths and a folder as destination) and `F_FCP` copy into another folder, at the menu | **Move: no** (success, nothing moved). **Copy: yes.** So the Mega's sync moves are an on-cart copy plus a delete | 2026-11-04 |
| Mega plan with one big file to verify ("Verifying 1/1"), app open | **`error: read: 0x53`** 38 s in: the app's 30 s wait ran out, and its HELLO landed in the host's read. The box now sends a STATUS every 10 s during long plans | 2026-11-04 |
| Mega `F_FCRC` at the menu on a 31,493,280-byte file (`sfe crc`), one call | **Works:** the same CRC as zlib's CRC-32 of the library's copy, 7.69 s (4001 KiB/s), against ~700 KiB/s to read it over USB. So a plan's first look at a file checksums it on the cart | 2026-11-04 |
| Truncated write wedges until power cycle | No result recorded (`bench-test.md` step 6). The rule still rests on NESCD, and we keep to it | |

## Benchmarks

| Date | Op | Size | KiB/s | Notes |
|---|---|---|---|---|
| 2026-10-31 | read | 8 KiB (`bram.srm`) | 619 | single `get`, includes stat/open/close |
| 2026-10-31 | read | 768 KiB (Kirby's Adventure) | 777 | near the full-speed USB ceiling |
| 2026-10-31 | write / read | 32 KiB | 438–444 / 635–646 | `sfe bench` ×3, all readbacks match |
| 2026-10-31 | write / read | 256 KiB | 451–453 / 761–764 | |
| 2026-10-31 | write / read | 1 MiB | 453–457 / 776–779 | |
| 2026-10-31 | write / read | 4 MiB | 444–454 / 780–783 | Writes ~454 KiB/s (1 KiB acked blocks), reads ~780 KiB/s |
