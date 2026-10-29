# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import asyncio
import os

import pytest

from opener_tools.spinel import HdlcDecoder, hdlc_encode
from opener_tools.transport import TransportClosed, UartTransport

TIMEOUT = 1.0


@pytest.fixture
def pty():
    master, slave = os.openpty()
    os.set_blocking(master, False)
    yield master, os.ttyname(slave)
    os.close(master)
    os.close(slave)


async def read_master(master, size):
    data = b""
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while len(data) < size:
        assert asyncio.get_running_loop().time() < deadline, f"got {data.hex()}"
        try:
            data += os.read(master, size - len(data))
        except BlockingIOError:
            await asyncio.sleep(0.01)
    return data


async def test_recv_frames_split_across_writes(pty):
    master, name = pty
    first = bytes([0x81, 0x06, 0x00, 0x70])
    second = bytes([0x82, 0x06, 0x01, 0x7E, 0x7D, 0x11, 0x13, 0xF8])
    stream = hdlc_encode(first) + hdlc_encode(second)
    async with UartTransport(name) as transport:
        os.write(master, stream[:3])
        await asyncio.sleep(0.02)
        os.write(master, stream[3:12])
        await asyncio.sleep(0.02)
        os.write(master, stream[12:])
        assert await asyncio.wait_for(transport.recv(), TIMEOUT) == first
        assert await asyncio.wait_for(transport.recv(), TIMEOUT) == second
        assert transport.errors == 0


async def test_send_writes_hdlc_frame(pty):
    master, name = pty
    frame = bytes(range(256))
    expected = hdlc_encode(frame)
    async with UartTransport(name) as transport:
        await asyncio.wait_for(transport.send(frame), TIMEOUT)
        assert await read_master(master, len(expected)) == expected


async def read_frames(master, count):
    decoder = HdlcDecoder(1 << 20)
    frames = []
    deadline = asyncio.get_running_loop().time() + TIMEOUT
    while len(frames) < count:
        assert asyncio.get_running_loop().time() < deadline, (
            f"got {len(frames)} frames, {decoder.errors} errors"
        )
        try:
            data = os.read(master, 4096)
        except BlockingIOError:
            await asyncio.sleep(0.001)
            continue
        frames += decoder.feed(data)
    assert decoder.errors == 0
    return frames


async def test_concurrent_sends_do_not_interleave(pty):
    master, name = pty
    # Larger than the pty buffer: each send blocks part-way through.
    first = bytes([0x11]) * 20000
    second = bytes([0x22]) * 20000
    async with UartTransport(name, max_frame=len(first)) as transport:
        sends = asyncio.gather(transport.send(first), transport.send(second))
        frames = await read_frames(master, 2)
        await asyncio.wait_for(sends, TIMEOUT)
        assert sorted(frames) == [first, second]


async def test_garbage_before_frame_counts_error(pty):
    master, name = pty
    frame = bytes([0x81, 0x02, 0x01])
    async with UartTransport(name) as transport:
        os.write(master, b"\x01\x02\x03" + hdlc_encode(frame))
        assert await asyncio.wait_for(transport.recv(), TIMEOUT) == frame
        assert transport.errors == 1


async def test_recv_after_close_raises(pty):
    _, name = pty
    transport = UartTransport(name)
    await transport.open()
    await transport.close()
    with pytest.raises(TransportClosed):
        await asyncio.wait_for(transport.recv(), TIMEOUT)


async def test_peer_hang_up_raises():
    master, slave = os.openpty()
    name = os.ttyname(slave)
    os.close(slave)
    async with UartTransport(name) as transport:
        os.close(master)
        with pytest.raises(TransportClosed):
            await asyncio.wait_for(transport.recv(), TIMEOUT)
        with pytest.raises(TransportClosed):
            await transport.recv()
