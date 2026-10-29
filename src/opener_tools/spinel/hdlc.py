# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Spinel HDLC-lite framing.

Same rules as the device side (opener subsys/spinel/hdlc.c): flag 0x7E, escape
0x7D (XOR 0x20) for 0x7E, 0x7D, 0x11, 0x13 and 0xF8, FCS-16 (CRC-16/X-25)
appended least significant byte first. The FCS table logic is pyspinel's
(spinel/hdlc.py, RFC 1662 Appendix C).
"""

HDLC_FLAG = 0x7E
HDLC_ESCAPE = 0x7D
HDLC_ESCAPE_XOR = 0x20
HDLC_FCS_SIZE = 2

# Other bytes Spinel HDLC-lite escapes: software flow control and vendor.
HDLC_XON = 0x11
HDLC_XOFF = 0x13
HDLC_VENDOR = 0xF8

HDLC_ESCAPED = frozenset((HDLC_FLAG, HDLC_ESCAPE, HDLC_XON, HDLC_XOFF, HDLC_VENDOR))

HDLC_FCS_INIT = 0xFFFF
HDLC_FCS_POLY = 0x8408
# FCS computed over a frame followed by its own (complemented, LSB first) FCS.
HDLC_FCS_GOOD = 0xF0B8

# Default largest Spinel frame, the device's CONFIG_DECTNRP_SPINEL_MAX_FRAME_SIZE.
DEFAULT_MAX_FRAME = 1300


def _make_fcs_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        fcs = byte
        for _ in range(8):
            if fcs & 1:
                fcs = (fcs >> 1) ^ HDLC_FCS_POLY
            else:
                fcs = fcs >> 1
        table.append(fcs & 0xFFFF)
    return tuple(table)


_FCS_TABLE = _make_fcs_table()


def _fcs_update(fcs: int, byte: int) -> int:
    index = (fcs ^ byte) & 0xFF
    return (fcs >> 8) ^ _FCS_TABLE[index]


def _put_byte(out: bytearray, byte: int) -> None:
    if byte in HDLC_ESCAPED:
        out.append(HDLC_ESCAPE)
        out.append(byte ^ HDLC_ESCAPE_XOR)
    else:
        out.append(byte)


def hdlc_encode(frame: bytes) -> bytes:
    """Return the HDLC-lite encoding of a Spinel frame, with both flags."""
    out = bytearray([HDLC_FLAG])
    fcs = HDLC_FCS_INIT
    for byte in frame:
        fcs = _fcs_update(fcs, byte)
        _put_byte(out, byte)

    fcs ^= 0xFFFF
    _put_byte(out, fcs & 0xFF)
    _put_byte(out, fcs >> 8)
    out.append(HDLC_FLAG)
    return bytes(out)


class HdlcDecoder:
    """Incremental HDLC-lite decoder.

    Frames are accepted with or without a leading flag. Bad FCS and frames
    longer than max_frame are dropped and counted in errors; the decoder
    resynchronizes on the next flag.
    """

    def __init__(self, max_frame: int = DEFAULT_MAX_FRAME):
        self.max_frame = max_frame
        self.errors = 0
        self._buf = bytearray()
        self._escaped = False
        self._overflow = False
        self._fcs = HDLC_FCS_INIT

    def _reset(self) -> None:
        self._buf = bytearray()
        self._escaped = False
        self._overflow = False
        self._fcs = HDLC_FCS_INIT

    def _end_of_frame(self) -> bytes | None:
        frame = None
        if self._overflow:
            self.errors += 1
        elif len(self._buf) == 0:
            # Back-to-back flags: empty frame, ignored.
            pass
        elif len(self._buf) < HDLC_FCS_SIZE or self._fcs != HDLC_FCS_GOOD:
            self.errors += 1
        else:
            frame = bytes(self._buf[:-HDLC_FCS_SIZE])
        self._reset()
        return frame

    def feed(self, data: bytes) -> list[bytes]:
        """Decode data; return the frames it completes, in order."""
        frames = []
        for byte in data:
            if byte == HDLC_FLAG:
                frame = self._end_of_frame()
                if frame is not None:
                    frames.append(frame)
                continue

            if self._overflow:
                continue

            if byte == HDLC_ESCAPE:
                self._escaped = True
                continue

            if self._escaped:
                byte ^= HDLC_ESCAPE_XOR
                self._escaped = False

            if len(self._buf) == self.max_frame + HDLC_FCS_SIZE:
                self._overflow = True
                continue

            self._buf.append(byte)
            self._fcs = _fcs_update(self._fcs, byte)
        return frames
