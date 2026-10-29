# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

from opener_tools.dectnrp.timing import Timing


def test_nrf91_timing():
    timing = Timing(2880)

    assert timing.ticks_per_subslot == 14400
    assert timing.ticks_per_slot == 28800
    assert timing.ticks_per_frame == 691200
    assert timing.ticks_per_ms == 69120


def test_conversions():
    timing = Timing(2880)

    assert timing.ms_to_ticks(20) == 1382400
    assert timing.ms_to_ticks(0.5) == 34560
    assert timing.ticks_to_ms(1382400) == 20.0
