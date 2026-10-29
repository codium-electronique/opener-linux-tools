#
#  Copyright (c) 2016-2017, The OpenThread Authors.
#  All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#  Vendored from https://github.com/openthread/pyspinel spinel/codec.py@5e0627abd04b7d2c7ca47c6615bd14b31227eb4d,
#  modified: kept the SpinelCodec class only, imports reduced to struct; added
#  encode_X; 'c' encodes signed; encode_i(0) gives one byte; encode_U encodes
#  to UTF-8 bytes; parse_field('i') returns the value only; encode_fields and
#  next_code are classmethods; unknown format codes and extra fields raise
#  ValueError instead of printing a traceback.
#
"""
Module providing a Spinel coder / decoder class.
"""

from struct import pack
from struct import unpack

from .const import SPINEL

#=========================================
#   SpinelCodec
#=========================================

#  0: DATATYPE_NULL
#'.': DATATYPE_VOID: Empty data type. Used internally.
#'b': DATATYPE_BOOL: Boolean value. Encoded in 8-bits as either 0x00 or 0x01.
#                    All other values are illegal.
#'C': DATATYPE_UINT8: Unsigned 8-bit integer.
#'c': DATATYPE_INT8: Signed 8-bit integer.
#'S': DATATYPE_UINT16: Unsigned 16-bit integer. (Little-endian)
#'s': DATATYPE_INT16: Signed 16-bit integer. (Little-endian)
#'L': DATATYPE_UINT32: Unsigned 32-bit integer. (Little-endian)
#'l': DATATYPE_INT32: Signed 32-bit integer. (Little-endian)
#'i': DATATYPE_UINT_PACKED: Packed Unsigned Integer. (See section 7.2)
#'6': DATATYPE_IPv6ADDR: IPv6 Address. (Big-endian)
#'E': DATATYPE_EUI64: EUI-64 Address. (Big-endian)
#'e': DATATYPE_EUI48: EUI-48 Address. (Big-endian)
#'D': DATATYPE_DATA: Arbitrary Data. (See section 7.3)
#'d': DATATYPE_DATA_WLEN: Arbitrary Data with Prepended Length. (See section 7.3)
#'U': DATATYPE_UTF8: Zero-terminated UTF8-encoded string.
#'t': DATATYPE_STRUCT: Structured datatype. Compound type. Length prepended. (See section 7.4)
#'A': DATATYPE_ARRAY: Array of datatypes. Compound type. (See section 7.5)


