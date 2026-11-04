# Sync Forever app ↔ box protocol (v7)

How the Sync Forever apps (`nes/`, `md/`, `snes/`, `n64/`) and the box
(`host/src/syncforever/appserver.py`, run by `sfe serve`; `PROTO_VERSION` there, `LINK_VERSION` or
`UI_VERSION` in the apps) talk while an app runs.

## Transport

On the N8 and Mega PRO:

| Direction | Mechanism | Notes |
|---|---|---|
| app → box | `CMD_USB_WR` (edio `ed_cmd_usb_wr`): the MCU forwards the bytes to USB | arrive unsolicited on the box's serial port |
| box → app | host `CMD_MEM_WR` to `ADDR_FIFO` (`N8Pro.fifo_write`, like the menu ping) | the app reads `REG_FIFO_DATA`; FIFO holds 2 KB |

The SNES app has no FIFO: two rings in cart memory carry the same bytes (`fxapp.py`,
`snes/src/link_io.c`, `snes-plan.md`). The X7 app's travel in UNFLoader frames of type `0x55`, beside
its file service (`x7.py`, `n64/src/link.c`, `x7-protocol.md`).

Both directions carry the same frames. The app starts an exchange; the box answers each request
with one reply frame (after STATUS frames for progress), except that it can send a PLAN unasked
when the library changes (below). Bytes outside a frame are skipped by both sides.

```
'S' 'F' type:u8 len:u16le payload[len]
str = len:u8 + ASCII bytes (the box maps anything outside 0x20-0x7E to '?')
```

## Messages

