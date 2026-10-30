# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Pure-Python reader for the pcap files the tests write."""

import struct


def read_pcap(data):
    """Return the global header fields and the (sec, usec, record) list."""
    header = struct.unpack_from("<IHHiIII", data)
    offset = 24
    records = []
    while offset < len(data):
        sec, usec, incl, orig = struct.unpack_from("<IIII", data, offset)
        assert incl == orig
        offset += 16
        records.append((sec, usec, data[offset : offset + incl]))
        offset += incl
    assert offset == len(data)
    return header, records
