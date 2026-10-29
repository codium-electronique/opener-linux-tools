# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""UART transport: Spinel frames in HDLC-lite framing.

The port is read from the event loop (add_reader on the non-blocking file
descriptor), so there is no reader thread.
"""

import asyncio
import os

import serial

from ..spinel.hdlc import DEFAULT_MAX_FRAME, HdlcDecoder, hdlc_encode
from .base import Transport, TransportClosed

DEFAULT_BAUD = 1_000_000

# Bytes read from the port at once.
READ_SIZE = 4096


class UartTransport(Transport):
    def __init__(self, path: str, baud: int = DEFAULT_BAUD, max_frame: int = DEFAULT_MAX_FRAME):
        self.path = path
        self.baud = baud
        self._decoder = HdlcDecoder(max_frame)
        self._serial = None
        self._frames = asyncio.Queue()
        # A frame is written in several parts when the port buffer fills:
        # one sender at a time, or frames interleave on the wire.
        self._send_lock = asyncio.Lock()
        self._closed = True

    @property
    def errors(self) -> int:
        return self._decoder.errors

    async def open(self) -> None:
        self._serial = serial.Serial(self.path, self.baud, timeout=0)
        fd = self._serial.fileno()
        os.set_blocking(fd, False)
        self._closed = False
        loop = asyncio.get_running_loop()
        loop.add_reader(fd, self._on_readable)

    async def close(self) -> None:
        self._shut_down()
        if self._serial is not None:
            self._serial.close()
            self._serial = None

    async def send(self, frame: bytes) -> None:
        if self._closed:
            raise TransportClosed(self.path)
        fd = self._serial.fileno()
        data = memoryview(hdlc_encode(frame))
        async with self._send_lock:
            while data:
                try:
                    written = os.write(fd, data)
                except BlockingIOError:
                    await self._writable(fd)
                    continue
                data = data[written:]

    async def recv(self) -> bytes:
        if self._closed and self._frames.empty():
            raise TransportClosed(self.path)
        frame = await self._frames.get()
        if frame is None:
            # Leave the end marker for the next caller.
            self._frames.put_nowait(None)
            raise TransportClosed(self.path)
        return frame

    def _on_readable(self) -> None:
        fd = self._serial.fileno()
        try:
            data = os.read(fd, READ_SIZE)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            # Device unplugged, or the pty peer closed.
            self._shut_down()
            return
        frames = self._decoder.feed(data)
        for frame in frames:
            self._frames.put_nowait(frame)

    async def _writable(self, fd: int) -> None:
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        loop.add_writer(fd, ready.set_result, None)
        try:
            await ready
        finally:
            loop.remove_writer(fd)

    def _shut_down(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = asyncio.get_running_loop()
        loop.remove_reader(self._serial.fileno())
        self._frames.put_nowait(None)
