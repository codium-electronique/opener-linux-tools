# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""TCP transport to an nrp-hub control port: frames as [len u16 LE][frame]."""

import asyncio

from .base import Transport, TransportClosed

LENGTH_SIZE = 2
MAX_FRAME = 0xFFFF


class TcpTransport(Transport):
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._reader = None
        self._writer = None

    async def open(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

    async def close(self) -> None:
        if self._writer is None:
            return
        writer = self._writer
        self._writer = None
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass

    async def send(self, frame: bytes) -> None:
        if self._writer is None:
            raise TransportClosed(f"{self.host}:{self.port}")
        if len(frame) > MAX_FRAME:
            raise ValueError(f"frame of {len(frame)} bytes exceeds {MAX_FRAME}")
        header = len(frame).to_bytes(LENGTH_SIZE, "little")
        self._writer.write(header + frame)
        await self._writer.drain()

    async def recv(self) -> bytes:
        if self._writer is None:
            raise TransportClosed(f"{self.host}:{self.port}")
        try:
            header = await self._reader.readexactly(LENGTH_SIZE)
            length = int.from_bytes(header, "little")
            return await self._reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            raise TransportClosed(f"{self.host}:{self.port}") from exc
