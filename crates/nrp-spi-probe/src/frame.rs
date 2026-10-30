// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Codium Electronique

//! Spinel SPI framing (same rules as opener_tools/spinel/spi_frame.py) and the
//! few Spinel frames the probe uses.

pub const HEADER_LEN: usize = 5;
const FLAG_CRC: u8 = 0x40;
const PATTERN: u8 = 0x02;
const PATTERN_MASK: u8 = 0x03;

pub const CMD_PROP_VALUE_GET: u8 = 2;
pub const CMD_PROP_VALUE_SET: u8 = 3;
pub const CMD_PROP_VALUE_IS: u8 = 6;
pub const PROP_PROTOCOL_VERSION: u32 = 1;
pub const PROP_NCP_VERSION: u32 = 2;
pub const PROP_DRV_ENABLED: u32 = 0x3d00;
pub const PROP_DRV_TIME: u32 = 0x3d02;

#[derive(Debug, PartialEq, Eq)]
pub struct SpiHeader {
    pub recv_len: u16,
    pub data_len: u16,
}

pub fn build_header(recv_len: u16, data_len: u16) -> [u8; HEADER_LEN] {
    let recv = recv_len.to_le_bytes();
    let data = data_len.to_le_bytes();
    [PATTERN, recv[0], recv[1], data[0], data[1]]
}

/// None if data is shorter than a header, has a bad pattern or the CRC flag.
pub fn parse_header(data: &[u8]) -> Option<SpiHeader> {
    if data.len() < HEADER_LEN {
        return None;
    }
    let hdr = data[0];
    if hdr & PATTERN_MASK != PATTERN || hdr & FLAG_CRC != 0 {
        return None;
    }
    let recv_len = u16::from_le_bytes([data[1], data[2]]);
    let data_len = u16::from_le_bytes([data[3], data[4]]);
    Some(SpiHeader { recv_len, data_len })
}

/// Spinel header byte: flag, IID 0 and the transaction id (1..=15).
pub fn spinel_header(tid: u8) -> u8 {
    0x80 | (tid & 0x0f)
}

pub fn tid_of(frame: &[u8]) -> Option<u8> {
    let first = frame.first()?;
    Some(first & 0x0f)
}

pub fn encode_packed(mut value: u32, out: &mut Vec<u8>) {
    loop {
        let low = (value & 0x7f) as u8;
        value >>= 7;
        if value == 0 {
            out.push(low);
            return;
        }
        out.push(low | 0x80);
    }
}

/// Packed unsigned integer at the start of data, with its length.
pub fn decode_packed(data: &[u8]) -> Option<(u32, usize)> {
    let mut value: u32 = 0;
    for (i, byte) in data.iter().take(4).enumerate() {
        value |= u32::from(byte & 0x7f) << (7 * i);
        if byte & 0x80 == 0 {
            return Some((value, i + 1));
        }
    }
    None
}

pub fn prop_value_get(tid: u8, prop: u32) -> Vec<u8> {
    let mut frame = vec![spinel_header(tid), CMD_PROP_VALUE_GET];
    encode_packed(prop, &mut frame);
    frame
}

pub fn prop_value_set(tid: u8, prop: u32, value: &[u8]) -> Vec<u8> {
    let mut frame = vec![spinel_header(tid), CMD_PROP_VALUE_SET];
    encode_packed(prop, &mut frame);
    frame.extend_from_slice(value);
    frame
}

/// Value of a PROP_VALUE_IS frame for prop, None for another frame.
pub fn prop_value_is(frame: &[u8], prop: u32) -> Option<&[u8]> {
    if frame.len() < 2 || frame[1] != CMD_PROP_VALUE_IS {
        return None;
    }
    let (got, len) = decode_packed(&frame[2..])?;
    if got != prop {
        return None;
    }
    Some(&frame[2 + len..])
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_round_trip() {
        let header = build_header(1300, 3);
        assert_eq!(header, [0x02, 0x14, 0x05, 0x03, 0x00]);
        let parsed = parse_header(&header);
        assert_eq!(
            parsed,
            Some(SpiHeader {
                recv_len: 1300,
                data_len: 3
            })
        );
    }

    #[test]
    fn header_rejects_bad_pattern_and_crc() {
        assert_eq!(parse_header(&[0x00, 0, 0, 0, 0]), None);
        assert_eq!(parse_header(&[0x42, 0, 0, 0, 0]), None);
        assert_eq!(parse_header(&[0x02, 0, 0]), None);
    }

    #[test]
    fn packed_uint() {
        let mut out = Vec::new();
        encode_packed(0x3c00, &mut out);
        assert_eq!(out, [0x80, 0x78]);
        assert_eq!(decode_packed(&out), Some((0x3c00, 2)));
        assert_eq!(decode_packed(&[0x80]), None);
    }

    #[test]
    fn value_get_and_is() {
        assert_eq!(prop_value_get(1, PROP_PROTOCOL_VERSION), [0x81, 0x02, 0x01]);
        let reply = [0x81, 0x06, 0x01, 0x04, 0x03];
        assert_eq!(
            prop_value_is(&reply, PROP_PROTOCOL_VERSION),
            Some(&[0x04, 0x03][..])
        );
        assert_eq!(prop_value_is(&reply, PROP_NCP_VERSION), None);
        assert_eq!(tid_of(&reply), Some(1));
        assert_eq!(
            prop_value_set(2, PROP_DRV_ENABLED, &[1]),
            [0x82, 0x03, 0x80, 0x7a, 0x01]
        );
    }
}
