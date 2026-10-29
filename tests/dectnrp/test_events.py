# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import pytest

from opener_tools.dectnrp.events import MsgError, MsgReceived, OpFinished, OpResults, decode_event
from opener_tools.spinel import SpinelCodec
from opener_tools.spinel import dectnrp_constants as nrp

PCC = bytes(range(10))
PDC = b"Hello World!"


def event_value(token, code, suffix_format="", *suffix, status=0, dropped=0):
    prefix = SpinelCodec.encode_fields(nrp.FMT_EVT_PREFIX, token, code, status, dropped)
    return prefix + SpinelCodec.encode_fields(suffix_format, *suffix)


def test_msg_received_valid():
    value = event_value(
        7,
        nrp.EVENT_MSG_RECEIVED,
        nrp.FMT_EVT_MSG_RECEIVED,
        1,
        0b11,
        123456789,
        0x50,
        -3,
        1677,
        PCC,
        PDC,
        dropped=2,
    )

    event = decode_event(value)

    assert event == MsgReceived(
        token=7,
        status=0,
        dropped=2,
        phy_type=1,
        start_time=123456789,
        rssi2=0x50,
        snr=-3,
        channel=1677,
        pcc=PCC,
        pdc=PDC,
    )


def test_msg_received_not_valid():
    value = event_value(
        7, nrp.EVENT_MSG_RECEIVED, nrp.FMT_EVT_MSG_RECEIVED, 1, 0, 1000, 0x50, 20, 1677, PCC, PDC
    )

    event = decode_event(value)

    assert event.rssi2 is None
    assert event.snr is None


def test_msg_error_without_pcc():
    value = event_value(
        3, nrp.EVENT_MSG_ERROR, nrp.FMT_EVT_MSG_ERROR, 1, 0b11, 1000, 0x50, 20, 1677, b"", status=4
    )

    event = decode_event(value)

    assert isinstance(event, MsgError)
    assert event.status == 4
    assert event.rssi2 == 0x50
    assert event.pcc is None


def test_msg_error_with_pcc():
    value = event_value(
        3, nrp.EVENT_MSG_ERROR, nrp.FMT_EVT_MSG_ERROR, 1, 0b111, 1000, 0x50, 20, 1677, PCC
    )

    event = decode_event(value)

    assert event.pcc == PCC


def test_op_results():
    rssi1 = bytes([0x60] * 48)
    value = event_value(9, nrp.EVENT_OP_RESULTS, nrp.FMT_EVT_OP_RESULTS, 1677, rssi1)

    event = decode_event(value)

    assert event == OpResults(token=9, status=0, dropped=0, channel=1677, rssi1=rssi1)


def test_op_finished():
    value = event_value(9, nrp.EVENT_OP_FINISHED, status=-5)

    assert decode_event(value) == OpFinished(token=9, status=-5, dropped=0)


def test_unknown_code():
    value = event_value(9, 42)

    with pytest.raises(ValueError):
        decode_event(value)


def test_truncated():
    with pytest.raises(ValueError):
        decode_event(b"\x01\x00")
