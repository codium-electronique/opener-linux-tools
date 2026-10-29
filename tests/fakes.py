# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Test doubles."""

import asyncio

from opener_tools.transport import Transport, TransportClosed


class FakeTransport(Transport):
    """In-memory transport: frames sent by the client land in sent, frames
    passed to inject() are what recv() returns."""

    def __init__(self):
        self.sent: asyncio.Queue[bytes] = asyncio.Queue()
        self._received: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False

    def inject(self, frame: bytes) -> None:
        self._received.put_nowait(frame)

    async def open(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self._received.put_nowait(None)

    async def send(self, frame: bytes) -> None:
        if self.closed:
            raise TransportClosed("fake transport closed")
        self.sent.put_nowait(frame)

    async def recv(self) -> bytes:
        frame = await self._received.get()
        if frame is None:
            self._received.put_nowait(None)
            raise TransportClosed("fake transport closed")
        return frame
