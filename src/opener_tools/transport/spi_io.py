# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Hardware boundary of the SPI transport: the SPI bus and the IRQ and BUSY
lines. LinuxSpiIo uses spidev and libgpiod 2.x (the spi extra); tests use a
fake device instead."""

import re
from typing import Protocol

MISSING_EXTRA = "the SPI transport needs the spi extra: pip install 'opener-tools[spi]'"

CONSUMER = "nrp-spi"

_SPIDEV_PATH = re.compile(r"/dev/spidev(\d+)\.(\d+)")


class SpiIo(Protocol):
    def open(self) -> None: ...

    def close(self) -> None: ...

    def transfer(self, data: bytes) -> bytes:
        """One full-duplex transaction, chip select held for all of it."""

    def irq_fd(self) -> int:
        """File descriptor readable when IRQ edges are pending."""

    def read_irq_edges(self) -> None:
        """Drain the pending IRQ edges."""

    def irq_active(self) -> bool: ...

    def busy_active(self) -> bool: ...


class LinuxSpiIo(SpiIo):
    def __init__(self, path: str, irq: tuple[str, int], busy: tuple[str, int], speed: int):
        match = _SPIDEV_PATH.fullmatch(path)
        if match is None:
            raise ValueError(f"not a spidev path: {path}")
        self.path = path
        self.irq = irq
        self.busy = busy
        self.speed = speed
        self._bus = int(match.group(1))
        self._device = int(match.group(2))
        self._spi = None
        self._irq_request = None
        self._busy_request = None
        self._active = None

    def open(self) -> None:
        try:
            import gpiod
            import spidev
            from gpiod.line import Direction, Edge, Value
        except ImportError:
            raise RuntimeError(MISSING_EXTRA) from None
        self._active = Value.ACTIVE
        spi = spidev.SpiDev()
        spi.open(self._bus, self._device)
        self._spi = spi
        try:
            spi.mode = 0
            spi.max_speed_hz = self.speed
            irq_settings = gpiod.LineSettings(direction=Direction.INPUT, edge_detection=Edge.RISING)
            busy_settings = gpiod.LineSettings(direction=Direction.INPUT)
            self._irq_request = self._request(gpiod, self.irq, irq_settings)
            self._busy_request = self._request(gpiod, self.busy, busy_settings)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if self._irq_request is not None:
            self._irq_request.release()
            self._irq_request = None
        if self._busy_request is not None:
            self._busy_request.release()
            self._busy_request = None
        if self._spi is not None:
            self._spi.close()
            self._spi = None

    def transfer(self, data: bytes) -> bytes:
        # xfer2 is one ioctl: chip select stays active between the bytes.
        values = list(data)
        received = self._spi.xfer2(values)
        return bytes(received)

    def irq_fd(self) -> int:
        return self._irq_request.fd

    def read_irq_edges(self) -> None:
        self._irq_request.read_edge_events()

    def irq_active(self) -> bool:
        line = self.irq[1]
        value = self._irq_request.get_value(line)
        return value == self._active

    def busy_active(self) -> bool:
        line = self.busy[1]
        value = self._busy_request.get_value(line)
        return value == self._active

    @staticmethod
    def _request(gpiod, chip_line: tuple[str, int], settings):
        chip, line = chip_line
        return gpiod.request_lines(f"/dev/{chip}", consumer=CONSUMER, config={line: settings})
