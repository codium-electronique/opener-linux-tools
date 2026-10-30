# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""SPI transport: Spinel frames in the Spinel SPI framing (spinel/spi_frame.py).

The host is the SPI master. The device raises IRQ (active high) while it has
a frame pending and holds BUSY (active high) while it is not ready for a
transaction. A link task clocks a transaction when IRQ is active or a frame
is waiting to be sent. Transfers are short, so they run inline in the event
loop; the IRQ edges are read with add_reader.
"""

import asyncio
from collections import deque

from ..spinel.hdlc import DEFAULT_MAX_FRAME
from ..spinel.spi_frame import HEADER_LEN, build_header, parse_header
from .base import Transport, TransportClosed
from .spi_io import LinuxSpiIo, SpiIo

DEFAULT_SPEED = 8_000_000
# Payload bytes clocked when no longer frame is known: most device frames fit
# in one transaction.
DEFAULT_CLOCK = 128
BUSY_POLL_S = 0.0002


class SpiTransport(Transport):
    """errors counts the invalid device headers, the device frames too long
    to receive and the host frames the device refused."""

    def __init__(
        self,
        path: str,
        irq: tuple[str, int],
        busy: tuple[str, int],
        speed: int = DEFAULT_SPEED,
        max_frame: int = DEFAULT_MAX_FRAME,
        io: SpiIo | None = None,
    ):
        self.path = path
        self.irq = irq
        self.busy = busy
        self.speed = speed
        self.max_frame = max_frame
        if io is None:
            io = LinuxSpiIo(path, irq, busy, speed)
        self._io = io
        self.errors = 0
        self._frames = asyncio.Queue()
        # Frames to send, with the future their send() waits on.
        self._pending: deque[tuple[bytes, asyncio.Future]] = deque()
        self._wake = asyncio.Event()
        self._task = None
        self._closed = True

    async def open(self) -> None:
        self._io.open()
        self._closed = False
        loop = asyncio.get_running_loop()
        fd = self._io.irq_fd()
        loop.add_reader(fd, self._on_irq)
        self._task = asyncio.ensure_future(self._run())

    async def close(self) -> None:
        task = self._task
        self._shut_down()
        if task is not None:
            task.cancel()
            await asyncio.wait({task})

    async def send(self, frame: bytes) -> None:
        """Return once the device has accepted frame."""
        if self._closed:
            raise TransportClosed(self.path)
        loop = asyncio.get_running_loop()
        accepted = loop.create_future()
        self._pending.append((frame, accepted))
        self._wake.set()
        await accepted

    async def recv(self) -> bytes:
        if self._closed and self._frames.empty():
            raise TransportClosed(self.path)
        frame = await self._frames.get()
        if frame is None:
            # Leave the end marker for the next caller.
            self._frames.put_nowait(None)
            raise TransportClosed(self.path)
        return frame

    def _on_irq(self) -> None:
        try:
            self._io.read_irq_edges()
        except OSError:
            self._shut_down()
            return
        self._wake.set()

    async def _run(self) -> None:
        try:
            while True:
                # The device updates IRQ before it releases BUSY: the level
                # is current only once BUSY is inactive.
                await self._wait_not_busy()
                await self._wait_work()
                await self._wait_not_busy()
                await self._exchange()
                # Let the other tasks run between transactions, also when
                # the device keeps refusing a frame.
                await asyncio.sleep(0)
        except OSError:
            self._shut_down()

    async def _wait_not_busy(self) -> None:
        while True:
            busy = self._io.busy_active()
            if not busy:
                return
            await asyncio.sleep(BUSY_POLL_S)

    async def _wait_work(self) -> None:
        """Wait until the device has a frame pending or one waits to be sent."""
        while True:
            self._wake.clear()
            if self._pending:
                return
            irq = self._io.irq_active()
            if irq:
                return
            await self._wake.wait()

    async def _exchange(self) -> None:
        """Send the first pending frame and receive the device's, with a
        second transaction if the device frame was longer than clocked."""
        frame = b""
        if self._pending:
            frame, _ = self._pending[0]
        rx = self._transfer(frame, DEFAULT_CLOCK)
        header = parse_header(rx)
        if header is None:
            self.errors += 1
            return
        if frame:
            self._sent(header.recv_len)
        data_len = header.data_len
        if data_len == 0:
            return
        if data_len > self.max_frame:
            self.errors += 1
            return
        end = HEADER_LEN + data_len
        if end > len(rx):
            await self._wait_not_busy()
            rx = self._transfer(b"", data_len)
            header = parse_header(rx)
            if header is None:
                self.errors += 1
                return
            data_len = header.data_len
            end = HEADER_LEN + data_len
            if data_len == 0 or end > len(rx):
                # Another frame than announced: the next IRQ brings it.
                return
        self._frames.put_nowait(rx[HEADER_LEN:end])

    def _transfer(self, frame: bytes, clock: int) -> bytes:
        """Clock a header, frame and padding up to clock payload bytes."""
        payload = max(len(frame), clock)
        header = build_header(payload, len(frame))
        padding = bytes(payload - len(frame))
        return self._io.transfer(header + frame + padding)

    def _sent(self, device_recv_len: int) -> None:
        frame, accepted = self._pending[0]
        if device_recv_len < len(frame):
            # No room on the device: sent again in a later transaction.
            self.errors += 1
            return
        self._pending.popleft()
        if not accepted.done():
            accepted.set_result(None)

    def _shut_down(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = asyncio.get_running_loop()
        try:
            loop.remove_reader(self._io.irq_fd())
        finally:
            self._io.close()
        while self._pending:
            _, accepted = self._pending.popleft()
            if not accepted.done():
                accepted.set_exception(TransportClosed(self.path))
        self._frames.put_nowait(None)
