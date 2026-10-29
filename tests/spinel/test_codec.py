# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import pytest

from opener_tools.spinel.codec import SpinelCodec

OP_PREFIX = (0x1234, 1, 1677, 1, 0x0102030405060708)
OP_PREFIX_HEX = "341201" + "8d0601" + "0807060504030201"


def test_encode_op_prefix():
    packed = SpinelCodec.encode_fields("SCSCX", *OP_PREFIX)

    assert packed.hex() == OP_PREFIX_HEX


def test_parse_op_prefix():
    payload = bytes.fromhex(OP_PREFIX_HEX)

    assert SpinelCodec.parse_fields(payload, "SCSCX") == OP_PREFIX


def test_packed_uint_roundtrip():
    packed = SpinelCodec.encode_fields("i", 0x3D00)

    assert packed == b"\x80\x7a"
    assert SpinelCodec.parse_fields(packed, "i") == (0x3D00,)


def test_packed_uint_zero():
    packed = SpinelCodec.encode_fields("i", 0)

    assert packed == b"\x00"
    assert SpinelCodec.parse_fields(packed, "i") == (0,)


def test_signed_byte_roundtrip():
    packed = SpinelCodec.encode_fields("cC", -3, 0xFD)

    assert packed == b"\xfd\xfd"
    assert SpinelCodec.parse_fields(packed, "cC") == (-3, 0xFD)


def test_data_with_length_then_data():
    packed = SpinelCodec.encode_fields("dd", b"\x01\x02", b"abc")

    assert packed == b"\x02\x00\x01\x02\x03\x00abc"
    assert SpinelCodec.parse_fields(packed, "dd") == (b"\x01\x02", b"abc")


def test_utf8_roundtrip():
    packed = SpinelCodec.encode_fields("U", "OPENER/1.0")

    assert packed == b"OPENER/1.0\x00"
    assert SpinelCodec.parse_fields(packed, "U") == ("OPENER/1.0",)


def test_too_many_fields():
    with pytest.raises(ValueError):
        SpinelCodec.encode_fields("C", 1, 2)


def test_unknown_format():
    with pytest.raises(ValueError):
        SpinelCodec.encode_fields("Z", 1)


def test_encode_packet():
    payload = SpinelCodec.encode_fields("i", 0x3D00)
    packet = SpinelCodec.encode_packet(2, payload, tid=0x81)

    assert packet == b"\x81\x02\x80\x7a"
