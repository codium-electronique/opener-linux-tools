# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

from opener_tools.spinel.hdlc import HdlcDecoder, hdlc_encode

MAX_FRAME = 300


def test_fcs_check_value():
    # CRC-16/X-25 check value of "123456789" is 0x906E, sent LSB first.
    encoded = hdlc_encode(b"123456789")

    assert len(encoded) == 1 + 9 + 2 + 1
    assert encoded[10:] == b"\x6e\x90\x7e"


def test_encode_escapes():
    # FCS = 0xBB50: neither byte needs escaping.
    encoded = hdlc_encode(b"\x7e\x11\x42")

    assert encoded == bytes.fromhex("7e7d5e7d314250bb7e")


def test_encode_escapes_all_special_bytes():
    encoded = hdlc_encode(b"\x7e\x7d\x11\x13\xf8")

    assert encoded[1:11] == bytes.fromhex("7d5e7d5d7d317d337dd8")


def test_roundtrip_all_bytes():
    frame = bytes(range(256))
    decoder = HdlcDecoder(MAX_FRAME)

    frames = decoder.feed(hdlc_encode(frame))

    assert frames == [frame]
    assert decoder.errors == 0


def test_bad_fcs():
    frame = b"\x80\x02\x01\x02\x03"
    encoded = bytearray(hdlc_encode(frame))
    decoder = HdlcDecoder(MAX_FRAME)

    # Corrupt one payload byte: the frame is dropped.
    encoded[3] ^= 0x01
    assert decoder.feed(bytes(encoded)) == []
    assert decoder.errors == 1

    # Restore it: the same frame now decodes.
    encoded[3] ^= 0x01
    assert decoder.feed(bytes(encoded)) == [frame]
    assert decoder.errors == 1


def test_garbage_then_frame():
    garbage = bytes.fromhex("0013a57dff42115ef8")
    frame = b"\x81\x03\x00"
    decoder = HdlcDecoder(MAX_FRAME)

    # The frame's leading flag closes the garbage, which is dropped as a bad frame.
    frames = decoder.feed(garbage + hdlc_encode(frame))

    assert frames == [frame]
    assert decoder.errors == 1


def test_overflow_resync():
    big = b"\x55" * (MAX_FRAME + 10)
    frame = b"\x80\x01\x00"
    decoder = HdlcDecoder(MAX_FRAME)

    assert decoder.feed(hdlc_encode(big)) == []
    assert decoder.errors == 1

    assert decoder.feed(hdlc_encode(frame)) == [frame]
    assert decoder.errors == 1


def test_max_frame_accepted():
    frame = b"\x55" * MAX_FRAME
    decoder = HdlcDecoder(MAX_FRAME)

    assert decoder.feed(hdlc_encode(frame)) == [frame]
    assert decoder.errors == 0


def test_empty_frames_ignored():
    decoder = HdlcDecoder(MAX_FRAME)

    assert decoder.feed(b"\x7e\x7e\x7e") == []
    assert decoder.errors == 0


def test_split_feed_one_byte_at_a_time():
    first = b"\x81\x06\x01\x04\x03"
    second = bytes(range(0x70, 0x80))
    encoded = hdlc_encode(first) + hdlc_encode(second)
    decoder = HdlcDecoder(MAX_FRAME)
    frames = []

    for byte in encoded:
        frames.extend(decoder.feed(bytes([byte])))

    assert frames == [first, second]
    assert decoder.errors == 0


def test_frame_without_leading_flag():
    frame = b"\x80\x06\x00\x70"
    decoder = HdlcDecoder(MAX_FRAME)

    assert decoder.feed(hdlc_encode(frame)[1:]) == [frame]
