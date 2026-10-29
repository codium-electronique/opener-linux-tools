# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Driver events, the values of DRV_EVENT."""

import struct
from dataclasses import dataclass

from opener_tools.spinel import SpinelCodec
from opener_tools.spinel import dectnrp_constants as nrp

# Size of the FMT_EVT_PREFIX fields: token, code, status, dropped.
EVT_PREFIX_SIZE = 2 + 1 + 4 + 2


@dataclass(frozen=True)
class Event:
    token: int
    status: int
    dropped: int


@dataclass(frozen=True)
class MsgReceived(Event):
    phy_type: int
    start_time: int
    rssi2: int | None
    snr: int | None
    channel: int
    pcc: bytes
    pdc: bytes


@dataclass(frozen=True)
class MsgError(Event):
    phy_type: int
    start_time: int
    rssi2: int | None
    snr: int | None
    channel: int
    pcc: bytes | None


@dataclass(frozen=True)
class OpResults(Event):
    channel: int
    rssi1: bytes


@dataclass(frozen=True)
class OpFinished(Event):
    pass


def _valid_or_none(valid: int, bit: int, value: int) -> int | None:
    if valid & bit:
        return value
    return None


def decode_event(value: bytes) -> Event:
    """Decode a DRV_EVENT value; raise ValueError if it is malformed."""
    if len(value) < EVT_PREFIX_SIZE:
        raise ValueError(f"event too short ({len(value)} bytes)")
    token, code, status, dropped = SpinelCodec.parse_fields(value, nrp.FMT_EVT_PREFIX)
    suffix = value[EVT_PREFIX_SIZE:]
    try:
        return _decode_suffix(token, code, status, dropped, suffix)
    except (struct.error, IndexError):
        raise ValueError(f"truncated event, code {code}") from None


def _decode_suffix(token: int, code: int, status: int, dropped: int, suffix: bytes) -> Event:
    if code == nrp.EVENT_MSG_RECEIVED:
        fields = SpinelCodec.parse_fields(suffix, nrp.FMT_EVT_MSG_RECEIVED)
        phy_type, valid, start_time, rssi2, snr, channel, pcc, pdc = fields
        return MsgReceived(
            token=token,
            status=status,
            dropped=dropped,
            phy_type=phy_type,
            start_time=start_time,
            rssi2=_valid_or_none(valid, nrp.RX_VALID_RSSI2, rssi2),
            snr=_valid_or_none(valid, nrp.RX_VALID_SNR, snr),
            channel=channel,
            pcc=pcc,
            pdc=pdc,
        )
    if code == nrp.EVENT_MSG_ERROR:
        fields = SpinelCodec.parse_fields(suffix, nrp.FMT_EVT_MSG_ERROR)
        phy_type, valid, start_time, rssi2, snr, channel, pcc = fields
        pcc_valid = valid & nrp.RX_VALID_PCC
        if not pcc_valid or not pcc:
            pcc = None
        return MsgError(
            token=token,
            status=status,
            dropped=dropped,
            phy_type=phy_type,
            start_time=start_time,
            rssi2=_valid_or_none(valid, nrp.RX_VALID_RSSI2, rssi2),
            snr=_valid_or_none(valid, nrp.RX_VALID_SNR, snr),
            channel=channel,
            pcc=pcc,
        )
    if code == nrp.EVENT_OP_RESULTS:
        channel, rssi1 = SpinelCodec.parse_fields(suffix, nrp.FMT_EVT_OP_RESULTS)
        return OpResults(token=token, status=status, dropped=dropped, channel=channel, rssi1=rssi1)
    if code == nrp.EVENT_OP_FINISHED:
        return OpFinished(token=token, status=status, dropped=dropped)
    raise ValueError(f"unknown event code {code}")
