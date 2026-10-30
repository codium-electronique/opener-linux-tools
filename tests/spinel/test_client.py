# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import asyncio
import time

import pytest
from fakes import FakeTransport

from opener_tools.spinel import SPINEL, SpinelCodec
from opener_tools.spinel import dectnrp_constants as nrp
from opener_tools.spinel.client import (
    MAX_IN_FLIGHT,
    DeviceReset,
    SpinelClient,
    SpinelError,
    SpinelTimeout,
)

TIMEOUT = 0.1


def make_frame(tid, cmd, prop, value=b""):
    header = 0x80 | tid
    payload = SpinelCodec.encode_i(prop) + value
    return SpinelCodec.encode_packet(cmd, payload, tid=header)


def status_frame(tid, status):
    value = SpinelCodec.encode_i(status)
    return make_frame(tid, SPINEL.RSP_PROP_VALUE_IS, SPINEL.PROP_LAST_STATUS, value)


def parse_request(frame):
    """Return (tid, cmd, prop) of a frame sent by the client."""
    tid = frame[0] & 0x0F
    cmd, cmd_len = SpinelCodec.parse_i(frame[1:])
    prop, _ = SpinelCodec.parse_i(frame[1 + cmd_len :])
    return tid, cmd, prop


async def answer_next(fake, value):
    """Answer the next request with PROP_VALUE_IS(value); return its TID."""
    frame = await fake.sent.get()
    tid, _, prop = parse_request(frame)
    fake.inject(make_frame(tid, SPINEL.RSP_PROP_VALUE_IS, prop, value))
    return tid


@pytest.fixture
async def fake():
    return FakeTransport()


@pytest.fixture
async def client(fake):
    client = SpinelClient(fake, timeout=TIMEOUT)
    await client.start()
    yield client
    await client.close()


async def test_get_matches_tid(client, fake):
    get = asyncio.create_task(client.get(nrp.PROP_DRV_TIME))

    tid = await answer_next(fake, b"\x2a")

    assert tid == 1
    assert await get == b"\x2a"


async def test_status_error(client, fake):
    get = asyncio.create_task(client.get(nrp.PROP_DRV_TIME))
    frame = await fake.sent.get()
    tid, _, _ = parse_request(frame)

    fake.inject(status_frame(tid, SPINEL.STATUS_PROP_NOT_FOUND))

    with pytest.raises(SpinelError) as excinfo:
        await get
    assert excinfo.value.status == SPINEL.STATUS_PROP_NOT_FOUND
    assert "STATUS_PROP_NOT_FOUND" in str(excinfo.value)


def test_errno_status_str():
    error = SpinelError(nrp.STATUS_ERRNO_BASE + 5)

    assert "errno 5" in str(error)


async def test_noop_status_ok(client, fake):
    noop = asyncio.create_task(client.request(SPINEL.CMD_NOOP))
    frame = await fake.sent.get()
    tid = frame[0] & 0x0F

    fake.inject(status_frame(tid, SPINEL.STATUS_OK))

    reply = await noop
    assert reply.tid == tid
    assert reply.prop == SPINEL.PROP_LAST_STATUS


async def test_prop_mismatch(client, fake):
    get = asyncio.create_task(client.get(nrp.PROP_DRV_TIME))
    frame = await fake.sent.get()
    tid, _, _ = parse_request(frame)

    fake.inject(make_frame(tid, SPINEL.RSP_PROP_VALUE_IS, nrp.PROP_DRV_CAPS, b"\x00"))

    with pytest.raises(SpinelError) as excinfo:
        await get
    assert excinfo.value.status == SPINEL.STATUS_PARSE_ERROR


async def test_timeout(client):
    start = time.monotonic()

    with pytest.raises(SpinelTimeout):
        await client.get(nrp.PROP_DRV_TIME)

    elapsed = time.monotonic() - start
    assert TIMEOUT * 0.9 <= elapsed < TIMEOUT * 5


async def test_stale_reply_ignored(client, fake):
    with pytest.raises(SpinelTimeout):
        await client.get(nrp.PROP_DRV_TIME)
    stale = await fake.sent.get()
    stale_tid, _, _ = parse_request(stale)
    assert stale_tid == 1

    tids = []
    for i in range(15):
        get = asyncio.create_task(client.get(nrp.PROP_DRV_TIME))
        tid = await answer_next(fake, bytes([i]))
        assert await get == bytes([i])
        tids.append(tid)
    assert 1 not in tids

    get = asyncio.create_task(client.get(nrp.PROP_DRV_TIME))
    frame = await fake.sent.get()
    tid, _, prop = parse_request(frame)
    fake.inject(make_frame(stale_tid, SPINEL.RSP_PROP_VALUE_IS, prop, b"stale"))
    fake.inject(make_frame(tid, SPINEL.RSP_PROP_VALUE_IS, prop, b"fresh"))
    assert await get == b"fresh"


async def test_concurrent_requests(client, fake):
    props = [nrp.PROP_DRV_ENABLED + i for i in range(20)]
    gets = [asyncio.create_task(client.get(prop)) for prop in props]

    answered = 0
    while answered < len(props):
        batch = [await fake.sent.get()]
        while not fake.sent.empty():
            batch.append(fake.sent.get_nowait())
        for frame in reversed(batch):
            tid, _, prop = parse_request(frame)
            value = SpinelCodec.encode_i(prop)
            fake.inject(make_frame(tid, SPINEL.RSP_PROP_VALUE_IS, prop, value))
        answered += len(batch)

    values = await asyncio.gather(*gets)
    assert values == [SpinelCodec.encode_i(prop) for prop in props]


