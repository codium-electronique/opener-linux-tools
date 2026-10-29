# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import asyncio

import pytest

from opener_tools.transport import TcpTransport, TransportClosed

TIMEOUT = 1.0


async def echo(reader, writer):
    data = await reader.read(4096)
    while data:
        writer.write(data)
        await writer.drain()
        data = await reader.read(4096)
    writer.close()


async def hang_up(reader, writer):
    writer.close()


async def start_peer(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


async def test_round_trip_max_frame():
    server, port = await start_peer(echo)
    frame = bytes(i & 0xFF for i in range(1300))
    async with server, TcpTransport("127.0.0.1", port) as transport:
        await transport.send(frame)
        assert await asyncio.wait_for(transport.recv(), TIMEOUT) == frame
        assert transport.errors == 0


async def test_wire_format_is_length_prefixed():
    received = asyncio.get_running_loop().create_future()

    async def capture(reader, writer):
        received.set_result(await reader.readexactly(5))
        writer.close()

    server, port = await start_peer(capture)
    async with server, TcpTransport("127.0.0.1", port) as transport:
        await transport.send(b"\x81\x02\x01")
        assert await asyncio.wait_for(received, TIMEOUT) == b"\x03\x00\x81\x02\x01"


async def test_peer_close_raises():
    server, port = await start_peer(hang_up)
    async with server, TcpTransport("127.0.0.1", port) as transport:
        with pytest.raises(TransportClosed):
            await asyncio.wait_for(transport.recv(), TIMEOUT)
