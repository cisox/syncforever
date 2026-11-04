"""Byte transports: the real USB CDC port, or anything with the same four methods."""

import time
from typing import Protocol

from .protocol import ST_VCP_PID, ST_VCP_VID, USB_VID, LinkError


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...

    # pyserial semantics: returns up to n bytes, fewer (or none) on timeout.
    def read(self, n: int) -> bytes: ...

    def reset_input(self) -> None: ...

    def close(self) -> None: ...


def read_exact(transport, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = transport.read(n - len(buf))
        if not chunk:
            raise LinkError(f"read timeout ({len(buf)}/{n} bytes)")
        buf += chunk
    return bytes(buf)


def is_everdrive(port):
    # Firmware built 2025-09-30 enumerates as ST's stock VCP ID with product
    # "EverDrive N8"; NESCD/NESPC saw Krikzz's own VID on their firmware.
    if port.vid == USB_VID:
        return True
    return (port.vid, port.pid) == (ST_VCP_VID, ST_VCP_PID) and "everdrive" in (port.product or "").lower()


def find_ports():
    from serial.tools import list_ports

    return [p for p in list_ports.comports() if is_everdrive(p)]


class PortBusy(LinkError):
    """Another process has the cart's port (usually the box service)."""


def open_port(device, **kw):
    """pyserial's Serial, opened exclusively (flock): the box service and a manual `sfe` must
    never both talk to one cart (their bytes would mix: on the PRO carts that can wedge a write
    until a power cycle). PortBusy if another process holds it."""
    import serial

    try:
        return serial.Serial(device, exclusive=True, **kw)
    except serial.SerialException as e:
        if "lock" in str(e).lower():
            raise PortBusy(f"{device} is in use by another program (the box service?)") from e
        raise


class SerialTransport:
    # The baud rate is ignored by the CDC device; the reference hosts use 921600.
    def __init__(self, device, timeout=2.0):
        self.device = device
        self._port = open_port(device, baudrate=921600, timeout=timeout, write_timeout=10)

    @classmethod
    def auto(cls, timeout=2.0):
        ports = find_ports()
        if not ports:
            raise LinkError("no EverDrive found on USB")
        if len(ports) > 1:
            names = ", ".join(p.device for p in ports)
            raise LinkError(f"several EverDrives found ({names}); pass --port")
        return cls(ports[0].device, timeout)

    @property
    def timeout(self):
        return self._port.timeout

    @timeout.setter
    def timeout(self, value):
        self._port.timeout = value

    def write(self, data):
        self._port.write(data)

    def read(self, n):
        return self._port.read(n)

    def reset_input(self):
        time.sleep(0.05)
        self._port.reset_input_buffer()

    def close(self):
        self._port.close()

    def reopen(self, wait=10.0):
        """Open the port again after the cart dropped off USB (a reset, power off, cable, or the
        ~0.2 s re-enumeration at a menu <-> program switch). Raises LinkError if it isn't back
        within `wait` seconds; wait=None waits for good."""
        import os

        import serial

        timeout = self._port.timeout
        try:
            self._port.close()
        except (OSError, serial.SerialException):
            pass
        end = time.monotonic() + (wait or 0)
        while True:
            try:
                if os.path.exists(self.device):
                    self._port = open_port(self.device, baudrate=921600, timeout=timeout, write_timeout=10)
                    return
            except (OSError, serial.SerialException, PortBusy):
                pass
            if wait is not None and time.monotonic() > end:
                raise LinkError(f"{self.device} didn't come back")
            time.sleep(0.1)
