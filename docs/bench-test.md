# Milestone A bench test: laptop ↔ N8 PRO

Historical plan: most of this was run from 2026-10-31 to 2026-11-02, and the results are in
`docs/test-results.md`. It is written for the N8 PRO. It still works as the read, write and
benchmark check for a new PRO-family cart. The X7 and FXPAK PRO were checked through
`n64-plan.md` and `snes-plan.md` instead (`sfe --x7`, `sfe --fxpak`).

Run from `host/`. Record results in `docs/test-results.md` as you go.

## 0. Before connecting

- [x] Back up the whole microSD card with a card reader to a folder, e.g. `~/everdrive-sd-backup/`. (Ours: `~/Desktop/NESBak`.)
- [ ] Put the card back. Laptop on battery if possible (see `safety.md`).
- [ ] `uv sync`

## 1. Detect (read-only)

**Done 2026-10-31.** The cart showed `0483:5740` then, and `38DF:0017` since firmware v25.1123.

NES on, at the N8 PRO menu, cable connected.

```sh
uv run sfe ports          # expect one /dev/cu.usbmodem… with 38DF:0017
uv run sfe info --raw     # record firmware os/hw/boot and the raw bytes
```

If `ports` is empty: check the cable carries data (most "charging" micro-USB cables
don't) and look in System Information → USB for a device with vendor 0x38DF.

## 2. Read-only checks

**Done 2026-10-31:** all 232 root files matched the card-reader backup.

```sh
uv run sfe ls /
uv run sfe ls /<your ROM folder>
uv run sfe stat /<some small file>
uv run sfe get /<some small file> /tmp/x.bin    # prints sha256
shasum -a 256 ~/everdrive-sd-backup/<same file>     # must match
```

Or all at once: `uv run pytest --hardware --sd-backup ~/everdrive-sd-backup -s -k "not upload"`

## 3. Verify the write gate (sim assumptions)

**Done 2026-10-31:** STATUS turned out to be the last command's result, so the gate now uses the menu ping (`sfe info` → "at menu"). See `test-results.md`. Original steps:

These steps check what the simulator assumes. **Do not write anything during this step.**

- [ ] At the menu: `sfe info`. Is status `0x00`?
- [ ] Start a game, then run `sfe info`. Record the status (NESCD says `0x04`/`0x05`).
      Does `info` work at all mid-game? Does the game glitch?
- [ ] Back at the menu, `sfe stat /does-not-exist`, then `sfe info`. Does status
      still read `0x00`, or does it report the last error (`0x04`)? If it reports the last
      error, the gate needs a different signal; flag it.

## 4. Writes (disposable files only, under /syncforever-test)

**Done 2026-10-31**, including the full hardware suite.

```sh
uv run sfe mkdir /syncforever-test
echo hello > /tmp/hello.txt
uv run sfe put /tmp/hello.txt /syncforever-test/hello.txt   # uploads + verifies readback
uv run sfe put /tmp/hello.txt /syncforever-test/hello.txt   # must fail: FR_EXIST
uv run sfe rm /syncforever-test/hello.txt
uv run pytest --hardware -s                               # full hardware suite
```

## 5. Benchmark

**Done 2026-10-31:** three runs, all readbacks match (`test-results.md`, Benchmarks).

```sh
uv run sfe bench --log ../docs/results/bench.jsonl     # 32K, 256K, 1M, 4M
```

Run it 3 times. Note the throughput and whether any run had `match False`.

## 6. Reconnect and recovery

- [x] Unplug USB at the menu, replug, run `sfe info`. Does it recover without a power cycle?
      (Yes, 2026-10-31.)
- [ ] Run `ls` with a long-running listing and unplug mid-command. Does reconnect work? (reads only)
      (No result recorded.)
- [ ] **Only after everything above passes:** unplug USB mid-`put` of a 4 MB file to
      `/syncforever-test`. sfe should report "wedged". Replug, run `sfe info`, and
      record what happens. Then power-cycle the NES and check the SD card
      (`ls /syncforever-test`). This confirms the wedge model in `usb-protocol.md`.
      (No result recorded.)

## 7. Bonus: Mega EverDrive PRO identify (read-only)

**Done 2026-11-01; the steps below are as first written.** The Mega is now supported
(`carts.py`), so `sfe` no longer fails with "not an N8 PRO". It shows `38DF:0018` since
firmware v26.0925.

Same cable, Genesis at the Mega PRO menu. `sfe` only speaks N8 today, so expect
`connect` to fail with "not an N8 PRO". The point is to capture the evidence for the
Mega adapter (`cartridge-compatibility.md`):

- [x] `uv run sfe ports`: record the PID. (`0483:5740`, "Mega EverDrive", 2026-11-01)
- [x] `uv run sfe info`: the error reads `not an N8 PRO (protocol 0x.., device 0x..,
      status reply ....)`. Record all three. Expect protocol `0x05`. A status reply of
      `a500` confirms the big-endian `[0xA5, code]` order; `00a5` would mean N8 order.
      (Got protocol `0x05`, device `0x18`, `a500`; SYS_INF parses big-endian: `cartridge-compatibility.md`.)

## 8. Save backup and game-exit detection

Done 2026-11-01. The box doesn't ping a menu that may be in use, so the ping test below is history (`box.py`).

- [x] At the menu: `uv run sfe backup --store ~/sfe-saves --all`, then again: second run all
      "unchanged". Compare one file with `~/Desktop/NESBak/EDN8/gamedata/<game>/bram.srm`.
- [x] Launch a game with `sfe launch`, exit with the in-game reset, `sfe get
      /EDN8/sysdata/recent.bin`: is the game it started in slot 0? (`backup --recent`
      depends on it.)
- [x] During a game: what does `MEM_RD` of `ADDR_CFG` (42 bytes) return, and does the game
      glitch? Expect byte 32 = the game's mapper, 255 at the menu. (A quiet "game → menu" probe.)
- [x] `uv run sfe clock` (read), then `sfe clock --set` at the menu, then `sfe clock` again:
      off by ≤ 1 s? Write a file (`sfe put`) and `sfe stat` it: is its mtime now right?
- [x] Only with a game whose save doesn't matter: send ~1100 menu pings during play (fills
      the 2 KB FIFO), then exit to the menu. Does the link still answer? Does the menu?
- [x] `sfe serve --library … --saves ~/sfe-saves`: the startup log reports the save check.
      Launch a battery-save game with `sfe launch`, save in game, in-game reset: within ~3 s
      the log says "backed up", and the new version matches the cart's `bram.srm`.

## 9. Start watch (`box.StartWatch`)

- [x] `sfe serve` running: use the menu hard for a few minutes (start games, resets, folders).
      No errors. Each start logs "the menu started a game or the app", then "recent games: …".
- [x] Save in Zelda, in-game reset, start DuckTales: Zelda's new save is backed up ~5 s later.
- [ ] Fresh cart folder without the app: the app appears after the first game start.

## Success criterion

Repeatable round-trip file transfers without corruption or removing the SD card. Met on the
N8 PRO, 2026-10-31.
