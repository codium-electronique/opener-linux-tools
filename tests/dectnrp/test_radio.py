# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import asyncio

import pytest
from fakes import FakeTransport, ScriptedDevice, event_frame, status_frame

from opener_tools.dectnrp import MsgReceived, OpFinished, Radio, TxParams
from opener_tools.spinel import SPINEL, SpinelCodec
from opener_tools.spinel import dectnrp_constants as nrp
from opener_tools.spinel.client import DeviceReset, SpinelClient, SpinelError
from opener_tools.spinel.dectnrp_constants import Reception

TIMEOUT = 0.5

# DRV_CAPS in FMT_DRV_CAPS order; ticks_per_symbol 2880, 4 operation slots.
CAPS = (0, 2, 4, 1, 1, 0, 4, 25344, 1, 0, 1, 0, 2880, 1400, 4, 1, 2, 3, 4, 5, 6, 7, 8)
PCC = bytes(range(10))


@pytest.fixture
async def device():
    fake = FakeTransport()
    device = ScriptedDevice(fake)
    device.props[SPINEL.PROP_INTERFACE_TYPE] = SpinelCodec.encode_i(nrp.INTERFACE_TYPE)
    device.props[SPINEL.PROP_CAPS] = SpinelCodec.encode_i(nrp.CAP_DRV)
    device.props[nrp.PROP_DRV_CAPS] = SpinelCodec.encode_fields(nrp.FMT_DRV_CAPS, *CAPS)
    device.start()
    yield device
    await device.stop()


@pytest.fixture
async def radio(device):
    client = SpinelClient(device.transport, timeout=TIMEOUT)
    await client.start()
    radio = Radio(client)
    yield radio
    await radio.close()
    await client.close()


def finished(token):
    return event_frame(token, nrp.EVENT_OP_FINISHED)


def received(token):
    suffix = SpinelCodec.encode_fields(
        nrp.FMT_EVT_MSG_RECEIVED, 1, 0b11, 1000, 0x50, 20, 1677, PCC, b"hi"
    )
    return event_frame(token, nrp.EVENT_MSG_RECEIVED, suffix)


async def test_init(radio):
    caps = await radio.init()

    assert caps.ticks_per_symbol == 2880
    assert caps.op_slots == 4
    assert caps.stop_to_rf_off == 8
    assert caps.timing.ticks_per_frame == 691200
    assert radio.caps is caps


async def test_init_checks_identity(radio, device):
    device.props[SPINEL.PROP_INTERFACE_TYPE] = SpinelCodec.encode_i(3)

    with pytest.raises(RuntimeError):
        await radio.init()


async def test_init_checks_caps(radio, device):
    device.props[SPINEL.PROP_CAPS] = SpinelCodec.encode_i(nrp.CAP_MAC)

    with pytest.raises(RuntimeError):
        await radio.init()


async def test_properties(radio, device):
    device.props[nrp.PROP_DRV_TIME] = SpinelCodec.encode_fields("X", 123456789)
    device.props[nrp.PROP_DRV_STATS] = SpinelCodec.encode_fields(
        nrp.FMT_DRV_STATS, 1, 2, 3, 4, 5, 6, 7
    )

    await radio.enable()
    await radio.set_network_id(0x12345678)
    await radio.set_filter_short_nwid(True, 0x78)
    await radio.set_filter_short_rxaddr(True, 0xBEEF)

    assert device.props[nrp.PROP_DRV_ENABLED] == b"\x01"
    assert device.props[nrp.PROP_DRV_NETWORK_ID] == bytes.fromhex("78563412")
    assert device.props[nrp.PROP_DRV_FILTER_SHORT_NWID] == b"\x01\x78"
    assert device.props[nrp.PROP_DRV_FILTER_SHORT_RXADDR] == b"\x01\xef\xbe"
    assert await radio.time() == 123456789
    stats = await radio.stats()
    assert stats.ops_accepted == 1
    assert stats.transport_errors == 7


async def test_schedule_rx_payload(radio, device):
    op = await radio.schedule_rx(channel=1677, duration=691200, start=1000)

    expected = SpinelCodec.encode_fields("SCSCXLC", op.token, 1, 1677, 1, 1000, 691200, 0)
    assert device.inserts == [expected]


