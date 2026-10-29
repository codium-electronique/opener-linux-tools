# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Smoke tests of a device running the dectnrp-spinel sample. A peer is
optional: only test_rx_from_peer, run with --peer, expects a frame on air."""

import asyncio

import pytest
from integration import scenarios

from opener_tools.spinel.dectnrp_constants import Reception

pytestmark = pytest.mark.hw

PEER_WINDOW_MS = 5000
# CONFIG_DECTNRP_SAMPLE_NETWORK_ID of dectnrp-driver: the PDC does not decode
# with another network ID.
PEER_NETWORK_ID = 0x32545279
PEER_TIMEOUT = 8.0


async def test_identity(radio):
    caps = await scenarios.identity(radio)

    assert caps.sched_op_transition > 0


async def test_reset_enable_time(radio):
    await scenarios.reset_enable_time(radio)


async def test_rx_window(radio):
    await scenarios.rx_window(radio, 500, Reception.CONTINUOUS)


async def test_pipelined_windows(radio):
    await scenarios.pipelined_windows(radio)


def is_peer_frame(event):
    # dectnrp-driver sends a type 2 physical header; its PDC starts with
    # the MAC header.
    return event.phy_type == 1 and b"Hello World!" in event.pdc


async def test_rx_from_peer(request, radio):
    if not request.config.getoption("--peer"):
        pytest.skip("needs --peer")

    # dectnrp-driver sends type 2 format 0 headers, unicast once synchronized:
    # nRF91x1 continuous reception does not receive them and semi-continuous
    # reception ends on them, so use single shot windows. Each one ends on
    # its first frame, which may fail or come from another transmitter: keep
    # opening windows for PEER_WINDOW_MS.
    await radio.set_network_id(PEER_NETWORK_ID)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + PEER_WINDOW_MS / 1000
    frames = []
    while not frames and loop.time() < deadline:
        remaining_ms = round((deadline - loop.time()) * 1000)
        op = await scenarios.rx_window(
            radio, remaining_ms, Reception.SINGLE_SHOT, timeout=PEER_TIMEOUT
        )
        received = scenarios.received(op)
        frames = [event for event in received if is_peer_frame(event)]

    assert len(frames) >= 1
