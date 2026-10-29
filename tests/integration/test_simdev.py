# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Radio against the simulated device: a mock radio with a frame on air
every 100 ms, every 10th one failing its PDC CRC."""

import asyncio

import pytest

from opener_tools.dectnrp import MsgReceived, OpFinished, Radio
from opener_tools.spinel.client import SpinelClient
from opener_tools.spinel.dectnrp_constants import Reception
from opener_tools.transport import open_transport

pytestmark = pytest.mark.simdev

CHANNEL = 1677
# Modem ticks with 2880 ticks per symbol.
TICKS_PER_MS = 69120
TICKS_100_MS = 100 * TICKS_PER_MS
TIMEOUT = 3.0


@pytest.fixture
async def radio(simdev_url):
    transport = open_transport(simdev_url)
    await transport.open()
    client = SpinelClient(transport)
    await client.start()
    radio = Radio(client)
    # The device may still run the ops of a previous test: start clean.
    await radio.reset()
    await radio.init()
    await radio.enable()
    yield radio
    await radio.close()
    await client.close()
    await transport.close()


async def test_identity(radio):
    caps = await radio.init()

    assert caps.ticks_per_symbol == 2880
    assert caps.op_slots == 8
    assert caps.sched_op_transition == 25920


async def test_reset_enable_time(radio):
    await radio.reset()
    await radio.enable(True)

    loop = asyncio.get_running_loop()
    host_first = loop.time()
    first = await radio.time()
    await asyncio.sleep(0.1)
    host_second = loop.time()
    second = await radio.time()

    # The second read also waits for the first request's round trip (about
    # 20 ms over the simdev pty), so compare with the host interval.
    host_ms = (host_second - host_first) * 1000
    expected = round(host_ms * TICKS_PER_MS)
    elapsed = second - first
    assert abs(elapsed - expected) <= TICKS_100_MS // 10


async def test_rx_window_receives(radio):
    duration = radio.caps.timing.ms_to_ticks(500)

    # Continuous: a single-shot window closes on its first frame, which may
    # be one of the failing ones.
    op = await radio.schedule_rx(CHANNEL, duration, reception=Reception.CONTINUOUS)
    result = await asyncio.wait_for(op, TIMEOUT)

    received = [event for event in op.events if isinstance(event, MsgReceived)]
    assert len(received) >= 1
    assert received[0].pdc == b"Hello World!"
    assert op.events[-1] is result


async def test_semicontinuous_window(radio):
    duration = radio.caps.timing.ms_to_ticks(1000)

    op = await radio.schedule_rx(CHANNEL, duration, reception=Reception.SEMICONTINUOUS)
    await asyncio.wait_for(op, TIMEOUT)

    received = [event for event in op.events if isinstance(event, MsgReceived)]
    finished = [event for event in op.events if isinstance(event, OpFinished)]
    assert len(received) >= 5
    assert len(finished) == 1
    assert op.events[-1] is finished[0]


async def test_pipelined_windows(radio):
    timing = radio.caps.timing
    window = timing.ms_to_ticks(50)
    events = radio.events()

    now = await radio.time()
    start = now + timing.ms_to_ticks(20)
    ops = []
    for _ in range(3):
        op = await radio.schedule_rx(CHANNEL, window, start=start)
        ops.append(op)
        start += window + radio.caps.sched_op_transition

    finished = []
    while len(finished) < 3:
        event = await asyncio.wait_for(anext(events), TIMEOUT)
        if isinstance(event, OpFinished):
            finished.append(event.token)
    tokens = [op.token for op in ops]
    assert finished == tokens


async def test_stats_count(radio):
    stats = await radio.stats()

    assert stats.ops_accepted >= 3
