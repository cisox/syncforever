# Sync Forever (NES app)

The TV side of Sync Forever (`../docs/design.md`, section 3): preview and run a sync of the
box's ROM library to the cart, then go back to the menu. A plain mapper-0 `.nes` the N8 PRO
menu runs like any game; `sudo sfe install` puts it on the cart and the box keeps it up to date.

```sh
brew install cc65
make                 # -> build/sync-forever.nes
make install         # sfe put to /syncforever-test/ and sfe launch it (menu must be up)
make package         # the copy the box ships (edio font)
cd ../host && uv run sfe serve --library ~/roms   # be the box
cd ../host && uv run pytest tests/test_app.py   # app + box + fake cart, no NES
```

Talks to the box with `docs/app-protocol.md` (`../app/link.c`). Without an
EverDrive (an emulator) it says so and does nothing else.

**Font:** the build copies glyphs 0-127 from the menu's CHR ROM in your own copy of
`EDN8/nesos.nes` (default `~/Desktop/NESBak/EDN8/nesos.nes`, override with `MENU_ROM=`), so
the app reads like the menu. That artwork is Krikzz's and is not committed. Without it the
build falls back to edio's MIT font. Tiles `$80-$FF` are generated inverted copies for the
cursor bar (`char | HL`); the header art (stripes, double-size title) sits in the slots text
never uses (`../app/art.h`). Colours: charcoal and red (`../app/app.c` `make_pal`). Rows 0-1 stay blank for overscan.

Layout: `../app/` what the NES and SNES apps share (`app.c` UI states, `link.c` protocol, `clock.c` the sync clock), `src/` the NES's own part (`plat.c` start and exit, `link_io.h` the FIFO, `screen.c` + `fast.s` drawing and the exit), `edio/` Krikzz's MIT library (vendored), `../host/src/syncforever/chr.py` the font
builder.
