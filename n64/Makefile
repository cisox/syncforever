# Sync Forever for the EverDrive-64 X7. `make` builds in Docker (the sfe-libdragon:preview image,
# see docs/n64-plan.md); `make package` copies the ROM to where the box ships it.
ROM = sync-forever.z64
DATA = ../host/src/syncforever/data

ifeq ($(N64_INST),)

IMAGE = sfe-libdragon:preview
all clean $(ROM):
	docker run --rm --platform linux/amd64 -v "$(CURDIR):/n64" -w /n64 $(IMAGE) make $@

else

BUILD_DIR = build
include $(N64_INST)/include/n64.mk
N64_CFLAGS += -Wall -Wextra -Wno-unused-parameter

OBJS = $(BUILD_DIR)/main.o $(BUILD_DIR)/link.o $(BUILD_DIR)/files.o $(BUILD_DIR)/ui.o

all: $(ROM)
$(ROM): N64_ROM_TITLE = "Sync Forever"
$(BUILD_DIR)/sync-forever.elf: $(OBJS)

$(BUILD_DIR)/%.o: src/%.c
	@mkdir -p $(dir $@)
	@echo "    [CC] $<"
	$(CC) -c $(CFLAGS) -o $@ $<

clean:
	rm -rf $(BUILD_DIR) $(ROM)

-include $(wildcard $(BUILD_DIR)/*.d)

endif

package: all  # always asks: `$(ROM)` alone is "up to date" here whatever the sources say
	cp $(ROM) $(DATA)/sync-forever.z64

.PHONY: all clean package
