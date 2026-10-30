# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

# The byte vectors are shared with the device's tests/subsys/spinel/spi_link
# suite in opener: keep them identical.

import pytest

from opener_tools.spinel.spi_frame import SpiHeader, build_header, parse_header


def test_build_header():
    header = build_header(1300, 7)

    assert header == bytes.fromhex("0214050700")


def test_build_header_reset():
    header = build_header(1300, 7, reset=True)

    assert header[0] == 0x82


def test_build_header_empty():
    header = build_header(1300, 0)

    assert header == bytes.fromhex("0214050000")


def test_parse_header():
    header = parse_header(bytes.fromhex("0200010300aabbcc"))

    assert header == SpiHeader(reset=False, recv_len=256, data_len=3)


def test_parse_header_reset():
    header = parse_header(bytes.fromhex("8214050000"))

    assert header == SpiHeader(reset=True, recv_len=1300, data_len=0)


@pytest.mark.parametrize("data", ["ffffffffff", "0000000000", "4200010000", "02000100"])
def test_parse_header_invalid(data):
    header = parse_header(bytes.fromhex(data))

    assert header is None


def test_round_trip():
    data = build_header(600, 42, reset=True)

    header = parse_header(data)

    assert header == SpiHeader(reset=True, recv_len=600, data_len=42)
