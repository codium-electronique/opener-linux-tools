# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""DECT NR+ Spinel identifiers.

Mirror of opener include/zephyr/net/dectnrp_spinel.h, the single source of
truth, with the DECTNRP_SPINEL_ prefix dropped. tests/spinel/test_constants.py
checks both stay in sync.
"""

from enum import IntEnum

# Driver-level vendor properties, sub-range 0x3D00-0x3DFF.
PROP_DRV_ENABLED = 0x3D00
PROP_DRV_CAPS = 0x3D01
PROP_DRV_TIME = 0x3D02
PROP_DRV_NETWORK_ID = 0x3D03
PROP_DRV_FILTER_SHORT_NWID = 0x3D04
PROP_DRV_FILTER_SHORT_RXADDR = 0x3D05
PROP_DRV_OP_QUEUE = 0x3D10
PROP_DRV_EVENT = 0x3D11
PROP_DRV_STATS = 0x3D20

# Capabilities, one per level; MAC, DLC and CVG are reserved.
CAP_DRV = 0x3D00
CAP_MAC = 0x3E00
CAP_DLC = 0x3F00
CAP_CVG = 0x3F80

# Vendor status of a driver errno: base + positive errno, capped at 0x3CFF.
STATUS_ERRNO_BASE = 0x3C80
STATUS_ERRNO_LAST = 0x3CFF

# PROP_INTERFACE_TYPE value for DECT NR+ ("NR").
INTERFACE_TYPE = 0x4E52

# DRV_OP_QUEUE type field.
OP_TX = 0
OP_RX = 1
OP_RSSI1 = 2

# DRV_OP_QUEUE flags.
OP_FLAG_SCHEDULED = 1 << 0
OP_FLAG_LBT = 1 << 1
OP_FLAG_HARQ = 1 << 2
OP_FLAG_BEACON = 1 << 3
OP_FLAG_RX_RECEPTION_SHIFT = 4
OP_FLAG_RX_RECEPTION_MASK = 0x3 << OP_FLAG_RX_RECEPTION_SHIFT

# DRV_EVENT code; differs from the driver's enum dectnrp_event_type.
EVENT_MSG_RECEIVED = 1
EVENT_MSG_ERROR = 2
EVENT_OP_RESULTS = 3
EVENT_OP_FINISHED = 4

# MSG_RECEIVED / MSG_ERROR valid bits.
RX_VALID_RSSI2 = 1 << 0
RX_VALID_SNR = 1 << 1
RX_VALID_PCC = 1 << 2

# Spinel formats.
FMT_OP_PREFIX = "SCSCX"
FMT_OP_TX = "CSCCCCLCd"
FMT_OP_RX = "LC"
FMT_OP_RSSI1 = "L"
FMT_EVT_PREFIX = "SClS"
FMT_EVT_MSG_RECEIVED = "CCXCcSdd"
FMT_EVT_MSG_ERROR = "CCXCcSd"
FMT_EVT_OP_RESULTS = "Sd"
FMT_DRV_CAPS = "LCCCCcCLCCCCLSCLLLLLLLL"
FMT_DRV_STATS = "LLLLLLL"


class OpType(IntEnum):
    TX = OP_TX
    RX = OP_RX
    RSSI1 = OP_RSSI1


class EventCode(IntEnum):
    MSG_RECEIVED = EVENT_MSG_RECEIVED
    MSG_ERROR = EVENT_MSG_ERROR
    OP_RESULTS = EVENT_OP_RESULTS
    OP_FINISHED = EVENT_OP_FINISHED