async def test_in_flight_limit(client, fake):
    props = [nrp.PROP_DRV_ENABLED + i for i in range(MAX_IN_FLIGHT + 2)]
    gets = [asyncio.create_task(client.get(prop)) for prop in props]
    await asyncio.sleep(TIMEOUT / 10)

    assert fake.sent.qsize() == MAX_IN_FLIGHT

    await answer_next(fake, b"")
    await asyncio.sleep(TIMEOUT / 10)

    assert fake.sent.qsize() == MAX_IN_FLIGHT
    for get in gets:
        get.cancel()
    await asyncio.gather(*gets, return_exceptions=True)


async def test_unsolicited_fanout(client, fake):
    first = client.subscribe()
    second = client.subscribe()
    event = make_frame(0, SPINEL.RSP_PROP_VALUE_IS, nrp.PROP_DRV_EVENT, b"\x01\x02")

    fake.inject(event)

    for queue in (first, second):
        frame = await asyncio.wait_for(queue.get(), TIMEOUT)
        assert frame.tid == 0
        assert frame.prop == nrp.PROP_DRV_EVENT
        assert frame.value == b"\x01\x02"


async def test_unsubscribe(client, fake):
    queue = client.subscribe()
    client.unsubscribe(queue)

    fake.inject(make_frame(0, SPINEL.RSP_PROP_VALUE_IS, nrp.PROP_DRV_EVENT))
    await asyncio.sleep(0.01)

    assert queue.empty()


async def test_reset_ok(client, fake):
    resets = []
    client.on_reset.append(resets.append)
    reset = asyncio.create_task(client.reset())
    frame = await fake.sent.get()
    assert frame == bytes([0x80, SPINEL.CMD_RESET])
    tid = await next_noop(fake)

    fake.inject(status_frame(0, SPINEL.STATUS_RESET_SOFTWARE))
    fake.inject(status_frame(tid, SPINEL.STATUS_OK))

    await asyncio.wait_for(reset, TIMEOUT)
    assert resets == []


async def next_noop(fake):
    """Wait for the NOOP that reset() sends after CMD_RESET; return its TID."""
    frame = await asyncio.wait_for(fake.sent.get(), TIMEOUT)
    tid = frame[0] & 0x0F
    assert frame == bytes([0x80 | tid, SPINEL.CMD_NOOP])
    assert tid != 0
    return tid


async def test_reset_ignores_stale_reset_status(client, fake):
    # Over SPI, a notification the device could not send stays armed and
    # comes out with CMD_RESET: here the boot notification.
    resets = []
    client.on_reset.append(resets.append)
    reset = asyncio.create_task(client.reset())
    await fake.sent.get()
    fake.inject(status_frame(0, SPINEL.STATUS_RESET_POWER_ON))
    tid = await next_noop(fake)
    await asyncio.sleep(0.01)
    assert not reset.done()

    fake.inject(status_frame(0, SPINEL.STATUS_RESET_SOFTWARE))
    fake.inject(status_frame(tid, SPINEL.STATUS_OK))

    await asyncio.wait_for(reset, TIMEOUT)
    assert resets == []


async def test_reset_without_notification_times_out(client, fake):
    reset = asyncio.create_task(client.reset(timeout=TIMEOUT))
    await fake.sent.get()
    tid = await next_noop(fake)

    fake.inject(status_frame(tid, SPINEL.STATUS_OK))

    with pytest.raises(SpinelTimeout):
        await asyncio.wait_for(reset, 2 * TIMEOUT)


async def test_reset_fails_other_requests(client, fake):
    get = asyncio.create_task(client.get(nrp.PROP_DRV_TIME))
    await fake.sent.get()
    reset = asyncio.create_task(client.reset())
    await fake.sent.get()
    tid = await next_noop(fake)

    fake.inject(status_frame(0, SPINEL.STATUS_RESET_SOFTWARE))
    fake.inject(status_frame(tid, SPINEL.STATUS_OK))

    await asyncio.wait_for(reset, TIMEOUT)
    with pytest.raises(DeviceReset):
        await get


async def never_sent(frame):
    await asyncio.Future()


async def test_request_times_out_when_send_blocks(client, fake):
    # A device that never accepts the frame (BUSY stuck, no firmware).
    fake.send = never_sent

    with pytest.raises(SpinelTimeout):
        await asyncio.wait_for(client.get(nrp.PROP_DRV_TIME), 2 * TIMEOUT)


async def test_reset_times_out_when_send_blocks(client, fake):
    fake.send = never_sent

    with pytest.raises(SpinelTimeout):
        await asyncio.wait_for(client.reset(timeout=TIMEOUT), 2 * TIMEOUT)


async def test_unsolicited_reset_callback(client, fake):
    called = asyncio.Event()
    resets = []

    def on_reset(status):
        resets.append(status)
        called.set()

    client.on_reset.append(on_reset)

    fake.inject(status_frame(0, SPINEL.STATUS_RESET_POWER_ON))

    await asyncio.wait_for(called.wait(), TIMEOUT)
    assert resets == [SPINEL.STATUS_RESET_POWER_ON]


async def test_reset_fails_pending_request(client, fake):
    get = asyncio.create_task(client.get(nrp.PROP_DRV_TIME))
    await fake.sent.get()

    fake.inject(status_frame(0, SPINEL.STATUS_RESET_POWER_ON))

    with pytest.raises(DeviceReset) as excinfo:
        await get
    assert excinfo.value.status == SPINEL.STATUS_RESET_POWER_ON
