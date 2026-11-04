# Sync Forever (NES app). Needs cc65 (brew install cc65) and python3.
#   make                 build build/sync-forever.nes
#   make MENU_ROM=...    take the font from this copy of EDN8/nesos.nes
#   make install         copy to the cart (/syncforever-test) and start it, via sfe
#   make package         rebuild the copy the box ships and installs (edio font; the box
#                        patches in each cart's menu font): ../host/src/syncforever/data/

CC65 ?= cc65
CA65 ?= ca65
LD65 ?= ld65
PYTHON ?= python3

# The menu font is Krikzz's artwork: read from your own SD backup, never committed.
MENU_ROM ?= $(HOME)/Desktop/NESBak/EDN8/nesos.nes
CART_PATH ?= /syncforever-test/Sync Forever.nes

B := build
OUT := $(B)/sync-forever.nes
C_SRC := ../app/app.c src/plat.c src/screen.c ../app/link.c ../app/clock.c edio/everdrive.c
S_SRC := src/crt0.s src/fast.s edio/everdrive_asm.s
OBJ := $(addprefix $(B)/,$(notdir $(C_SRC:.c=.o) $(S_SRC:.s=.o)))
CFLAGS := -O -t nes -I src -I ../app -I edio
AFLAGS := -t nes --bin-include-dir $(B)

vpath %.c src ../app edio
vpath %.s src edio

all: $(OUT)

$(OUT): $(OBJ) nes.cfg
	$(LD65) -C nes.cfg -o $@ -m $(B)/map.txt $(OBJ) nes.lib

CHR_PY := ../host/src/syncforever/chr.py
PACKAGED := ../host/src/syncforever/data/sync-forever.nes

$(B)/font.chr: $(CHR_PY) edio/font.chr | $(B)
	$(PYTHON) -I $(CHR_PY) "$(MENU_ROM)" edio/font.chr $@

$(B)/crt0.o: $(B)/font.chr

$(B)/%.s: %.c src/main.h src/screen.h src/link_io.h src/plat_config.h $(wildcard ../app/*.h) | $(B)
	$(CC65) $(CFLAGS) -o $@ $<

$(B)/%.o: $(B)/%.s
	$(CA65) $(AFLAGS) -o $@ $<

$(B)/%.o: %.s | $(B)
	$(CA65) $(AFLAGS) -o $@ $<

$(B):
	mkdir -p $@

install: $(OUT)
	cd ../host && uv run sfe put --overwrite "$(CURDIR)/$(OUT)" "$(CART_PATH)" && uv run sfe launch "$(CART_PATH)"

# Never with the menu font: Krikzz's artwork must not be committed or shipped.
package:
	$(MAKE) B=build/package MENU_ROM=/nonexistent
	cp build/package/sync-forever.nes $(PACKAGED)

FCEUX ?= fceux

# Opens an FCEUX window briefly, plays tests/smoke.lua, writes build/shots/*.png.
test: $(OUT)
	rm -rf $(B)/shots && mkdir -p $(B)/shots
	SHOTS="$(CURDIR)/$(B)/shots" $(FCEUX) --loadlua tests/smoke.lua $(OUT)
	ls $(B)/shots

clean:
	rm -rf $(B)

.PHONY: all install package test clean
.SECONDARY:
