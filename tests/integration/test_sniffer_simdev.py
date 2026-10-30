# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""nrp-sniffer against the simulated device: a frame on air every 100 ms,
every 10th one failing its PDC CRC."""

import asyncio
import logging
import re
import shutil
import struct
import subprocess

import pytest
from pcapfile import read_pcap

from opener_tools.capture.tap import HDR_LEN, TLV_DATA_TYPE, TLV_LEN, TLV_PAD, parse_tap_record
from opener_tools.cli.sniffer import (
    DEFAULT_CHANNEL,
    DEFAULT_LEAD_MS,
    DEFAULT_NETWORK_ID,
    DEFAULT_RX_MODE,
    DEFAULT_WINDOW_MS,
    run_sniffer,
)

pytestmark = pytest.mark.simdev

CAPTURE_S = 3.0
DEPTH = 4
# The first Wireshark release with the DECT-NR-TAP dissector.
TSHARK_MIN = (4, 7, 4)
# Data type 5: no PCC, nothing for the DECT NR+ dissector.
DATA_TYPE_NONE = 5
# Expert info of the TLV types the dissector does not know: padding, and
# the modem time.
UNKNOWN_TLV = "Unknown TLV"
TSHARK_VERSION = re.compile(r"TShark \(Wireshark\) (\d+)\.(\d+)\.(\d+)")


async def capture(url, path, modem_time=False):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.call_later(CAPTURE_S, stop.set)
    log = logging.getLogger("test")
    with open(path, "wb") as out:
        stats = await run_sniffer(
            url,
            out,
            DEFAULT_CHANNEL,
            DEFAULT_NETWORK_ID,
            DEFAULT_RX_MODE,
            DEFAULT_WINDOW_MS,
            DEPTH,
            DEFAULT_LEAD_MS,
            stop,
            log,
            modem_time=modem_time,
        )
    return stats


def pad_count(record):
    tlvs_len = struct.unpack_from("<H", record, 2)[0]
    offsets = range(HDR_LEN, HDR_LEN + tlvs_len, TLV_LEN)
    types = [struct.unpack_from("<H", record, offset)[0] for offset in offsets]
    return types.count(TLV_PAD)


def tshark_version():
    """Return the tshark version as a tuple, None without tshark."""
    tshark = shutil.which("tshark")
    if tshark is None:
        return None
    result = subprocess.run([tshark, "--version"], capture_output=True, text=True, check=True)
    match = TSHARK_VERSION.search(result.stdout)
    if match is None:
        return None
    numbers = [int(group) for group in match.groups()]
    return tuple(numbers)


async def test_sniffer_captures(simdev_url, tmp_path):
    path = tmp_path / "capture.pcap"

    stats = await capture(simdev_url, path)

    data = path.read_bytes()
    _, records = read_pcap(data)
    values = [parse_tap_record(record) for _, _, record in records]
    received = [value for value in values if value[TLV_DATA_TYPE] == 2]
    errors = [value for value in values if value[TLV_DATA_TYPE] == 4]
    assert len(records) >= 20
    assert len(received) + len(errors) == len(records)
    # About one frame in ten fails.
    assert 1 <= len(errors) <= len(records) // 5
    for value in received:
        assert value["body"].endswith(b"Hello World!")
    assert stats.records == len(records)
    assert stats.crc_errors == len(errors)
    assert stats.gaps == 0


@pytest.mark.parametrize("modem_time", [False, True])
async def test_tshark_decodes(simdev_url, tmp_path, modem_time):
    version = tshark_version()
    if version is None or version < TSHARK_MIN:
        pytest.skip("needs tshark >= 4.7.4")
    path = tmp_path / "capture.pcap"
    await capture(simdev_url, path, modem_time)
    data = path.read_bytes()
    _, records = read_pcap(data)

    command = [
        "tshark",
        "-r",
        str(path),
        "-T",
        "fields",
        "-e",
        "dect_nr_tap.tlv_element.data_type",
        "-e",
        "dect_nr.phf.hf",
        "-e",
        "_ws.expert.message",
        "-E",
        "occurrence=a",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)

    lines = result.stdout.splitlines()
    assert len(lines) == len(records)
    for line, (_, _, record) in zip(lines, records, strict=True):
        data_type, header_format, messages = line.split("\t")
        values = parse_tap_record(record)
        assert int(data_type) == values[TLV_DATA_TYPE]
        if values[TLV_DATA_TYPE] != DATA_TYPE_NONE:
            assert header_format != ""
        expected_unknown = pad_count(record)
        if modem_time:
            expected_unknown += 2
        # tshark escapes the separator of the messages: count, not split.
        assert messages.count(UNKNOWN_TLV) == expected_unknown
