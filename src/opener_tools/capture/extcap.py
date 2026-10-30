# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Wireshark extcap interface of nrp-sniffer.

Wireshark runs the extcap with --extcap-interfaces, --extcap-dlts and
--extcap-config to list it, then with --capture --fifo PATH and the
configured arguments to capture; it stops the capture with SIGTERM.
See https://www.wireshark.org/docs/wsdg_html_chunked/ChCaptureExtcap.html.
"""

import argparse

INTERFACE = "nrp-sniffer"
INTERFACE_DISPLAY = "DECT NR+ Sniffer (Opener Spinel)"
DLT_DECT_NR_TAP = 304
DEFAULT_TRANSPORT = "uart:///dev/ttyACM0"

# The value of an extcap string argument; no braces, they delimit fields.
_INT_ANY_BASE = "^(0x[0-9a-fA-F]+|[0-9]+)$"


def wants_extcap(argv: list[str]) -> bool:
    """Whether Wireshark runs the program as an extcap."""
    for arg in argv:
        if arg.startswith("--extcap-") or arg == "--capture":
            return True
    return False


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("Wireshark extcap")
    group.add_argument("--extcap-interfaces", action="store_true")
    group.add_argument("--extcap-interface", default=INTERFACE)
    group.add_argument("--extcap-dlts", action="store_true")
    group.add_argument("--extcap-config", action="store_true")
    group.add_argument("--extcap-version", nargs="?", const="")
    # A capture filter would need a BPF engine for DLT 304: ignored.
    group.add_argument("--extcap-capture-filter")
    group.add_argument("--capture", action="store_true")
    group.add_argument("--fifo", metavar="PATH")


def interfaces(version: str) -> str:
    return (
        f"extcap {{version={version}}}\n"
        f"interface {{value={INTERFACE}}}{{display={INTERFACE_DISPLAY}}}\n"
    )


def dlts() -> str:
    return f"dlt {{number={DLT_DECT_NR_TAP}}}{{name=DECT_NR_TAP}}{{display=DECT NR+ TAP}}\n"


def config(
    channel: int,
    network_id: int,
    rx_modes: dict[str, str],
    rx_mode: str,
    window_ms: float,
    depth: int,
) -> str:
    """The capture options Wireshark shows. rx_modes maps each --rx-mode
    value to its display name; rx_mode is the default one."""
    lines = [
        "arg {number=0}{call=--transport}{display=Device}{type=string}"
        f"{{default={DEFAULT_TRANSPORT}}}{{required=true}}"
        "{tooltip=Spinel link: uart:///dev/ttyACM0?baud=1000000 or tcp://host:port}",
        "arg {number=1}{call=--channel}{display=Channel}{type=unsigned}"
        f"{{range=0,65535}}{{default={channel}}}"
        "{tooltip=DECT NR+ carrier number}",
        "arg {number=2}{call=--network-id}{display=Network ID}{type=string}"
        f"{{default={network_id:#010x}}}{{validation={_INT_ANY_BASE}}}"
        "{tooltip=32-bit network ID, decimal or 0x hex}",
        "arg {number=3}{call=--rx-mode}{display=RX mode}{type=selector}"
        "{tooltip=Single-shot receives every frame format; a frame ends the window}",
    ]
    for value, display in rx_modes.items():
        is_default = value == rx_mode
        default = str(is_default).lower()
        lines.append(f"value {{arg=3}}{{value={value}}}{{display={display}}}{{default={default}}}")
    lines += [
        "arg {number=4}{call=--window}{display=RX window (ms)}{type=double}"
        f"{{default={window_ms:g}}}"
        "{tooltip=Length of each scheduled RX window}",
        "arg {number=5}{call=--depth}{display=Queued windows}{type=unsigned}"
        f"{{default={depth}}}"
        "{tooltip=RX windows queued on the device}",
        "arg {number=6}{call=--modem-time-tlv}{display=Modem time TLVs}{type=boolflag}"
        "{default=false}"
        "{tooltip=Add the frame start modem time to each record; Wireshark shows it as unknown TLVs}",
    ]
    return "\n".join(lines) + "\n"
