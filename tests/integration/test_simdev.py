# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Radio against the simulated device: a mock radio with a frame on air
every 100 ms, every 10th one failing its PDC CRC."""

import pytest
from integration import scenarios

from opener_tools.spinel.dectnrp_constants import Reception

pytestmark = pytest.mark.simdev


@pytest.fixture
async def radio(simdev_url):
    async with scenarios.open_radio(simdev_url) as radio:
        yield radio


async def test_identity(radio):
    caps = await scenarios.identity(radio)

    assert caps.sched_op_transition == 25920


async def test_reset_enable_time(radio):
    await scenarios.reset_enable_time(radio)


async def test_rx_window_receives(radio):
    # Continuous: a single-shot window closes on its first frame, which may
    # be one of the failing ones.
    op = await scenarios.rx_window(radio, 500, Reception.CONTINUOUS)

    received = scenarios.received(op)
    assert len(received) >= 1
    assert received[0].pdc == b"Hello World!"


async def test_semicontinuous_window(radio):
    op = await scenarios.rx_window(radio, 1000, Reception.SEMICONTINUOUS)

    received = scenarios.received(op)
    assert len(received) >= 5


async def test_pipelined_windows(radio):
    await scenarios.pipelined_windows(radio)


async def test_stats_count(radio):
    stats = await radio.stats()

    assert stats.ops_accepted >= 3