class SpinelCodec(object):
    """ A general coder / decoder class for Spinel protocol. """

    @classmethod
    def parse_b(cls, payload):
        return unpack("<B", payload[:1])[0]

    @classmethod
    def parse_c(cls, payload):
        return unpack("<b", payload[:1])[0]

    @classmethod
    def parse_C(cls, payload):
        return unpack("<B", payload[:1])[0]

    @classmethod
    def parse_s(cls, payload):
        return unpack("<h", payload[:2])[0]

    @classmethod
    def parse_S(cls, payload):
        return unpack("<H", payload[:2])[0]

    @classmethod
    def parse_l(cls, payload):
        return unpack("<l", payload[:4])[0]

    @classmethod
    def parse_L(cls, payload):
        return unpack("<L", payload[:4])[0]

    @classmethod
    def parse_X(cls, payload):
        return unpack("<Q", payload[:8])[0]

    @classmethod
    def parse_6(cls, payload):
        return payload[:16]

    @classmethod
    def parse_E(cls, payload):
        return payload[:8]

    @classmethod
    def parse_e(cls, payload):
        return payload[:6]

    @classmethod
    def parse_U(cls, payload):
        nullchar = b'\0'
        return payload[:payload.index(nullchar)].decode('utf-8')  # strip null

    @classmethod
    def parse_D(cls, payload):
        return payload

    @classmethod
    def parse_d(cls, payload):
        return payload[2:2 + unpack("<H", payload[:2])[0]]

    @classmethod
    def parse_i(cls, payload):
        """ Decode EXI integer format. """
        value = 0
        value_len = 0
        value_mul = 1

        while value_len < 4:
            byte = payload[value_len]

            value += (byte & 0x7F) * value_mul
            if byte < 0x80:
                break
            value_mul *= 0x80
            value_len += 1

        return (value, value_len + 1)

    @classmethod
    def parse_i_value(cls, payload):
        """ Decode value of EXI integer format. """
        return cls.parse_i(payload)[0]

    @classmethod
    def parse_i_len(cls, payload):
        """ Decode length of EXI integer format. """
        return cls.parse_i(payload)[1]

    @classmethod
    def index_of_ending_brace(cls, spinel_format, idx):
        """ Determines the index of the matching closing brace. """
        count = 1
        orig_idx = idx
        while count > 0 and idx < len(spinel_format) - 1:
            idx += 1
            if spinel_format[idx] == ')':
                count -= 1
            elif spinel_format[idx] == '(':
                count += 1
        if count != 0:
            raise ValueError('Unbalanced parenthesis in format string "' +
                             spinel_format + '", idx=' + str(idx))
        return idx

    @classmethod
    def parse_field(cls, payload, spinel_format):
        map_decode = {
            'b': cls.parse_b,
            'c': cls.parse_c,
            'C': cls.parse_C,
            's': cls.parse_s,
            'S': cls.parse_S,
            'L': cls.parse_L,
            'l': cls.parse_l,
            '6': cls.parse_6,
            'X': cls.parse_X,
            'E': cls.parse_E,
            'e': cls.parse_e,
            'U': cls.parse_U,
            'D': cls.parse_D,
            'd': cls.parse_d,
            'i': cls.parse_i_value,
        }
        try:
            decode = map_decode[spinel_format[0]]
        except KeyError:
            raise ValueError('Unknown format "' + spinel_format[0] + '"')
        return decode(payload)

    @classmethod
    def get_payload_size(cls, payload, spinel_format):
        map_lengths = {
            'b': 1,
            'c': 1,
            'C': 1,
            's': 2,
            'S': 2,
            'l': 4,
            'L': 4,
            '6': 16,
            'X': 8,
            'E': 8,
            'e': 6,
        }

        result = 0

        idx = 0
        while idx < len(spinel_format):
            format = spinel_format[idx]

            if format == 't':
                if spinel_format[idx + 1] != '(':
                    raise ValueError('Invalid structure format')

                struct_end = cls.index_of_ending_brace(spinel_format, idx + 1)

                result += 2 + cls.parse_S(payload[result:])
                idx = struct_end + 1

            elif format == 'd':
                result += 2 + cls.parse_S(payload[result:])
                idx += 1

            elif format == 'D' or format == 'A':
                if idx != len(spinel_format) - 1:
                    raise ValueError('Invalid type syntax for "' + format +
                                     '", must go at end of format string')
                result = len(payload)
                idx += 1

            elif format == 'U':
                result += payload[result:].index(0) + 1
                idx += 1

            elif format == 'i':
                result += cls.parse_i_len(payload[result:])
                idx += 1

            else:
                result += map_lengths[format]
                idx += 1

        return result

    @classmethod
    def parse_fields(cls, payload, spinel_format):
        result = []

        idx = 0
        while idx < len(spinel_format):
            format = spinel_format[idx]

            if format == 'A':
                if spinel_format[idx + 1] != '(':
                    raise ValueError('Invalid structure format')

                array_end = cls.index_of_ending_brace(spinel_format, idx + 1)

                array_format = spinel_format[idx + 2:array_end]

                array = []
                while len(payload):
                    array.append(cls.parse_fields(payload, array_format))
                    payload = payload[cls.
                                      get_payload_size(payload, array_format):]

                result.append(tuple(array))
                idx = array_end + 1

            elif format == 't':
                if spinel_format[idx + 1] != '(':
                    raise ValueError('Invalid structure format')

                struct_end = cls.index_of_ending_brace(spinel_format, idx + 1)

                struct_format = spinel_format[idx + 2:struct_end]
                struct_len = cls.parse_S(payload)

                result.append(
                    cls.parse_fields(payload[2:struct_len + 2], struct_format))
                payload = payload[struct_len + 2:]

                idx = struct_end + 1

            else:
                result.append(cls.parse_field(payload, format))
                payload = payload[cls.get_payload_size(payload, format):]

                idx += 1

        return tuple(result)

    @classmethod
    def encode_i(cls, data):
        """ Encode EXI integer format. """
        result = bytes()
        while True:
            value = data & 0x7F
            data >>= 7
            if data:
                value |= 0x80
            result = result + pack("<B", value)
            if not data:
                break
        return result

    @classmethod
    def encode_b(cls, value):
        return pack('B', value)

    @classmethod
    def encode_c(cls, value):
        return pack('<b', value)

    @classmethod
    def encode_C(cls, value):
        return pack('B', value)

    @classmethod
    def encode_s(cls, value):
        return pack('<h', value)

    @classmethod
    def encode_S(cls, value):
        return pack('<H', value)

    @classmethod
    def encode_l(cls, value):
        return pack('<l', value)

    @classmethod
    def encode_L(cls, value):
        return pack('<L', value)

    @classmethod
    def encode_X(cls, value):
        return pack('<Q', value)

    @classmethod
    def encode_6(cls, value):
        return value[:16]

    @classmethod
    def encode_E(cls, value):
        return value[:8]

    @classmethod
    def encode_e(cls, value):
        return value[:6]

    @classmethod
    def encode_U(cls, value):
        return value.encode('utf-8') + b'\0'

    @classmethod
    def encode_D(cls, value):
        return value

    @classmethod
    def encode_d(cls, value):
        return cls.encode_S(len(value)) + value

    @classmethod
    def encode_field(cls, code, value):
        map_encode = {
            'b': cls.encode_b,
            'c': cls.encode_c,
            'C': cls.encode_C,
            's': cls.encode_s,
            'S': cls.encode_S,
            'L': cls.encode_L,
            'l': cls.encode_l,
            '6': cls.encode_6,
            'X': cls.encode_X,
            'E': cls.encode_E,
            'e': cls.encode_e,
            'U': cls.encode_U,
            'D': cls.encode_D,
            'd': cls.encode_d,
            'i': cls.encode_i,
        }
        try:
            encode = map_encode[code]
        except KeyError:
            raise ValueError('Unknown format "' + code + '"')
        return encode(value)

    @classmethod
    def next_code(cls, spinel_format):
        if not spinel_format:
            raise ValueError('More fields than format codes')
        code = spinel_format[0]
        spinel_format = spinel_format[1:]
        # TODO: Handle T() and A()
        return code, spinel_format

    @classmethod
    def encode_fields(cls, spinel_format, *fields):
        packed = bytes()
        for field in fields:
            code, spinel_format = cls.next_code(spinel_format)
            packed += cls.encode_field(code, field)
        return packed

    @classmethod
    def encode_packet(cls,
                      command_id,
                      payload=bytes(),
                      tid=SPINEL.HEADER_DEFAULT):
        """ Encode the given payload as a Spinel frame. """
        header = pack(">B", tid)
        cmd = cls.encode_i(command_id)
        pkt = header + cmd + payload
        return pkt
