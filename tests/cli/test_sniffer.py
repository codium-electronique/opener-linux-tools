# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import asyncio
import io
import logging
import os
import signal

import pytest
from fakes import event_frame
from pcapfile import read_pcap

from opener_tools.capture.tap import (
    TLV_DATA_TYPE,
    TLV_MODEM_TIME_HI,
    TLV_MODEM_TIME_LO,
    parse_tap_record,
)
from opener_tools.cli.sniffer import main, run_sniffer
from opener_tools.spinel import SPINEL, SpinelCodec
from opener_tools.spinel import dectnrp_constants as nrp
from opener_tools.spinel.dectnrp_constants import Reception

TIMEOUT = 5.0
TICKS_PER_MS = 69120
CHANNEL = 1677
NETWORK_ID = 0x32545279
WINDOW_MS = 5
DEPTH = 4
LEAD_MS = 10
PCC = bytes(range(10))

RESET = (SPINEL.CMD_RESET, None, b"")
SET_NETWORK_ID = (
    SPINEL.CMD_PROP_VALUE_SET,
    nrp.PROP_DRV_NETWORK_ID,
    SpinelCodec.encode_fields("L", NETWORK_ID),
)
ENABLE = (SPINEL.CMD_PROP_VALUE_SET, nrp.PROP_DRV_ENABLED, b"\x01")
DISABLE = (SPINEL.CMD_PROP_VALUE_SET, nrp.PROP_DRV_ENABLED, b"\x00")
GET_TIME = (SPINEL.CMD_PROP_VALUE_GET, nrp.PROP_DRV_TIME, b"")


def received(token, dropped=0):
    suffix = SpinelCodec.encode_fields(
        nrp.FMT_EVT_MSG_RECEIVED, 0, 0b11, 1000, 0x50, 20, CHANNEL, PCC[:5], b"Hello World!"
    )
    return event_frame(token, nrp.EVENT_MSG_RECEIVED, suffix, dropped=dropped)


def crc_error(token):
    suffix = SpinelCodec.encode_fields(
        nrp.FMT_EVT_MSG_ERROR, 1, 0b111, 1000, 0x50, 20, CHANNEL, PCC
    )
    return event_frame(token, nrp.EVENT_MSG_ERROR, suffix)


async def sniff(out, stop):
    log = logging.getLogger("test")
    sniffer = run_sniffer(
        "fake://",
        out,
        CHANNEL,
        NETWORK_ID,
        Reception.SINGLE_SHOT,
        WINDOW_MS,
        DEPTH,
        LEAD_MS,
        stop,
        log,
    )
    return await asyncio.wait_for(sniffer, TIMEOUT)


def data_types(path):
    data = path.read_bytes()
    _, records = read_pcap(data)
    types = []
    for _, _, record in records:
        values = parse_tap_record(record)
        types.append(values[TLV_DATA_TYPE])
    return types


def find_in_order(requests, expected):
    """Return the index of each expected request, each searched after the
    previous one; raise ValueError if one is missing."""
    indexes = []
    start = 0
    for request in expected:
        index = requests.index(request, start)
        indexes.append(index)
        start = index + 1
    return indexes


def stop_at(stop, n):
    def after_insert(count):
        if count == n:
            stop.set()

    return after_insert


def interrupt_at(n):
    def after_insert(count):
        if count == n:
            os.kill(os.getpid(), signal.SIGINT)

    return after_insert


class ClosedPipe(io.BytesIO):
    """Takes the pcap header, then fails like a pipe whose reader is gone.
    Closing it fails once too, as flushing a buffered pipe does."""

    def __init__(self):
        super().__init__()
        self.close_failed = False

    def write(self, data):
        position = self.tell()
        if position > 0:
            raise BrokenPipeError(32, "Broken pipe")
        return super().write(data)

    def close(self):
        super().close()
        if not self.close_failed:
            self.close_failed = True
            raise BrokenPipeError(32, "Broken pipe")


async def test_writes_records(device, tmp_path):
    stop = asyncio.Event()
    device.script = [lambda token: received(token, dropped=2), received, crc_error, received]
    device.when_written(4, DEPTH, stop.set)
    path = tmp_path / "out.pcap"

    stats = await sniff(path.open("wb"), stop)

    assert data_types(path) == [1, 1, 4, 1]
    assert stats.records == 4
    assert stats.crc_errors == 1
    assert stats.dropped_events == 2


