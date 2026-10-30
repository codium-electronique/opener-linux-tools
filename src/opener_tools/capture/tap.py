# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique
# Derived from Deveritec dectnrp-sniffer (Apache-2.0), opener-samples deveritec/dectnrp-sniffer:samples/dectnrp-sniffer/

"""DECT-NR-TAP records (LINKTYPE_DECT_NR_TAP, 304), one per received frame.

A record is an 8-byte header, 8-byte TLV entries, then the PCC and PDC bytes:
- header: u8 version 1, u8 0, u16 TLV chain length in bytes, u16 body length,
  u16 0;
- TLV: u16 type, u16 value length (2 or 4), value zero-padded to 4 bytes.
All fields are little endian. The format (DSRCorporation/dect-nr-tap)
defines TLV types 1 to 9; the modem time TLVs are an Opener addition that
Wireshark shows as unknown, written only on request.
"""

import struct

from opener_tools.dectnrp import MsgError, MsgReceived

TAP_VERSION = 1
HDR_LEN = 8
TLV_LEN = 8
# The reference dissector rejects records shorter than 8 + 8 * 8 bytes.
MIN_RECORD_LEN = HDR_LEN + 8 * TLV_LEN

TLV_PAD = 0
TLV_DATA_TYPE = 1
TLV_HEADER_STATUS = 2
TLV_PCC_RSSI2 = 3
TLV_PCC_SNR = 4
TLV_PDC_RSSI2 = 5
TLV_PDC_SNR = 6
TLV_TRANSACTION_ID = 7
TLV_CHANNEL = 8
# Opener additions: the frame start, in modem time ticks.
TLV_MODEM_TIME_HI = 10
TLV_MODEM_TIME_LO = 11

# Plus the driver phy_type: 0 for a 40-bit PCC, 1 for an 80-bit one.
DATA_TYPE_PCC_PDC = 1
DATA_TYPE_PCC = 3
DATA_TYPE_NONE = 5

HDR_STATUS_VALID = 0
HDR_STATUS_PDC_ERROR = 1
HDR_STATUS_PDC_ABSENT = 2

_HDR = struct.Struct("<BBHHH")
_TLV = struct.Struct("<HH")


def _tlv(tlv_type: int, value: int, size: int) -> bytes:
    value_bytes = value.to_bytes(size, "little")
    padding = bytes(4 - size)
    return _TLV.pack(tlv_type, size) + value_bytes + padding


def _metric(value: int) -> int:
    # Raw driver value as 16 bits: a negative SNR is sign-extended.
    return value & 0xFFFF


def build_tap_record(ev: MsgReceived | MsgError, modem_time: bool = False) -> bytes:
    """Build the TAP record of a received frame, or of a PDC CRC error;
    with modem_time, add the frame start time TLVs."""
    if isinstance(ev, MsgReceived):
        body = ev.pcc + ev.pdc
        rssi2_type = TLV_PDC_RSSI2
        snr_type = TLV_PDC_SNR
        if ev.pdc:
            data_type = DATA_TYPE_PCC_PDC + ev.phy_type
            hdr_status = HDR_STATUS_VALID
        else:
            data_type = DATA_TYPE_PCC + ev.phy_type
            hdr_status = HDR_STATUS_PDC_ABSENT
    else:
        # There is no PDC: the metrics are the PCC's.
        rssi2_type = TLV_PCC_RSSI2
        snr_type = TLV_PCC_SNR
        hdr_status = HDR_STATUS_PDC_ERROR
        if ev.pcc is None:
            body = b""
            data_type = DATA_TYPE_NONE
        else:
            body = ev.pcc
            data_type = DATA_TYPE_PCC + ev.phy_type

    tlvs = [
        _tlv(TLV_DATA_TYPE, data_type, 2),
        _tlv(TLV_HEADER_STATUS, hdr_status, 2),
        _tlv(TLV_TRANSACTION_ID, ev.token & 0xFFFF, 2),
        _tlv(TLV_CHANNEL, ev.channel, 2),
    ]
    if ev.rssi2 is not None:
        tlvs.append(_tlv(rssi2_type, _metric(ev.rssi2), 2))
    if ev.snr is not None:
        tlvs.append(_tlv(snr_type, _metric(ev.snr), 2))
    if modem_time:
        time_lo = ev.start_time & 0xFFFFFFFF
        time_hi = (ev.start_time >> 32) & 0xFFFFFFFF
        tlvs.append(_tlv(TLV_MODEM_TIME_LO, time_lo, 4))
        tlvs.append(_tlv(TLV_MODEM_TIME_HI, time_hi, 4))
    # Wireshark shows padding as unknown TLVs: only as much as needed.
    while HDR_LEN + TLV_LEN * len(tlvs) + len(body) < MIN_RECORD_LEN:
        tlvs.append(_tlv(TLV_PAD, 0, 0))

    chain = b"".join(tlvs)
    header = _HDR.pack(TAP_VERSION, 0, len(chain), len(body), 0)
    return header + chain + body


def parse_tap_record(rec: bytes) -> dict[int | str, int | bytes]:
    """Return the TLV values by type, padding left out, and the PCC and PDC
    bytes under "body"; raise ValueError if the record is malformed."""
    if len(rec) < HDR_LEN:
        raise ValueError(f"record too short ({len(rec)} bytes)")
    _, _, chain_len, body_len, _ = _HDR.unpack_from(rec)
    body_start = HDR_LEN + chain_len
    if chain_len % TLV_LEN or len(rec) != body_start + body_len:
        raise ValueError(f"bad lengths: TLVs {chain_len}, body {body_len}, record {len(rec)}")
    values: dict[int | str, int | bytes] = {}
    for offset in range(HDR_LEN, body_start, TLV_LEN):
        tlv_type, size = _TLV.unpack_from(rec, offset)
        if tlv_type == TLV_PAD:
            continue
        value_start = offset + _TLV.size
        values[tlv_type] = int.from_bytes(rec[value_start : value_start + size], "little")
    values["body"] = rec[body_start:]
    return values
