# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Spinel SPI framing.

Same rules as the device side (opener subsys/spinel/spi_link.c) and
OpenThread's spi_frame.hpp. Each transaction starts with a 5-byte header in
both directions, followed by the raw Spinel frame, without HDLC:

    HDR (u8) | RECV_LEN (u16 LE) | DATA_LEN (u16 LE) | frame

HDR carries the RST, CRC and CCF flags and the pattern 0b10 in bits 1..0.
RECV_LEN is the frame length the sender can accept in this transaction,
DATA_LEN the length of the frame it sends, 0 for none. CRC is not supported:
a header with the CRC flag is invalid.
"""

import struct
from dataclasses import dataclass

HEADER_LEN = 5
FLAG_RST = 0x80
FLAG_CRC = 0x40
FLAG_CCF = 0x20
PATTERN = 0x02
PATTERN_MASK = 0x03

_HEADER = struct.Struct("<BHH")


@dataclass(frozen=True)
class SpiHeader:
    reset: bool
    recv_len: int
    data_len: int


def build_header(recv_len: int, data_len: int, reset: bool = False) -> bytes:
    hdr = PATTERN
    if reset:
        hdr |= FLAG_RST
    return _HEADER.pack(hdr, recv_len, data_len)


def parse_header(data: bytes) -> SpiHeader | None:
    """Return the header at the start of data, or None if it is shorter than
    a header, has a bad pattern or the CRC flag."""
    if len(data) < HEADER_LEN:
        return None
    hdr, recv_len, data_len = _HEADER.unpack_from(data)
    pattern = hdr & PATTERN_MASK
    if pattern != PATTERN:
        return None
    if hdr & FLAG_CRC:
        return None
    reset = bool(hdr & FLAG_RST)
    return SpiHeader(reset, recv_len, data_len)
