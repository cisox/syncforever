# Running a Sync Forever box

A Raspberry Pi (ours is a Pi 4; any model will do) on Raspberry Pi OS Bookworm or newer, or any
Linux with systemd and Python 3.11+. There's no prebuilt Pi image: install the OS, then the
command below. The cart's USB port plugs into the Pi. More than one
cart can share the Pi through a USB hub: the box serves them all at once, each with its
own console's folders, and tells them apart by what each cart reports. A cart whose console is
off is logged once as not answering; the box connects as soon as you turn that console on.

## Install

One command, the same to install and to update:

```sh
curl -fsSL https://raw.githubusercontent.com/cisox/syncforever/main/deploy/get.sh | sudo bash
```

It downloads the newest package (the GitHub release "latest") and runs its installer. Or from
a package you built yourself (`deploy/package.sh`; no internet needed on the Pi):

```sh
scp dist/syncforever-*.tar.gz pi@<the Pi>:     # on the computer
tar xzf syncforever-*.tar.gz                    # on the Pi
sudo syncforever/deploy/install.sh
```

This installs the software into `/opt/syncforever`, puts `sfe` on the path, creates a
`syncforever` system user (in `dialout`, for the cart's serial port), writes
`/etc/syncforever/config.toml` and starts the `syncforever` service, which starts on every
boot. On a fresh install it then asks for your folders and user (`sfe setup`, below). To update: run the same `curl … | sudo bash` again (or unpack a newer package and run
`sudo syncforever/deploy/install.sh`). It always replaces the installed software and prints the build it installed (`syncforever-0.1.0-<commit>`). Your config is kept, and so are the service's `User=`/`Group=` if you changed them (e.g. to the user that owns your library files).

## Set up

```sh
sudo sfe setup
```

asks for each console's ROM library and save folder (NES, Genesis, N64, SNES; leave a console
empty if you don't have it), the default sync mode, and which user the box runs as. That user
must be able to read the libraries and write the save folders: with a network share it's usually
the user that owns its files. It writes `/etc/syncforever/config.toml` (every setting is
explained there; `config.example.toml` here is the same file, and you can edit it by hand), puts
the user in a systemd drop-in, restarts the service and checks everything. Run it again any
time: your current answers are the defaults.

Save states (Genesis and SNES) go with the saves unless you give them a folder of their own:
setup asks, or set `states` in that console's section of the config. It's laid out the same
as the save folder; `states = ""` doesn't back them up. Copies already in the save folder stay
where they are.

```sh
sudo sfe doctor            # what's wrong, and how to fix it
journalctl -u syncforever -f
```

`sfe doctor` checks the service and, as the box's user, each library (how many ROMs; links
that point outside it, which sync skips; files it can't read) and save folder (can it write
there), and lists the carts on USB.

Each log line about a cart is tagged with it, e.g. `[N8 PRO 4E86]`, `[Mega PRO 1A2B]` or
`[X7 BVGM]` (the cart type and the end of its serial number).

The library and save folders can be on a disk attached to the Pi, or a network share mounted on it (an `/etc/fstab`
SMB or NFS entry). You set that up yourself: the box and `sfe setup` only use the paths you
give them. The `syncforever` user must be able to read the library and write the
save folder; for SMB, mount with `uid=syncforever,gid=syncforever`. If a folder is missing
(share not mounted), the box says so in the log and skips that part; it never creates it.

## What it does

