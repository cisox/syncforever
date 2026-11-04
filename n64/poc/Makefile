all: sfe-x7-poc.z64
.PHONY: all

BUILD_DIR = build
include $(N64_INST)/include/n64.mk

OBJS = $(BUILD_DIR)/poc.o

sfe-x7-poc.z64: N64_ROM_TITLE = "SFE X7 POC"
$(BUILD_DIR)/sfe-x7-poc.elf: $(OBJS)

clean:
	rm -rf $(BUILD_DIR) *.z64
.PHONY: clean

-include $(wildcard $(BUILD_DIR)/*.d)
