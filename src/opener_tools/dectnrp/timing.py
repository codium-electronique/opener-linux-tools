# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Modem time units."""

from dataclasses import dataclass

SYMBOLS_PER_SUBSLOT = 5
SYMBOLS_PER_SLOT = 10
SYMBOLS_PER_FRAME = 240
# A DECT NR+ frame lasts 10 ms (ETSI TS 103 636-3 V2.2.1 clause 4.4).
MS_PER_FRAME = 10


@dataclass(frozen=True)
class Timing:
    """Conversions between modem ticks and the DECT NR+ time units, from the
    modem ticks per symbol of DRV_CAPS."""

    ticks_per_symbol: int

    @property
    def ticks_per_subslot(self) -> int:
        return self.ticks_per_symbol * SYMBOLS_PER_SUBSLOT

    @property
    def ticks_per_slot(self) -> int:
        return self.ticks_per_symbol * SYMBOLS_PER_SLOT

    @property
    def ticks_per_frame(self) -> int:
        return self.ticks_per_symbol * SYMBOLS_PER_FRAME

    @property
    def ticks_per_ms(self) -> int:
        return self.ticks_per_frame // MS_PER_FRAME

    def ms_to_ticks(self, ms: float) -> int:
        return round(ms * self.ticks_per_ms)

    def ticks_to_ms(self, t: int) -> float:
        return t / self.ticks_per_ms
