"""NES CHR tiles -> SNES 2bpp tiles: the first 256 tiles of an NES pattern table (8 bytes of
plane 0, then 8 of plane 1) as SNES BG tiles (plane 0 and plane 1 interleaved by row).
Colour numbers stay the same, so chr.py's art and glyphs keep their meaning, except the
stripes under the title (chr.STRIPES), redrawn with 2-pixel gaps: a CRT ran the NES tile's
1-pixel gaps together on the SNES (2026-11-03).

usage: python3 -I snesfont.py IN.chr OUT.bin
"""

import sys
from pathlib import Path

TILES = 256
STRIPES = 0xFF
STRIPE_ROWS = {0: 2, 1: 2, 4: 1, 7: 1}  # row -> colour: the thick dark line, then two thin ones


def convert(nes: bytes) -> bytes:
    out = bytearray()
    for t in range(TILES):
        tile = nes[t * 16 : t * 16 + 16]
        for row in range(8):
            if t == STRIPES:
                c = STRIPE_ROWS.get(row, 0)
                out += bytes((0xFF if c & 1 else 0, 0xFF if c & 2 else 0))
            else:
                out += bytes((tile[row], tile[8 + row]))
    return bytes(out)


if __name__ == "__main__":
    src, dst = sys.argv[1:3]
    Path(dst).write_bytes(convert(Path(src).read_bytes()))
