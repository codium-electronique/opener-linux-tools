# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Check dectnrp_constants.py against the C header, the single source of truth."""

import os
import re
from pathlib import Path

import pytest

from opener_tools.spinel import dectnrp_constants

DEFAULT_OPENER_DIR = Path(__file__).parents[2] / ".." / "opener-samples/deps/dectnrp-opener"
HEADER = "include/zephyr/net/dectnrp_spinel.h"

ENUM_RE = re.compile(r"DECTNRP_SPINEL_(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)")
DEFINE_RE = re.compile(r"#define DECTNRP_SPINEL_(\w+)\s+(0x[0-9A-Fa-f]+|\d+)\s*$", re.MULTILINE)
BIT_RE = re.compile(r"#define DECTNRP_SPINEL_(\w+)\s+BIT\((\d+)\)")
FMT_RE = re.compile(r'#define DECTNRP_SPINEL_FMT_(\w+)\s+"(\w*)"')


@pytest.fixture(scope="module")
def header():
    opener_dir = Path(os.environ.get("OPENER_DIR", DEFAULT_OPENER_DIR))
    path = opener_dir / HEADER
    if not path.is_file():
        pytest.skip(f"{path} not found (set OPENER_DIR)")
    return path.read_text()


def c_numbers(header):
    values = {}
    for regex in (ENUM_RE, DEFINE_RE):
        for name, value in regex.findall(header):
            values[name] = int(value, 0)
    for name, bit in BIT_RE.findall(header):
        values[name] = 1 << int(bit)
    return values


def test_numbers_match(header):
    values = c_numbers(header)

    assert "PROP_DRV_ENABLED" in values
    assert "CAP_DRV" in values
    assert "OP_FLAG_SCHEDULED" in values
    for name, value in values.items():
        assert getattr(dectnrp_constants, name) == value, name


def test_formats_match(header):
    formats = FMT_RE.findall(header)

    assert formats
    for name, value in formats:
        assert getattr(dectnrp_constants, f"FMT_{name}") == value, name


def test_enums_match_constants():
    assert dectnrp_constants.OpType.RSSI1 == dectnrp_constants.OP_RSSI1
    assert dectnrp_constants.EventCode.OP_FINISHED == dectnrp_constants.EVENT_OP_FINISHED
