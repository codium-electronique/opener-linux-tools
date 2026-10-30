# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import asyncio

import pytest
from fakes import FakeRadio, make_caps

from opener_tools.dectnrp import MsgError, MsgReceived, OpFinished, pipeline
from opener_tools.dectnrp.pipeline import Gap, RxPipeline
from opener_tools.spinel.client import DeviceReset, SpinelError
from opener_tools.spinel.dectnrp_constants import Reception

TIMEOUT = 0.5
CHANNEL = 1677
TICKS_PER_MS = 69120
TRANSITION = 25920
WINDOW = 20 * TICKS_PER_MS
LEAD = 20 * TICKS_PER_MS
T0 = 1_000_000_000


@pytest.fixture
def radio(monkeypatch):
    radio = FakeRadio(make_caps(sched_op_transition=TRANSITION))
    radio.clock = T0
    # The host clock follows the modem clock, so advancing radio.clock is
    # seen as time passing on the host too.
    monkeypatch.setattr(pipeline, "monotonic", lambda: radio.clock / (TICKS_PER_MS * 1000))
    return radio


async def settle():
    """Let the pipeline run until it waits for an event."""
    for _ in range(20):
        await asyncio.sleep(0)


def starts(radio):
    return [start for _, start, _, _ in radio.scheduled]


def received(op):
    return MsgReceived(op.token, 0, 0, 1, T0, 0x50, 20, CHANNEL, bytes(10), b"hi")


def error(op):
    return MsgError(op.token, 0, 0, 1, T0, 0x50, 20, CHANNEL, bytes(10))


class Consumer:
    """Iterates a pipeline in a task, collecting what it yields."""

    def __init__(self, rx: RxPipeline):
        self.items = []
        self.task = asyncio.create_task(self._run(rx))

    async def _run(self, rx):
        async for item in rx:
            self.items.append(item)


async def test_back_to_back(radio):
    rx = RxPipeline(radio, CHANNEL, 20, depth=3)
    consumer = Consumer(rx)
    await settle()

    spacing = WINDOW + TRANSITION
    assert starts(radio) == [T0 + LEAD + k * spacing for k in range(3)]
    assert all(duration == WINDOW for _, _, duration, _ in radio.scheduled)

    radio.finish(radio.scheduled[0][0])
    await settle()

    assert starts(radio)[3] == T0 + LEAD + 3 * spacing
    consumer.task.cancel()


async def test_unknown_transition(radio):
    radio.caps = make_caps(sched_op_transition=0)
    rx = RxPipeline(radio, CHANNEL, 20, depth=2)
    consumer = Consumer(rx)
    await settle()

    assert starts(radio)[1] - starts(radio)[0] == WINDOW + 69120
    consumer.task.cancel()


async def test_yields_events_in_order(radio):
    rx = RxPipeline(radio, CHANNEL, 20, depth=2)
    consumer = Consumer(rx)
    await settle()
    first = radio.scheduled[0][0]
    second = radio.scheduled[1][0]

    events = [error(second), received(first), received(second)]
    for event in events:
        radio.emit(event)
    await settle()

    assert consumer.items == events
    consumer.task.cancel()


async def test_resync_on_late(radio):
    rx = RxPipeline(radio, CHANNEL, 20, depth=3)
    consumer = Consumer(rx)
    await settle()
    next_start = T0 + LEAD + 3 * (WINDOW + TRANSITION)

    radio.clock = next_start + 1000 * TICKS_PER_MS
    radio.finish(radio.scheduled[0][0])
    await settle()

    gap = consumer.items[0]
    assert gap == Gap(next_start, radio.clock + LEAD - next_start, "resync")
    assert starts(radio)[3] == radio.clock + LEAD
    assert rx.stats.gaps == 1
    assert rx.stats.gap_ticks == gap.length
    consumer.task.cancel()