The X7 and the FXPAK PRO differ; they have their own sections below. When an N8 PRO or Mega
EverDrive connects, the box sets its clock and says in the log if the Sync Forever app isn't
on the card yet. It leaves the menu alone while you use it (it only checks one file's date).

Put the **Sync Forever** app on the cart once: with the console at its menu, run
`sudo sfe install` (default `/Sync Forever.nes`, `.md` on a Mega EverDrive; the NES app uses the
font from the cart's own menu). It shows in the menu once the menu reloads the folder. After
that the box updates it whenever you open it and the box software has a newer one.

A few seconds after you start any game, the box backs up the saves of the recently played games
that changed (so a game's save reaches your save folder when you start the next game or open the app).
During a game it only reads: it never writes to the cart.

Opening the Sync Forever app backs up every changed save first. It then copies new or changed
ROMs from the library to the cart: it previews the changes (Add only, Update, Mirror, Mirror strict), syncs on A
and goes back to the menu. Turning the console off and on runs the steps above again.

If the library changes while the app shows a plan, the box plans again and the app shows the new
one. On the Genesis and the SNES the sync takes the whole screen, with progress bars and time
estimates, and B stops after the file being copied.

## The EverDrive-64 X7

The X7's menu offers no file access over USB, so on the N64 everything happens in the Sync
Forever app (`/Sync Forever.z64`): the box only listens, and serves the app when you open it.
Put the app on the card once, from a computer with the X7 at its menu:

```sh
sfe --x7 install
```

(`--x7` boots the app over USB, and `sfe` writes it to the card through the app.) After that, open Sync
Forever from the menu: it backs up every save in `ED64/gamedata`, previews the sync and copies
on A, with a progress bar for each ROM. The box keeps the app's file up to date when it
connects; a new version runs the next time you open it. The X7 has no way back to the menu
from a ROM: turn the N64 off and on to leave the app.

## The FXPAK PRO (SNES)

The FXPAK's controller serves its SD card at any time, so on the SNES the box does more on its
own: when the cart connects (at the menu) it installs or updates the app (`/Sync Forever.sfc`) and backs up
every save (battery saves, save states, cheats); it backs up again a few seconds after the game
changes, and every 10 minutes. Open Sync Forever from the menu for the same preview and sync as
on the other consoles; B goes back to the menu, and an app the box has just updated restarts by
itself. The app's firmware line shows the cart's firmware, green when it's the newest sd2snes
release. Firmware updates stay manual: copy a release's `sd2snes` folder over the card's from a
computer; the cart flashes itself at the next power-on.

Files already on a card that the box has no record of (say, a card you copied yourself) are
checked once against the library if they have the same name and size. `first_look` under a
console's section sets how:

- `"auto"` (the default): check them all, unless that would take over 10 minutes. Then a spot
  check of about 2 minutes samples them, and if every sampled file is the same, the rest are taken
  as the library's. If one differs, every file is checked. The log says which happened.
- `"check"`: always check every one.
- `"trust"`: never check. A file with the same name and size but other contents stays on the card
  until its library copy changes. On the FXPAK, whose listing has no sizes, this goes by name
  alone; `"auto"` there always checks.

The check is once per file: the Mega checksums files itself (about 4 MB/s); the others read them
over USB (the X7 about 0.5 MB/s).

With `msu = true` under `[snes]`, MSU-1 games sync with their `.msu` and `-N.pcm` files (and
under `[genesis]`, MSU-MD games with their `.cue` and audio `.bin`). It's off by default: MSU
games are then left out of the sync, and those on the card left alone. Keep them unzipped in
the library, a folder per game. Audio packs are big and the cart takes about 250 KiB/s, so a
500 MB pack takes around 35 minutes; the app shows the size of a big file while it copies.
`sfe --fxpak` writes (put, rm, bench, sync) only at the cart's menu: in a game, the cart's
autosave can meet an upload and hang its USB until a power cycle.

## Firmware

With `firmware = true` under `[box]` (the config the installer writes has it on), the box checks
once a day for new firmware. It never flashes anything.

On the N8 PRO and the Mega EverDrive, it copies a newer `.efu` to the card's root while the app is
open, and the app shows the version in red. Install it from the cart's menu (Krikzz's
`efu-install.txt`); once the cart runs it the version turns green and the box removes the file it
copied. Whether the box can safely keep running during the install is untested: the first N8
install was done with the box stopped.

The X7 and the FXPAK PRO are report only: the app shows the version, green when it's the newest,
red when there's a newer one. Their updates are copied over from a computer.

## Using `sfe` by hand

The service holds the carts' ports, and a cart talks to one program at a time. Run commands
with sudo and `sfe` pauses the service for them, then starts it again: for example
`sudo sfe install` (put the app on the cart now, at its menu), `sudo sfe info`,
`sudo sfe ls /`, `sudo sfe --fxpak sync`. Without sudo it tells you the service has the cart.
`sfe serve` runs the box in the foreground with the same config (stop the service first).
