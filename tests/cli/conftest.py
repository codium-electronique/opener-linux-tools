# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import pytest
from fakes import DeviceTransport, SniffDevice, make_caps

from opener_tools import transport
from opener_tools.transport import register_scheme

# nRF9131 values: 375 µs between scheduled operations.
TRANSITION = 25920


@pytest.fixture
def device(monkeypatch):
    """A SniffDevice reachable at fake://."""
    monkeypatch.setattr(transport, "_schemes", {})
    link = DeviceTransport()
    caps = make_caps(sched_op_transition=TRANSITION, sched_op_startup=2, rx_startup=3)
    link.device = SniffDevice(link, caps)
    register_scheme("fake", lambda parts: link)
    return link.device
