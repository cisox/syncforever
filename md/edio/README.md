# edio (vendored)

Krikzz's Mega EverDrive I/O library and sample runtime, from
[krikzz/mega-ed-pub](https://github.com/krikzz/mega-ed-pub) `edio/` at commit
`7064f454af3222256a92ee5ebcfd11e83500f1f7` (2026-10-28), MIT (`LICENSE`).

Our changes:
- `sega.s`: header names and copyright say Sync Forever.

Build notes (`../Makefile`): link `sega.o` first (vectors and header at 0), and compile
`everdrive.c` with `-fno-reorder-functions -fno-toplevel-reorder`: `ed_init` copies
`ed_halt_app` to RAM using `ed_halt_app_end - ed_halt_app` as its length, and GCC 16
otherwise places them the other way round (a -2 length that wipes RAM; see ../../CLAUDE.md).
`font.bin` is edio's own font (MIT), not the Mega menu's.
