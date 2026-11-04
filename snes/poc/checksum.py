"""Fill in a LoROM image's header checksum (and its complement) in place."""

import sys
from pathlib import Path

HEADER = 0x7FC0  # LoROM: $00:FFC0
path = Path(sys.argv[1])
rom = bytearray(path.read_bytes())
rom[HEADER + 0x1C : HEADER + 0x20] = b"\xff\xff\x00\x00"  # complement + checksum = $FFFF while summing
total = sum(rom) & 0xFFFF
rom[HEADER + 0x1C : HEADER + 0x20] = ((total ^ 0xFFFF).to_bytes(2, "little")) + total.to_bytes(2, "little")
path.write_bytes(rom)
print(f"{path}: {len(rom)} bytes, checksum {total:04X}")
