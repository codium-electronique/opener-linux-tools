# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Radio scenarios shared by the simulated device and the hardware tests."""

import asyncio
from contextlib import asynccontextmanager

from opener_tools.dectnrp import DrvCaps, MsgReceived, Op, OpFinished, Radio
from opener_tools.spinel.client import SpinelClient
from opener_tools.spinel.dectnrp_constants import Reception
from opener_tools.transport import open_transport

CHANNEL = 1677
# Modem ticks with 2880 ticks per symbol.
TICKS_PER_MS = 69120
TICKS_100_MS = 100 * TICKS_PER_MS
TIMEOUT = 3.0


@asynccontextmanager
async def open_radio(url):
    """Yield a Radio on url, reset, initialised and enabled."""
    transport = open_transport(url)
    await transport.open()
    client = SpinelClient(transport)
    await client.start()
    radio = Radio(client)
    try:
        # The device may still run the ops of a previous test: start clean.
        await radio.reset()
        await radio.init()
        await radio.enable()
        yield radio
    finally:
        await radio.close()
        await client.close()
        await transport.close()


async def identity(radio: Radio) -> DrvCaps:
    caps = await radio.init()

    assert caps.ticks_per_symbol == 2880
    assert caps.op_slots == 8
    return caps


async def reset_enable_time(radio: Radio) -> None:
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


async def rx_window(
    radio: Radio, duration_ms: int, reception: Reception, timeout: float = TIMEOUT
) -> Op:
    """Run an immediate RX window; return its Op once finished."""
    duration = radio.caps.timing.ms_to_ticks(duration_ms)

    op = await radio.schedule_rx(CHANNEL, duration, reception=reception)
    result = await asyncio.wait_for(op, timeout)

    finished = [event for event in op.events if isinstance(event, OpFinished)]
    assert finished == [result]
    assert op.events[-1] is result
    return op


def received(op: Op) -> list[MsgReceived]:
    return [event for event in op.events if isinstance(event, MsgReceived)]


async def pipelined_windows(radio: Radio) -> None:
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