async def test_init_sequence(device, tmp_path):
    stop = asyncio.Event()
    device.after_insert = stop_at(stop, 1)
    path = tmp_path / "out.pcap"

    await sniff(path.open("wb"), stop)

    requests = device.requests
    indexes = find_in_order(requests, [RESET, SET_NETWORK_ID, ENABLE, GET_TIME])
    commands = [cmd for cmd, _, _ in requests]
    first_insert = commands.index(SPINEL.CMD_PROP_VALUE_INSERT)
    assert first_insert > indexes[-1]


async def test_device_reset_reinit(device, tmp_path):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    device.script = [received] * 4
    device.when_written(2, DEPTH, lambda: loop.call_soon(device.power_on_reset))
    device.when_written(4, DEPTH, stop.set)
    path = tmp_path / "out.pcap"

    stats = await sniff(path.open("wb"), stop)

    assert data_types(path) == [1, 1, 1, 1]
    assert stats.resets == 1
    assert device.requests.count(SET_NETWORK_ID) == 2
    assert device.requests.count(ENABLE) == 2


async def test_clean_stop(device, tmp_path):
    stop = asyncio.Event()
    device.after_insert = stop_at(stop, 6)
    out = (tmp_path / "out.pcap").open("wb")

    await sniff(out, stop)

    assert len(device.ops) == 6
    assert device.requests[-1] == DISABLE
    assert device.running_at_disable == 0
    assert out.closed


async def test_broken_pipe_is_clean(device):
    stop = asyncio.Event()
    device.script = [received]

    stats = await sniff(ClosedPipe(), stop)

    assert stats.records == 0
    assert device.requests[-1] == DISABLE
    assert device.running_at_disable == 0


def test_cli_args(device, tmp_path):
    device.after_insert = interrupt_at(1)
    path = tmp_path / "out.pcap"

    code = main(["--transport", "fake://", "-w", str(path), "--network-id", "0x1234"])

    assert code == 0
    network_id = SpinelCodec.encode_fields("L", 0x1234)
    assert (SPINEL.CMD_PROP_VALUE_SET, nrp.PROP_DRV_NETWORK_ID, network_id) in device.requests
    data = path.read_bytes()
    header, _ = read_pcap(data)
    assert header[6] == 304


@pytest.mark.parametrize(
    "args, window_ms", [(["--window", "5"], 5), (["--window", "0.5"], 0.5), ([], 20)]
)
def test_window_option(device, tmp_path, args, window_ms):
    device.after_insert = interrupt_at(1)
    path = tmp_path / "out.pcap"

    code = main(["--transport", "fake://", "-w", str(path), *args])

    assert code == 0
    _, flags, _, duration = device.ops[0]
    assert duration == round(window_ms * TICKS_PER_MS)
    reception = (flags & nrp.OP_FLAG_RX_RECEPTION_MASK) >> nrp.OP_FLAG_RX_RECEPTION_SHIFT
    assert reception == Reception.SINGLE_SHOT


@pytest.mark.parametrize("window", ["0", "-1"])
def test_window_must_be_positive(window):
    with pytest.raises(SystemExit) as exc_info:
        main(["--transport", "fake://", "--window", window])

    assert exc_info.value.code == 2


def test_rx_mode_option(device, tmp_path):
    device.after_insert = interrupt_at(1)
    path = tmp_path / "out.pcap"

    main(["--transport", "fake://", "-w", str(path), "--rx-mode", "continuous"])

    _, flags, _, _ = device.ops[0]
    reception = (flags & nrp.OP_FLAG_RX_RECEPTION_MASK) >> nrp.OP_FLAG_RX_RECEPTION_SHIFT
    assert reception == Reception.CONTINUOUS


@pytest.mark.parametrize("args, expected", [(["--modem-time-tlv"], True), ([], False)])
def test_modem_time_option(device, tmp_path, args, expected):
    device.script = [received]
    device.when_written(1, DEPTH, lambda: os.kill(os.getpid(), signal.SIGINT))
    path = tmp_path / "out.pcap"

    main(["--transport", "fake://", "-w", str(path), *args])

    data = path.read_bytes()
    _, records = read_pcap(data)
    values = parse_tap_record(records[0][2])
    assert (TLV_MODEM_TIME_LO in values) == expected
    assert (TLV_MODEM_TIME_HI in values) == expected
