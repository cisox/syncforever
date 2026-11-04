; chr.py's font and art as SNES 2bpp tiles (tools/snesfont.py, at build time).
.export _font
.segment "RODATA"
_font:
    .incbin "font.bin"
