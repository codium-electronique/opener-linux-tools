# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import shutil
import struct
import subprocess

import pytest
from pcapfile import read_pcap

from opener_tools.capture.pcap import PcapWriter

GLOBAL_HEADER = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 304)
RECORDS = [bytes(72), bytes(range(89)), bytes(200)]


def write_records(path):
    with path.open("wb") as fp:
        writer = PcapWriter(fp)
        for k, record in enumerate(RECORDS):
            writer.write(record, ts_ns=k * 1_000_000_000)
        writer.close()
    return writer


def test_global_header(tmp_path):
    path = tmp_path / "out.pcap"
    with path.open("wb") as fp:
        PcapWriter(fp)

    assert path.read_bytes() == GLOBAL_HEADER


def test_record_header(tmp_path):
    path = tmp_path / "out.pcap"
    with path.open("wb") as fp:
        writer = PcapWriter(fp)
        writer.write(bytes(72), ts_ns=1_700_000_000_123_456_789)
        writer.close()

    data = path.read_bytes()
    assert data[24:40] == struct.pack("<IIII", 1700000000, 123456, 72, 72)
    assert writer.records == 1


def test_default_timestamp_is_now(tmp_path, monkeypatch):
    monkeypatch.setattr("opener_tools.capture.pcap.time_ns", lambda: 5_000_002_000)
    path = tmp_path / "out.pcap"
    with path.open("wb") as fp:
        writer = PcapWriter(fp)
        writer.write(bytes(72))
        writer.close()

    _, records = read_pcap(path.read_bytes())
    assert records[0][:2] == (5, 2)


def test_flush_each(tmp_path):
    path = tmp_path / "out.pcap"
    with path.open("wb") as fp:
        writer = PcapWriter(fp, flush_each=True)
        writer.write(bytes(72), ts_ns=0)

        assert path.stat().st_size == 24 + 16 + 72
        writer.close()


def test_roundtrip_reader(tmp_path):
    path = tmp_path / "out.pcap"
    writer = write_records(path)

    header, records = read_pcap(path.read_bytes())
    assert header[-1] == 304
    assert [record for _, _, record in records] == RECORDS
    assert [sec for sec, _, _ in records] == [0, 1, 2]
    assert writer.records == 3


@pytest.mark.skipif(shutil.which("tshark") is None, reason="needs tshark")
def test_tshark_reads(tmp_path):
    path = tmp_path / "out.pcap"
    write_records(path)

    result = subprocess.run(
        ["tshark", "-r", str(path), "-T", "fields", "-e", "frame.len"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split() == [str(len(record)) for record in RECORDS]
