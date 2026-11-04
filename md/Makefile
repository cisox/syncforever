# Sync Forever for the Mega EverDrive PRO (Genesis). Needs m68k-elf-gcc (brew install m68k-elf-gcc).
#   make                 build build/sync-forever.md
#   make install         copy to the cart (/syncforever-test) and start it, via sfe
#   make package         the copy the box ships: ../host/src/syncforever/data/sync-forever.md

CC := m68k-elf-gcc
OBJCOPY := m68k-elf-objcopy
CART_PATH ?= /syncforever-test/Sync Forever.md

B := build
OUT := $(B)/sync-forever.md
# -fno-reorder-functions/-fno-toplevel-reorder: edio's ed_init copies ed_halt_app to RAM by
# the distance to ed_halt_app_end; GCC 16 otherwise reorders them (CLAUDE.md).
CFLAGS := -m68000 -Wall -O2 -fomit-frame-pointer -ffreestanding -fno-reorder-functions \
	-fno-toplevel-reorder -I $(CURDIR)/src -I $(CURDIR)/edio -I $(abspath $(B))
LIBGCC := $(shell $(CC) -m68000 -print-libgcc-file-name)

# sega.o first: the vectors and the cartridge header live at address 0.
OBJ := $(B)/sega.o $(B)/main.o $(B)/link.o $(B)/art.o $(B)/everdrive.o $(B)/sys.o $(B)/str.o
PYTHON ?= python3

all: $(OUT)

$(OUT): $(OBJ) edio/rom.ld
	$(CC) -m68000 -T edio/rom.ld -nostdlib $(OBJ) $(LIBGCC) -o $(B)/sync-forever.elf
	$(OBJCOPY) --pad-to 0x20000 -O binary $(B)/sync-forever.elf $@

$(B)/sega.o: edio/sega.s edio/font.bin | $(B)
	cd edio && $(CC) $(CFLAGS) -c sega.s -o $(abspath $@)

$(B)/art.bin $(B)/art.h: tools/art.py edio/font.bin | $(B)
	$(PYTHON) -I tools/art.py edio/font.bin $(B)/art.bin $(B)/art.h

$(B)/art.o: src/art.s $(B)/art.bin
	cd $(B) && $(CC) $(CFLAGS) -c $(CURDIR)/src/art.s -o $(abspath $@)

$(B)/%.o: src/%.c src/main.h src/link.h $(B)/art.h edio/everdrive.h edio/sys.h | $(B)
	$(CC) $(CFLAGS) -c $< -o $@

$(B)/%.o: edio/%.c edio/everdrive.h edio/sys.h | $(B)
	$(CC) $(CFLAGS) -c $< -o $@

$(B):
	mkdir -p $@

PACKAGED := ../host/src/syncforever/data/sync-forever.md

package: $(OUT)
	cp $(OUT) $(PACKAGED)

install: $(OUT)
	cd ../host && uv run sfe put --overwrite "$(CURDIR)/$(OUT)" "$(CART_PATH)" && uv run sfe launch "$(CART_PATH)"

clean:
	rm -rf $(B)

.PHONY: all install package clean