async def test_rx_reception_flags(radio, device):
    await radio.schedule_rx(
        channel=1677, duration=691200, start=1000, reception=Reception.CONTINUOUS
    )

    flags = device.inserts[0][5]
    assert flags == 0x21


async def test_rx_immediate(radio, device):
    await radio.schedule_rx(channel=1677, duration=691200, beacon=True)

    flags = device.inserts[0][5]
    assert flags == nrp.OP_FLAG_BEACON


async def test_tx_payload(radio, device):
    params = TxParams(
        phy_header_type=1,
        tbs=136,
        mu=1,
        beta=1,
        mcs=2,
        slots=2,
        lbt_period=1000,
        lbt_rssi_threshold=0x80,
    )
    data = PCC + b"payload"

    op = await radio.schedule_tx(
        channel=1677, data=data, params=params, start=5000, lbt=True, harq=True
    )

    expected = SpinelCodec.encode_fields(
        nrp.FMT_OP_PREFIX + nrp.FMT_OP_TX,
        op.token,
        nrp.OP_TX,
        1677,
        nrp.OP_FLAG_SCHEDULED | nrp.OP_FLAG_LBT | nrp.OP_FLAG_HARQ,
        5000,
        1,
        136,
        1,
        1,
        2,
        2,
        1000,
        0x80,
        data,
    )
    assert device.inserts == [expected]


async def test_rssi1_payload(radio, device):
    op = await radio.schedule_rssi1(channel=1677, subslots=48)

    expected = SpinelCodec.encode_fields(
        nrp.FMT_OP_PREFIX + nrp.FMT_OP_RSSI1, op.token, nrp.OP_RSSI1, 1677, 0, 0, 48
    )
    assert device.inserts == [expected]


async def test_event_before_inserted(radio, device):
    device.on_insert = lambda token: [received(token)]

    op = await radio.schedule_rx(channel=1677, duration=691200)
    device.transport.inject(finished(op.token))
    result = await asyncio.wait_for(op, TIMEOUT)

    assert isinstance(result, OpFinished)
    assert op.done
    assert len(op.events) == 2
    assert isinstance(op.events[0], MsgReceived)
    assert op.events[1] is result


async def test_tokens_distinct(radio):
    first = await radio.schedule_rx(channel=1677, duration=1000)
    second = await radio.schedule_rx(channel=1677, duration=1000)

    assert first.token != second.token


async def test_insert_error_unregisters(radio, device):
    device.insert_status = SPINEL.STATUS_NOMEM

    with pytest.raises(SpinelError) as excinfo:
        await radio.schedule_rx(channel=1677, duration=691200)

    assert excinfo.value.status == SPINEL.STATUS_NOMEM
    (token,) = SpinelCodec.parse_fields(device.inserts[0], "S")
    assert token not in radio._ops


async def test_reset_fails_pending_ops(radio, device):
    first = await radio.schedule_rx(channel=1677, duration=691200)
    second = await radio.schedule_rssi1(channel=1677, subslots=48)

    device.transport.inject(status_frame(0, SPINEL.STATUS_RESET_POWER_ON))

    for op in (first, second):
        with pytest.raises(DeviceReset):
            await asyncio.wait_for(op, TIMEOUT)


async def test_reset_command_fails_pending_ops(radio):
    op = await radio.schedule_rx(channel=1677, duration=691200)

    await radio.reset()

    with pytest.raises(DeviceReset):
        await asyncio.wait_for(op, TIMEOUT)


async def test_finished_before_reset_resolves(radio, device):
    op = await radio.schedule_rx(channel=1677, duration=691200)

    device.transport.inject(finished(op.token))
    device.transport.inject(status_frame(0, SPINEL.STATUS_RESET_POWER_ON))

    result = await asyncio.wait_for(op, TIMEOUT)
    assert isinstance(result, OpFinished)


async def test_events_stream(radio, device):
    events = radio.events()
    op = await radio.schedule_rx(channel=1677, duration=691200)

    device.transport.inject(received(op.token))
    device.transport.inject(received(999))

    first = await asyncio.wait_for(anext(events), TIMEOUT)
    second = await asyncio.wait_for(anext(events), TIMEOUT)
    assert first.token == op.token
    assert second.token == 999
