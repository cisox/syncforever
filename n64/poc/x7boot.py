"""Boot the X7 proof-of-concept ROM over USB and run its tests (phase 1, docs/n64-plan.md).

Usage: uv run --project host python n64/poc/x7boot.py PORT ROM [boot|tests]...
The cart must be at the X7 menu for "boot".
"""

import struct
import sys
import time

import serial

TEXT, RAW = 0x01, 0x02
CRC_AREA = 0x100000 + 4096


def cmd(port, letter, addr=0, size=0, arg=0):
    port.write(b"cmd" + letter.encode() + struct.pack(">III", addr, size // 512, arg))


def boot(port, rom):
    port.reset_input_buffer()
    cmd(port, "t")
    reply = port.read(16)
    if reply[:4] != b"cmdr":
        sys.exit(f"the menu didn't answer t: {reply.hex()}")
    rom += bytes(-len(rom) % 512)
    if len(rom) < CRC_AREA:
        cmd(port, "c", 0x10000000, CRC_AREA)
    cmd(port, "W", 0x10000000, len(rom))
    t0 = time.monotonic()
    for i in range(0, len(rom), 0x8000):
        port.write(rom[i : i + 0x8000])
    port.flush()
    print(f"uploaded {len(rom)} bytes in {time.monotonic() - t0:.2f} s")
    time.sleep(0.5)
    cmd(port, "s")


def send(port, kind, data):
    pad = bytes(len(data) % 2)
    port.write(b"DMA@" + struct.pack(">I", kind << 24 | len(data)) + data + pad + b"CMPH")
    port.flush()


def receive(port, timeout=10.0):
    """One frame from the ROM: (type, data), or None after timeout."""
    end = time.monotonic() + timeout
    window = b""
    while window != b"DMA@":
        b = port.read(1)
        if not b:
            if time.monotonic() > end:
                return None
            continue
        window = (window + b)[-4:]
    (header,) = struct.unpack(">I", port.read(4))
    size = header & 0xFFFFFF
    data = b""
    while len(data) < size:
        chunk = port.read(size - len(data))
        if not chunk and time.monotonic() > end:
            raise TimeoutError(f"frame cut short at {len(data)} of {size}")
        data += chunk
    tail = port.read(4)
    if tail != b"CMPH":
        raise ValueError(f"bad frame end {tail.hex()}")
    port.read(size % 2)  # the frame is padded to an even length after CMPH
    return header >> 24, data


def text(port, timeout=30.0):
    frame = receive(port, timeout)
    if frame is None:
        return None
    kind, data = frame
    return data.decode(errors="replace") if kind == TEXT else f"<{kind}: {len(data)} bytes>"


def frames(port, total=256 * 1024):
    """Cart -> box in growing frame sizes; stops at the first that doesn't arrive whole."""
    for frame in (500, 504, 4096, 32768, 131072):
        send(port, TEXT, f"send {total} {frame}".encode())
        t0, got = time.monotonic(), 0
        try:
            while got < total:
                f = receive(port, 15)
                if f is None:
                    print(f"frame size {frame}: stalled after {got} bytes")
                    return
                got += len(f[1])
        except (TimeoutError, ValueError) as e:
            print(f"frame size {frame}: {e} after {got} bytes")
            return
        secs = time.monotonic() - t0
        print(f"frame size {frame}: {got} bytes in {secs:.2f} s = {got / secs / 1024:.0f} KB/s;", text(port))


def tests(port):
    send(port, TEXT, b"ping")
    print(text(port))
    # cart -> box
    size = 1024 * 1024
    send(port, TEXT, f"send {size}".encode())
    t0, got = time.monotonic(), 0
    while got < size:
        kind, data = receive(port, 30)
        got += len(data)
    secs = time.monotonic() - t0
    print(f"cart->box {got} bytes in {secs:.2f} s = {got / secs / 1024:.0f} KB/s;", text(port))
    # box -> cart
    blob = bytes(range(256)) * (size // 256)
    t0 = time.monotonic()
    send(port, RAW, blob)
    reply = text(port, 60)
    secs = time.monotonic() - t0
    print(f"box->cart {size} bytes in {secs:.2f} s = {size / secs / 1024:.0f} KB/s; expected sum {sum(blob)};", reply)
    for path in ("sd:/", "sd:"):
        send(port, TEXT, f"ls {path}".encode())
        listing = text(port, 30) or ""
        lines = listing.splitlines()
        print("\n".join(lines[:4] + lines[-1:]))
    send(port, TEXT, "sdread sd:/Banjo-Tooie (USA).z64".encode())
    print(text(port, 60))
    send(port, TEXT, f"sdwrite {4 * 1024 * 1024}".encode())
    print(text(port, 120))


def main():
    path, rom_path, *steps = sys.argv[1:]
    port = serial.Serial(path, timeout=0.5)
    time.sleep(0.3)
    if "boot" in steps:
        boot(port, open(rom_path, "rb").read())
        for _ in range(3):  # HELLO, then the listing
            msg = text(port, 15)
            if msg is None:
                break
            print(msg)
    if "list" in steps:
        send(port, TEXT, b"list")
        print(text(port))
    if "frames" in steps:
        frames(port)
    if "tests" in steps:
        tests(port)
    port.close()


if __name__ == "__main__":
    main()
