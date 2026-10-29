# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import os

import pytest


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
