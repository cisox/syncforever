"""The Sync Forever app's 8 KB CHR bank, and patching it into a built ROM.

Tiles $00-$7F: glyphs 0-127 (ASCII positions), drawn in colour 3 on colour 0.
Tiles $80-$FF: the same glyphs inverted for the cursor bar: colour 3 on colour 1.
Art in the slots text never uses (control codes $00-$1F, $7F and their inverted copies):
the header stripes, the double-size title and the version digits in colour 2, drawn
from the same glyphs.
app/art.h holds the matching slot numbers. Pattern table $1000: unused (zeros).

The glyphs come from the N8 PRO menu (EDN8/nesos.nes, CHR ROM, first pattern table), so
the app reads like the menu. That artwork is Krikzz's and is NOT committed or shipped: the
dev build reads the user's own copy, and the box patches it in from the cart it installs to
(apps.py). Without one, the glyphs come from edio's MIT font.chr.

Standard library only: the NES build runs this file as a script.
usage: python3 -I chr.py MENU_ROM FALLBACK_CHR OUT
"""

import sys
from pathlib import Path

TILE = 16
GLYPHS = 128 * TILE
CHR_SIZE = 0x2000


def menu_glyphs(rom: bytes) -> bytes:
    """Glyphs 0-127 from the menu ROM (EDN8/nesos.nes)."""
    if rom[:4] != b"NES\x1a":
        raise ValueError("not an iNES file")
    prg, chr_banks, flags6 = rom[4], rom[5], rom[6]
    if chr_banks == 0:
        raise ValueError("menu ROM has no CHR ROM")
    off = 16 + (512 if flags6 & 0x04 else 0) + prg * 16384
    glyphs = rom[off : off + GLYPHS]
    if len(glyphs) < GLYPHS:
        raise ValueError("menu ROM is too short")
    return glyphs


def mask(tile: bytes) -> list[int]:
    """Per row: which pixels are ink (any nonzero colour)."""
    return [tile[y] | tile[y + 8] for y in range(8)]


# Art slots (app/art.h must match).
STRIPES = 0xFF  # header row 3, under the title
TITLE = "SYNC FOREVER"
ACCENT = "EVER"  # the end of the title in colour 1 (the stripes' red): forEVER, EVERdrive
TITLE_SLOTS = list(range(0x02, 0x20)) + list(range(0x82, 0x94))  # 4 per letter, spaces skipped
# The firmware line's version ("v26.0925") in colour 2, which the app sets green or red.
VERSION_CHARS = "0123456789v."
VERSION_SLOTS = list(range(0x94, 0xA0))


def planar(pixels) -> bytes:
    """8x8 colour indices (0-3) -> the 16-byte NES tile."""
    lo = bytes(sum(((p & 1) << (7 - x)) for x, p in enumerate(row)) for row in pixels)
    hi = bytes(sum(((p >> 1) << (7 - x)) for x, p in enumerate(row)) for row in pixels)
    return lo + hi


def stripes(rows) -> bytes:
    """A tile of full-width stripes: rows maps tile row -> colour."""
    return planar([[rows.get(y, 0)] * 8 for y in range(8)])


def art(glyphs: bytes) -> dict[int, bytes]:
    """Slot -> tile. Under the title three lines: colour 2 (dark red) at header pixel rows
    26-27, colour 1 (red) at 29 and 31. The title is drawn 2x on header rows 1-2, in colour 3
    and its ACCENT in colour 1."""
    tiles = {STRIPES: stripes({2: 2, 3: 2, 5: 1, 7: 1})}
    slots = iter(TITLE_SLOTS)
    letters = TITLE.replace(" ", "")
    accent_from = len(letters) - len(ACCENT)
    for i, ch in enumerate(letters):
        ink = 1 if i >= accent_from else 3
        m = mask(glyphs[ord(ch) * TILE : (ord(ch) + 1) * TILE])
        big = [[ink if m[y // 2] & (0x80 >> (x // 2)) else 0 for x in range(16)] for y in range(16)]
        for ty in (0, 8):
            for tx in (0, 8):
                tiles[next(slots)] = planar([line[tx : tx + 8] for line in big[ty : ty + 8]])
    for ch, slot in zip(VERSION_CHARS, VERSION_SLOTS, strict=True):
        m = mask(glyphs[ord(ch) * TILE : (ord(ch) + 1) * TILE])
        tiles[slot] = planar([[2 if m[y] & (0x80 >> x) else 0 for x in range(8)] for y in range(8)])
    return tiles


def build(glyphs: bytes) -> bytes:
    normal, inverted = bytearray(), bytearray()
    for t in range(128):
        m = mask(glyphs[t * TILE : (t + 1) * TILE])
        normal += bytes(m) + bytes(m)  # colour 3 ink, colour 0 paper
        inverted += b"\xff" * 8 + bytes(m)  # colour 3 ink, colour 1 paper
    out = bytearray(normal + inverted)
    for slot, tile in art(glyphs).items():
        out[slot * TILE : (slot + 1) * TILE] = tile
    return bytes(out) + bytes(CHR_SIZE - len(out))


def patch_rom(app: bytes, glyphs: bytes) -> bytes:
    """The app ROM (one 8 KB CHR bank, last in the file) with its font rebuilt from glyphs."""
    if app[:4] != b"NES\x1a" or app[5] != 1 or len(app) != 16 + app[4] * 16384 + CHR_SIZE:
        raise ValueError("not a Sync Forever app ROM (expected one CHR bank at the end)")
    return app[:-CHR_SIZE] + build(glyphs)


def main() -> int:
    menu, fallback, out = (Path(a) for a in sys.argv[1:4])
    try:
        glyphs, source = menu_glyphs(menu.read_bytes()), f"menu font from {menu}"
    except (OSError, ValueError) as e:
        glyphs, source = fallback.read_bytes()[:GLYPHS], f"edio font (menu ROM unusable: {e})"
    if len(glyphs) < GLYPHS:
        print("chr: glyph source too short", file=sys.stderr)
        return 1
    out.write_bytes(build(glyphs))
    print(f"chr: {source}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
