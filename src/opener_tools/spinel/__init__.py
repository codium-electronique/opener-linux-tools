# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Spinel codec, constants and HDLC-lite framing."""

from .codec import SpinelCodec
from .const import SPINEL
from .hdlc import HdlcDecoder, hdlc_encode

__all__ = ["SPINEL", "HdlcDecoder", "SpinelCodec", "hdlc_encode"]
