# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Capture output: DECT-NR-TAP records."""

from .tap import build_tap_record, parse_tap_record

__all__ = ["build_tap_record", "parse_tap_record"]
