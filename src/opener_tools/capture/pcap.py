# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Classic libpcap writer: little endian, microsecond timestamps."""

import struct
from time import time_ns
from typing import BinaryIO

LINKTYPE_DECT_NR_TAP = 304
SNAPLEN = 65535

_GLOBAL_HEADER = struct.Struct("<IHHiIII")
_RECORD_HEADER = struct.Struct("<IIII")


class PcapWriter:
    """Writes one pcap record per write() to fp, which it owns: close()
    closes it. A BrokenPipeError from write() means the reader is gone."""

    def __init__(
        self, fp: BinaryIO, linktype: int = LINKTYPE_DECT_NR_TAP, flush_each: bool = False
    ):
        self.records = 0
        self._fp = fp
        self._flush_each = flush_each
        header = _GLOBAL_HEADER.pack(0xA1B2C3D4, 2, 4, 0, 0, SNAPLEN, linktype)
        fp.write(header)
        fp.flush()

    def write(self, record: bytes, ts_ns: int | None = None) -> None:
        """Write a record, time-stamped with ts_ns (host time, now if None)."""
        if ts_ns is None:
            ts_ns = time_ns()
        sec, rest_ns = divmod(ts_ns, 1_000_000_000)
        usec = rest_ns // 1000
        header = _RECORD_HEADER.pack(sec, usec, len(record), len(record))
        self._fp.write(header + record)
        if self._flush_each:
            self._fp.flush()
        self.records += 1

    def close(self) -> None:
        self._fp.close()
