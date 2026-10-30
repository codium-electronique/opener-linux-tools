# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import logging
import os
import signal
import threading
from importlib.metadata import version

from fakes import event_frame
from pcapfile import read_pcap

from opener_tools.cli.sniffer import main
from opener_tools.spinel import SPINEL, SpinelCodec
from opener_tools.spinel import dectnrp_constants as nrp

CHANNEL = 1677
DEPTH = 4
PCAP_HEADER_LEN = 24
DISABLE = (SPINEL.CMD_PROP_VALUE_SET, nrp.PROP_DRV_ENABLED, b"\x00")


def received(token):
    suffix = SpinelCodec.encode_fields(
        nrp.FMT_EVT_MSG_RECEIVED, 0, 0b11, 1000, 0x50, 20, CHANNEL, bytes(5), b"Hello World!"
    )
    return event_frame(token, nrp.EVENT_MSG_RECEIVED, suffix)


def interrupt():
    os.kill(os.getpid(), signal.SIGINT)


class FifoReader:
    """Reads a FIFO in a thread: everything, or only the first count bytes
    before closing it."""

    def __init__(self, path, count=None):
        self.path = path
        self.count = count
        self.data = b""
        self.thread = threading.Thread(target=self._run)
        self.thread.start()

    def _run(self):
        with open(self.path, "rb") as fifo:
            if self.count is None:
                self.data = fifo.read()
            else:
                self.data = fifo.read(self.count)

    def join(self):
        self.thread.join(5.0)
        assert not self.thread.is_alive()


def test_interfaces_output(capsys):
    code = main(["--extcap-interfaces", "--extcap-version=4.6"])

    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == [
        f"extcap {{version={version('opener-tools')}}}",
        "interface {value=nrp-sniffer}{display=DECT NR+ Sniffer (Opener Spinel)}",
    ]


def test_dlts_output(capsys):
    code = main(["--extcap-dlts", "--extcap-interface", "nrp-sniffer"])

    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["dlt {number=304}{name=DECT_NR_TAP}{display=DECT NR+ TAP}"]


def test_config_lists_args(capsys):
    code = main(["--extcap-config", "--extcap-interface", "nrp-sniffer"])

    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    args = [line for line in lines if line.startswith("arg ")]
    calls = [line.split("}{")[1] for line in args]
    assert calls == [
        "call=--transport",
        "call=--channel",
        "call=--network-id",
        "call=--rx-mode",
        "call=--window",
        "call=--depth",
        "call=--modem-time-tlv",
    ]
    assert "{type=string}" in args[0]
    assert "{default=uart:///dev/ttyACM0}" in args[0]
    assert "{type=unsigned}" in args[1]
    assert "{default=1677}" in args[1]
    assert "{type=string}" in args[2]
    assert "{default=0x32545279}" in args[2]
    assert "{type=selector}" in args[3]
    assert "{type=double}" in args[4]
    assert "{default=20}" in args[4]
    assert "{type=unsigned}" in args[5]
    assert "{default=4}" in args[5]
    assert "{type=boolflag}" in args[6]
    values = [line for line in lines if line.startswith("value {arg=3}")]
    assert values == [
        "value {arg=3}{value=single}{display=single-shot}{default=true}",
        "value {arg=3}{value=semi}{display=semicontinuous}{default=false}",
        "value {arg=3}{value=continuous}{display=continuous}{default=false}",
    ]


def test_capture_to_fifo(device, tmp_path, capsys):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    device.script = [received]
    device.when_written(1, DEPTH, interrupt)
    reader = FifoReader(path)

    code = main(
        [
            "--capture",
            "--extcap-interface",
            "nrp-sniffer",
            "--fifo",
            str(path),
            "--transport",
            "fake://",
            "--window",
            "5",
            "--extcap-capture-filter",
            "ignored",
        ]
    )

    reader.join()
    assert code == 0
    header, records = read_pcap(reader.data)
    assert header[6] == 304
    assert len(records) >= 1
    # Wireshark reports anything on stderr as an error.
    assert capsys.readouterr().err == ""
    log = logging.getLogger("nrp-sniffer")
    assert log.getEffectiveLevel() == logging.WARNING


def test_extcap_fifo_closed(device, tmp_path, caplog):
    path = tmp_path / "fifo"
    os.mkfifo(path)
    # More frames than it takes to notice the closed FIFO.
    device.script = [received] * 20
    device.when_written(20, DEPTH, interrupt)
    reader = FifoReader(path, PCAP_HEADER_LEN)

    # -v: extcap mode logs only warnings otherwise.
    with caplog.at_level(logging.INFO):
        code = main(
            ["--capture", "--fifo", str(path), "--transport", "fake://", "--window", "5", "-v"]
        )

    reader.join()
    assert code == 0
    assert "output closed, stopping" in caplog.text
    assert device.requests[-1] == DISABLE
    assert device.running_at_disable == 0
