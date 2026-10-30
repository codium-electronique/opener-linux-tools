# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import asyncio
import sys

import pytest
from fakes import FakeSpiDevice

from opener_tools.transport import SpiTransport, TransportClosed
from opener_tools.transport.spi_io import LinuxSpiIo

TIMEOUT = 1.0


@pytest.fixture
async def link():
    device = FakeSpiDevice()
    transport = SpiTransport("/dev/spidev0.0", ("gpiochip0", 0), ("gpiochip0", 1), io=device)
    await transport.open()
    yield transport, device
    await transport.close()


async def recv(transport):
    return await asyncio.wait_for(transport.recv(), TIMEOUT)


async def send(transport, frame):
    await asyncio.wait_for(transport.send(frame), TIMEOUT)


async def test_round_trip(link):
    transport, device = link

    await send(transport, b"\x81\x02\x03")
    assert device.received == [b"\x81\x02\x03"]

    device.queue(b"\x80\x06\x00")
    frame = await recv(transport)
    assert frame == b"\x80\x06\x00"
    assert transport.errors == 0


async def test_long_frame_second_transaction(link):
    transport, device = link
    long_frame = bytes(range(256)) * 2 + bytes(88)

    device.queue(long_frame)
    frame = await recv(transport)

    assert frame == long_frame
    assert device.transfers == [133, 605]


async def test_send_retried_until_accepted(link):
    transport, device = link
    device.recv_len = 0

    sending = asyncio.ensure_future(send(transport, b"\x81\x02\x03"))
    while not device.transfers:
        await asyncio.sleep(0)
    assert not sending.done()
    device.recv_len = 1300
    await sending

    assert device.received == [b"\x81\x02\x03"]
    assert transport.errors >= 1


async def test_invalid_header_retried(link):
    transport, device = link
    device.garbage_next()

    await send(transport, b"\x81\x02\x03")

    assert device.received == [b"\x81\x02\x03"]
    assert transport.errors == 1


async def test_waits_for_busy(link):
    transport, device = link
    device.busy_for(5)

    # The fake raises AssertionError on a transfer while BUSY is active.
    await send(transport, b"\x81\x02\x03")

    assert device.received == [b"\x81\x02\x03"]


async def test_irq_level_rechecked(link):
    transport, device = link

    # One rising edge: IRQ stays active while the second frame is pending.
    device.queue(b"\x80\x06\x01", b"\x80\x06\x02")
    first = await recv(transport)
    second = await recv(transport)

    assert [first, second] == [b"\x80\x06\x01", b"\x80\x06\x02"]
    assert device.edges == 1


async def test_close_ends_recv(link):
    transport, device = link
    device.recv_len = 0
    sending = asyncio.ensure_future(transport.send(b"\x81\x02\x03"))
    receiving = asyncio.ensure_future(transport.recv())
    while not device.transfers:
        await asyncio.sleep(0)

    await transport.close()

    with pytest.raises(TransportClosed):
        await asyncio.wait_for(sending, TIMEOUT)
    with pytest.raises(TransportClosed):
        await asyncio.wait_for(receiving, TIMEOUT)
    assert device.closed


async def test_io_error_closes(link):
    transport, device = link
    device.fail_next()

    device.queue(b"\x80\x06\x00")

    with pytest.raises(TransportClosed):
        await recv(transport)
    with pytest.raises(TransportClosed):
        await send(transport, b"\x81\x02\x03")


def test_missing_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "spidev", None)
    io = LinuxSpiIo("/dev/spidev3.0", ("gpiochip4", 0), ("gpiochip1", 1), 8_000_000)

    with pytest.raises(RuntimeError, match=r"pip install 'opener-tools\[spi\]'"):
        io.open()