async def test_resync_on_reject(radio):
    rx = RxPipeline(radio, CHANNEL, 20, depth=2)
    consumer = Consumer(rx)
    await settle()
    next_start = T0 + LEAD + 2 * (WINDOW + TRANSITION)

    radio.reject = 1
    radio.clock += 5 * TICKS_PER_MS
    radio.finish(radio.scheduled[0][0])
    await settle()

    # The rejected window is skipped, never moved before the queued ones.
    spacing = WINDOW + TRANSITION
    assert consumer.items == [Gap(next_start, spacing, "rejected")]
    assert starts(radio)[2] == next_start + spacing
    consumer.task.cancel()


async def test_reject_when_late(radio, monkeypatch):
    rx = RxPipeline(radio, CHANNEL, 20, depth=2)
    consumer = Consumer(rx)
    await settle()
    next_start = T0 + LEAD + 2 * (WINDOW + TRANSITION)

    # The device clock is ahead of the host's estimate: the start passed.
    radio.reject = 1
    radio.clock = next_start + 1000 * TICKS_PER_MS
    monkeypatch.setattr(pipeline, "monotonic", lambda: T0 / (TICKS_PER_MS * 1000))
    radio.finish(radio.scheduled[0][0])
    await settle()

    assert consumer.items == [Gap(next_start, radio.clock + LEAD - next_start, "rejected")]
    assert starts(radio)[2] == radio.clock + LEAD
    consumer.task.cancel()


async def test_double_reject_raises(radio):
    rx = RxPipeline(radio, CHANNEL, 20, depth=2)
    consumer = Consumer(rx)
    await settle()

    radio.reject = 2
    radio.finish(radio.scheduled[0][0])

    with pytest.raises(SpinelError):
        await asyncio.wait_for(consumer.task, TIMEOUT)


async def test_reception_passed(radio):
    rx = RxPipeline(radio, CHANNEL, 20, depth=2, reception=Reception.SEMICONTINUOUS)
    consumer = Consumer(rx)
    await settle()
    radio.finish(radio.scheduled[0][0])
    await settle()

    assert [reception for _, _, _, reception in radio.scheduled] == [Reception.SEMICONTINUOUS] * 3
    consumer.task.cancel()


async def test_early_finish_counted(radio):
    rx = RxPipeline(radio, CHANNEL, 20, depth=2)
    consumer = Consumer(rx)
    await settle()
    op = radio.scheduled[0][0]

    radio.emit(received(op))
    radio.finish(op)
    await settle()

    assert rx.stats.early_finishes == 1
    assert not any(isinstance(item, Gap) for item in consumer.items)
    consumer.task.cancel()


async def test_dropped_counted(radio):
    rx = RxPipeline(radio, CHANNEL, 20, depth=2)
    consumer = Consumer(rx)
    await settle()
    op = radio.scheduled[0][0]

    # The device reports its drops on the next event sent, whatever its token.
    radio.emit(MsgReceived(op.token, 0, 2, 1, T0, 0x50, 20, CHANNEL, bytes(10), b"hi"))
    radio.emit(OpFinished(op.token, 0, 3))
    radio.emit(OpFinished(999, 0, 1))
    await settle()

    assert rx.stats.dropped_events == 6
    consumer.task.cancel()


async def test_device_reset_propagates(radio):
    rx = RxPipeline(radio, CHANNEL, 20, depth=2)
    consumer = Consumer(rx)
    await settle()

    radio.device_reset()

    with pytest.raises(DeviceReset):
        await asyncio.wait_for(consumer.task, TIMEOUT)


async def test_stop_drains(radio):
    rx = RxPipeline(radio, CHANNEL, 20, depth=3)
    consumer = Consumer(rx)
    await settle()
    ops = [op for op, _, _, _ in radio.scheduled]

    stopping = asyncio.create_task(rx.stop())
    await settle()
    radio.finish(ops[0])
    radio.finish(ops[1])
    await settle()
    assert not stopping.done()

    radio.finish(ops[2])
    await asyncio.wait_for(stopping, TIMEOUT)
    await asyncio.wait_for(consumer.task, TIMEOUT)

    assert len(radio.scheduled) == 3
    await rx.stop()


async def test_depth_exceeds_slots(radio):
    with pytest.raises(ValueError):
        RxPipeline(radio, CHANNEL, 20, depth=9)
