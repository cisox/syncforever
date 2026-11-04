# Sync Forever (SNES app, for the FXPAK PRO). Needs cc65 (brew install cc65) and python3.
#   make           build build/sync-forever.sfc
#   make install   copy it to the cart's /syncforever-test, via sfe (start it from the menu)
#   make package   the copy the box ships and installs: ../host/src/syncforever/data/

CC65 ?= cc65
CA65 ?= ca65
LD65 ?= ld65
PYTHON ?= python3

CART_PATH ?= /syncforever-test/Sync Forever.sfc

B := build
OUT := $(B)/sync-forever.sfc
C_SRC := ../app/app.c src/plat.c src/screen.c ../app/link.c src/link_io.c ../app/clock.c
S_SRC := src/crt0.s src/sram.s src/font.s
OBJ := $(addprefix $(B)/,$(notdir $(C_SRC:.c=.o) $(S_SRC:.s=.o)))
CFLAGS := -O -t none --cpu 6502 -I src -I ../app
AFLAGS := --cpu 65816 --bin-include-dir $(B)

vpath %.c src ../app
vpath %.s src

all: $(OUT)

$(OUT): $(OBJ) snes.cfg tools/checksum.py
	$(LD65) -C snes.cfg -o $@ -m $(B)/map.txt $(OBJ) none.lib
	$(PYTHON) -I tools/checksum.py $@

# chr.py's font and art (edio's MIT glyphs: never the menu's), as SNES tiles.
CHR_PY := ../host/src/syncforever/chr.py
$(B)/font.chr: $(CHR_PY) ../nes/edio/font.chr | $(B)
	$(PYTHON) -I $(CHR_PY) /nonexistent ../nes/edio/font.chr $@
$(B)/font.bin: $(B)/font.chr tools/snesfont.py
	$(PYTHON) -I tools/snesfont.py $< $@
$(B)/font.o: $(B)/font.bin

$(B)/%.s: %.c src/main.h src/screen.h src/link_io.h src/plat_config.h src/nespal.h $(wildcard ../app/*.h) | $(B)
	$(CC65) $(CFLAGS) -o $@ $<

$(B)/%.o: $(B)/%.s
	$(CA65) $(AFLAGS) -o $@ $<

$(B)/%.o: %.s | $(B)
	$(CA65) $(AFLAGS) -o $@ $<

$(B):
	mkdir -p $@

install: $(OUT)
	cd ../host && uv run sfe --fxpak put --overwrite "$(CURDIR)/$(OUT)" "$(CART_PATH)"

PACKAGED := ../host/src/syncforever/data/sync-forever.sfc
package: $(OUT)
	cp $(OUT) $(PACKAGED)

# The link code in cc65's 6502 simulator (tests/link_test.c); host/tests/test_snes_app.py runs it.
test: | $(B)
	mkdir -p $(B)/test
	sed 's/^#pragma zpsym.*//' src/main.h > $(B)/test/main.h  # sram_val: a plain variable here
	cp src/link_io.c src/link_io.h src/screen.h ../app/link.c ../app/link.h ../app/clock.c ../app/clock.h $(B)/test/  # beside that main.h: "" includes look there first
	cp tests/link_test.c tests/clock_test.c $(B)/test/  # cl65 leaves its .o files beside the sources
	cl65 -t sim6502 -O -I $(B)/test -o $(B)/test/link_test $(B)/test/link_test.c $(B)/test/link.c $(B)/test/link_io.c
	sim65 $(B)/test/link_test
	cl65 -t sim6502 -O -I $(B)/test -o $(B)/test/clock_test $(B)/test/clock_test.c $(B)/test/clock.c
	sim65 $(B)/test/clock_test

clean:
	rm -rf $(B)

.PHONY: all install package test clean
