# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import struct
from pathlib import Path

from opener_tools.capture.tap import (
    TLV_CHANNEL,
    TLV_DATA_TYPE,
    TLV_HEADER_STATUS,
    TLV_MODEM_TIME_HI,
    TLV_MODEM_TIME_LO,
    TLV_PAD,
    TLV_PDC_RSSI2,
    TLV_PDC_SNR,
    TLV_TRANSACTION_ID,
    build_tap_record,
    parse_tap_record,
)
from opener_tools.dectnrp import MsgError, MsgReceived

PCC = bytes([1, 2, 3, 4, 5])
PDC = b"Hello World!"
START = 0x0000000102030405
REFERENCE = Path(__file__).parent / "data" / "deveritec_record.bin"


def frame(**fields):
    values = dict(
        token=7,
        status=0,
        dropped=0,
        phy_type=0,
        start_time=START,
        rssi2=0x50,
        snr=-3,
        channel=1677,
        pcc=PCC,
        pdc=PDC,
    )
    values.update(fields)
    return MsgReceived(**values)


def error(**fields):
    values = dict(
        token=7,
        status=0,
        dropped=0,
        phy_type=1,
        start_time=START,
        rssi2=None,
        snr=None,
        channel=1677,
        pcc=bytes(range(10)),
    )
    values.update(fields)
    return MsgError(**values)


def tlv_types(record):
    tlvs_len = struct.unpack_from("<H", record, 2)[0]
    offsets = range(8, 8 + tlvs_len, 8)
    return [struct.unpack_from("<H", record, offset)[0] for offset in offsets]


def test_record_layout():
    record = build_tap_record(frame(), modem_time=True)
    types = tlv_types(record)

    assert record[:8] == bytes([1, 0, 8 * len(types), 0, 17, 0, 0, 0])
    tlvs = parse_tap_record(record)
    body = tlvs.pop("body")
    assert tlvs == {
        TLV_DATA_TYPE: 1,
        TLV_HEADER_STATUS: 0,
        TLV_TRANSACTION_ID: 7,
        TLV_CHANNEL: 1677,
        TLV_PDC_RSSI2: 0x50,
        TLV_PDC_SNR: 0xFFFD,
        TLV_MODEM_TIME_LO: 0x02030405,
        TLV_MODEM_TIME_HI: 0x1,
    }
    assert body == PCC + PDC
    assert record[-17:] == PCC + PDC
    assert len(record) == 8 + 8 * len(types) + 17


def test_no_modem_time_by_default():
    record = build_tap_record(frame())
    types = tlv_types(record)

    # The DECT-NR-TAP format defines types 1 to 9 only.
    assert TLV_MODEM_TIME_LO not in types
    assert TLV_MODEM_TIME_HI not in types
    assert TLV_PAD not in types
    assert len(record) == 8 + 6 * 8 + 17


def test_min_record_length():
    record = build_tap_record(frame(rssi2=None, snr=None))
    types = tlv_types(record)

    # Padded only up to the 72 bytes the reference dissector requires.
    assert types[4:] == [TLV_PAD, TLV_PAD]
    assert len(record) == 8 + 6 * 8 + 17


def test_no_padding_with_modem_time():
    record = build_tap_record(frame(rssi2=None, snr=None), modem_time=True)
    types = tlv_types(record)

    assert TLV_PAD not in types


def test_empty_pdc():
    tlvs = parse_tap_record(build_tap_record(frame(pdc=b"")))

    assert tlvs[TLV_DATA_TYPE] == 3
    assert tlvs[TLV_HEADER_STATUS] == 2
    assert tlvs["body"] == PCC


def test_pdc_crc_error():
    pcc = bytes(range(10))
    tlvs = parse_tap_record(build_tap_record(error(pcc=pcc)))

    assert tlvs[TLV_DATA_TYPE] == 4
    assert tlvs[TLV_HEADER_STATUS] == 1
    assert tlvs["body"] == pcc


def test_msg_error_metrics_are_pcc():
    tlvs = parse_tap_record(build_tap_record(error(rssi2=0x40, snr=12)))

    assert tlvs[3] == 0x40
    assert tlvs[4] == 12
    assert TLV_PDC_RSSI2 not in tlvs
    assert TLV_PDC_SNR not in tlvs


def test_msg_error_no_pcc():
    record = build_tap_record(error(pcc=None))
    tlvs = parse_tap_record(record)

    assert tlvs[TLV_DATA_TYPE] == 5
    assert tlvs[TLV_HEADER_STATUS] == 1
    assert tlvs["body"] == b""
    assert len(record) == 72


def test_transaction_id_wraps():
    tlvs = parse_tap_record(build_tap_record(frame(token=0x12345)))

    assert tlvs[TLV_TRANSACTION_ID] == 0x2345


def test_matches_deveritec_reference():
    reference = parse_tap_record(REFERENCE.read_bytes())
    ours = parse_tap_record(build_tap_record(frame()))

    for tlv_type in (1, 2, 7, 8, 5, 6):
        assert ours[tlv_type] == reference[tlv_type], tlv_type
    assert ours["body"] == reference["body"]
