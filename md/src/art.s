/* The generated art tiles (tools/art.py -> build/art.bin), linked into the ROM. */
	.text
	.balign 2
	.global art_tiles
	.global art_tiles_end
art_tiles:
	.incbin "art.bin"
art_tiles_end:
	.balign 2
