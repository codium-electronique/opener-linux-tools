# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import os

import pytest
from fakes import DeviceTransport, SniffDevice, make_caps

from opener_tools import transport
from opener_tools.transport import register_scheme

# nRF9131 values: 375 µs between scheduled operations.
TRANSITION = 25920


def pytest_addoption(parser):
    parser.addoption(
        "--transport",
        default=None,
        help="device transport URL for hardware tests, e.g. uart:///dev/ttyACM0?baud=1000000",
    )
    parser.addoption(
        "--peer",
        action="store_true",
        help="a dectnrp-driver device transmits on channel 1677 during the hardware tests",
    )


def pytest_collection_modifyitems(config, items):
    transport = config.getoption("--transport")
    simdev = os.environ.get("OPENER_SIMDEV_EXE")
    skip_hw = pytest.mark.skip(reason="needs --transport")
    skip_simdev = pytest.mark.skip(reason="needs OPENER_SIMDEV_EXE")
    for item in items:
        if "hw" in item.keywords and transport is None:
            item.add_marker(skip_hw)
        if "simdev" in item.keywords and simdev is None:
            item.add_marker(skip_simdev)


@pytest.fixture
def device(monkeypatch):
    """A SniffDevice reachable at fake://."""
    monkeypatch.setattr(transport, "_schemes", {})
    link = DeviceTransport()
    caps = make_caps(sched_op_transition=TRANSITION, sched_op_startup=2, rx_startup=3)
    link.device = SniffDevice(link, caps)
    register_scheme("fake", lambda parts: link)
    return link.device