| Type | Name | Payload | Reply |
|---|---|---|---|
| `0x01` | HELLO | version:u8 (=7) | WELCOME; RESTART if the box just replaced the app (Updates, below); ERROR on a version mismatch |
| `0x06` | SYNC_PLAN | mode:u8 (0 add, 1 update, 2 mirror, 3 strict; `0xFF` = box default) | STATUS* then PLAN, or ERROR |
| `0x07` | SYNC_RUN | mode:u8 (must match the last PLAN), number:u8 (the PLAN's; ERROR "Plan changed" if the box has planned again) | STATUS* then SYNC_DONE, or ERROR |
| `0x0A` | PING | – | PONG. The app's heartbeat while it waits for input (v7+) |
| `0x0B` | EXIT | – | none: the box resets the console into the menu (FXPAK MENU_RESET). Sent only by the SNES app, whose ROM can't start the menu itself; boxes that can't do it log and ignore it |
| `0x0C` | STOP | – | during a sync (B on every app): the box ends it before the next file and sends SYNC_DONE "Stopped: n/N done"; after a sync it's ignored. The app sends it as a STATUS arrives, never while a file copies (on the SNES that would write save RAM mid-upload); the box listens briefly after each STATUS |
| `0x81` | WELCOME | status:str | |
| `0x83` | STATUS | text:str, detail:str (optional), expect:u16, left:u32 (both optional) | progress, shown on the status line; `detail` (up to 63 chars: the file being copied, read or removed) goes on the line under it, its middle cut out to fit ("International...n) (Rev 1).nes"). Apps read `detail` only if the frame has more after `text`, and blank that line on any other message or a STATUS without one. During a sync, `expect:u16` and `left:u32` follow: this copy's seconds (0 for a removal) and everything left's (this one included), at the speeds the sync has seen. The NES, SNES and Genesis sync screens count them (the X7 app counts `left` beside its copy panel) ("This file: 0:30 / 2:28", "(slow)" past twice that; "Time left: about 42 min") with a bar for the file and one for the whole sync, since the box sends nothing while a file copies; they show `detail` over 3 rows instead. The Genesis app also waits twice `expect` plus 30 s before "Disconnected". The NES and X7 apps skip both numbers |
| `0x84` | RESTART | text:str | the box replaced the app's file: show the text, exit; the box starts the new copy. The SNES app just shows it: the box starts the new copy ~1 s later itself (FXPAK BOOT). The X7 app never gets it (Updates, below) |
| `0x85` | ERROR | text:str, detail:str (optional) | shown on the status line in red, `detail` under it: the file a failed sync was on ("Failed: 3/12 done", "Zelda.nes"), or what went wrong ("Couldn't finish"); messages written for the screen ("Library offline") have none |
| `0x86` | PLAN | mode:u8, name:str, add:u16 (moves included), replace:u16, remove:u16, saves:str, fw:str, fw_state:u8, msu:u8, number:u8, note:str | the preview the user confirms; `saves` = this session's save backup ("Backed up (3)", "up to date (6)", "backup off", …); `fw` = the newest firmware ("v26.0925") and `fw_state` 0 no line, 1 the cart runs it (green), 2 it's waiting at the card root (red; on the X7 and FXPAK: a newer version exists, installed from a computer), 3 the newest isn't known (X7 and FXPAK; plain on the X7, red on the SNES app, which has only the two colours). Apps read `fw` only if the frame has more after `saves`. `msu`: the console's `msu` setting, 0 the cart plays no MSU games, 1 off, 2 on; the SNES and Genesis apps show it beside the mode ("MSU off"), the NES and X7 apps skip it. A PLAN can also come unasked while the app shows one: the box looks at the library every 10 s (`appserver.LIBRARY_CHECK`), and when it changed sends STATUS "Library changed..." (so the app waits instead of missing PONGs), plans again from the same cart listing, and sends the PLAN; every app redraws with "Library changed" |
| `0x87` | SYNC_DONE | text:str | the apps keep the text up and send SYNC_PLAN again at once, so the counts show what's left |
| `0x89` | COPY | n:u16, total:u16, stage:u8, done:u32, size:u32, name:str | the X7 app's copy panel: copy n of total, stage 0 copying / 1 checking the readback, bytes done of size; at most 4 a second. Only apps that show it get it (`AppServer.copy_panel`) |
| `0x8A` | PONG | known:u8 | 1 = this box WELCOMEd the app; 0 = it didn't (the box restarted): the app says hello again |

- The status line shows 22-23 characters after "Status:" on the SNES and NES (more on the
  N64 and Genesis); the box keeps its texts short.
- The app sends HELLO every ~2 s until WELCOME arrives; with no reply for ~5 s it shows
  "Disconnected" and keeps trying.
- While it waits for input it sends PING every second. No reply for ~3 s, or a PONG with
  `known` 0, and it clears the plan (it may be stale) and says hello again: "Disconnected",
  or "Connecting..." for a restarted box. Turning the box off and on, or restarting
  `sfe serve`, with the app open brings it back to a fresh plan by itself.
- It gives up on a plan or a sync after ~30 s without a STATUS ("Disconnected", then hello); on
  the X7 a COPY or a file request also counts. The SNES app waits 2 min for a plan and never
  gives up on a running sync: saying hello would write save RAM during an upload. The Genesis
  app's wait during a copy comes from `expect` (above).

## Session

The app opens on the plan for the box's default mode: it sends SYNC_PLAN(`0xFF`), the box
plans with `romsync.make_plan` (STATUS "Verifying N/TOTAL", detail the file's name, for each cart file with nothing on record: read to hash, or checksummed on the cart on the Mega; "Spot check N/TOTAL" before trusting a big card by name and size (`first_look`); and "Library N/TOTAL" while it checksums library files it has no checksum kept for)
and answers PLAN. Left/Right ask for the plan of the previous/next mode; A sends SYNC_RUN,
which carries out exactly the plan shown (the box refuses a run without a matching plan);
progress comes as STATUS "Copying n of N", "Replacing n of N" or "Removing n of N", detail the
file's name. B exits to the menu, which reloads the root and shows the new ROMs (the SNES app
asks the box to, with EXIT; the X7 can't leave its app: power off). During a sync, B on every app
stops after the current file (STOP). Modes and rules: `romsync.py` docstring.

Slow steps of a plan say what they're doing at most every 2 s (`romsync.STATUS_EVERY`), and a plan that finishes sooner says nothing, so changing mode doesn't flicker: "Scanning library" (detail "12 ROMs"), "Reading the card" ("3 folders"), "Checking library" and "Looking for moves" ("5 of 20"), "Library n/N" (the file's name). After a sync the box repeats SYNC_DONE's text as a STATUS before the PLAN, because the app leaves its status alone at that PLAN.

**The full-screen view.** A STATUS during a sync, or any STATUS with `left` or `expect`, puts the app's sync screen up: the name (`detail`), this file's clock if `expect` isn't 0, the time left and the whole bar (the N64 shows the detail line, the time left and the bar). So slow counted steps before a plan use it too: "Verifying n/N", "Spot check n/N", "Library n/N" and "Saves n of N", with `left` worked out from how long the done part took (`romsync.Eta`). The next STATUS without those ends it. A read-back of at least 2 s gets "Checking n of N" with its own `expect` (`run_plan`), and the box listens for STOP right after it, as after every STATUS. The apps must only send STOP during a sync.

**After a sync.** SYNC_DONE says "Sync done in 14 min". The PLAN after it carries `note`, shown under the status: what it did ("Added 12  Replaced 2  Removed 3"). The X7's first plan after an update says "Updated: power off and on to use". `note` is sent once; the next PLAN clears the line. All of these are additions older apps skip, so the version stays 7.

Under the plan the app shows the save backup the box made as it opened ("Saves: …"); there
is nothing to back up on request, since no game can run while the app does. After a sync,
A checks again.

**Updates.** At the first HELLO of a connection the box checks the app's file (the app runs,
so the menu doesn't) and replaces it if it differs. The app then gets RESTART("Updating..."),
exits, and the box starts the new copy. On the N8, through the menu with the hardware-tuned
timing (`appserver.MENU_FIRST_PING`/`MENU_SETTLE`: first ping 4.5 s after RESTART, 2.25 s after
the app exits; launch 2.0 s after the menu answers). On the Mega, it resets into the menu 2.5 s
after RESTART (`RESET_AFTER`) and launches when the menu answers. On the SNES, FXPAK BOOT, 1 s
after RESTART. The X7 can't restart: WELCOME says "Updated for next start", the running copy
carries on, and the new file runs next time.

The box writes only while the app runs (the design's rule), except installing the app itself
(`apps.py`). Saves: when the app opens, the box backs up every save before the first plan
(STATUS "Backing up saves", then "Saves n of N" with the game's name, then "Checking firmware" and "Scanning library"). On the N8 and Mega, a few seconds after the menu starts any game (a
new date on the recent list, which the box stats every 2 s), it also backs up the recent games'
changed saves (`box.StartWatch`). The FXPAK backs up on its own schedule (`snes-plan.md`); the X7
only when the app opens. The box must not ping a menu that's in use: the menu reads the cart's replies from the same FIFO, so a ping is read as a reply
(Error:40, or Error:2A for the `*`). A watch that pinged every 3 s did exactly that.
