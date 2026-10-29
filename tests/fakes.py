# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Test doubles."""

import asyncio
from collections.abc import Callable

from opener_tools.spinel import SPINEL, SpinelCodec
from opener_tools.spinel import dectnrp_constants as nrp
from opener_tools.transport import Transport, TransportClosed


class FakeTransport(Transport):
    """In-memory transport: frames sent by the client land in sent, frames
    passed to inject() are what recv() returns."""

    def __init__(self):
        self.sent: asyncio.Queue[bytes] = asyncio.Queue()
        self._received: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.closed = False

    def inject(self, frame: bytes) -> None:
        self._received.put_nowait(frame)

    async def open(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        self._received.put_nowait(None)

    async def send(self, frame: bytes) -> None:
        if self.closed:
            raise TransportClosed("fake transport closed")
        self.sent.put_nowait(frame)

    async def recv(self) -> bytes:
        frame = await self._received.get()
        if frame is None:
            self._received.put_nowait(None)
            raise TransportClosed("fake transport closed")
        return frame


def make_frame(tid: int, cmd: int, prop: int, value: bytes = b"") -> bytes:
    header = 0x80 | tid
    payload = SpinelCodec.encode_i(prop) + value
    return SpinelCodec.encode_packet(cmd, payload, tid=header)


def status_frame(tid: int, status: int) -> bytes:
    value = SpinelCodec.encode_i(status)
    return make_frame(tid, SPINEL.RSP_PROP_VALUE_IS, SPINEL.PROP_LAST_STATUS, value)


def event_frame(
    token: int, code: int, suffix: bytes = b"", status: int = 0, dropped: int = 0
) -> bytes:
    prefix = SpinelCodec.encode_fields(nrp.FMT_EVT_PREFIX, token, code, status, dropped)
    return make_frame(0, SPINEL.RSP_PROP_VALUE_IS, nrp.PROP_DRV_EVENT, prefix + suffix)


class ScriptedDevice:
    """Answers the requests sent over a FakeTransport.

    GET returns props[prop] and SET stores the value. INSERT records the value
    in inserts, injects the frames on_insert(token) returns, then replies
    INSERTED, or LAST_STATUS insert_status when it is set.
    """

    def __init__(self, transport: FakeTransport):
        self.transport = transport
        self.props: dict[int, bytes] = {}
        self.inserts: list[bytes] = []
        self.insert_status: int | None = None
        self.on_insert: Callable[[int], list[bytes]] | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._serve())

    async def stop(self) -> None:
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    def inject_event(self, token: int, code: int, suffix: bytes = b"", status: int = 0) -> None:
        frame = event_frame(token, code, suffix, status)
        self.transport.inject(frame)

    async def _serve(self) -> None:
        while True:
            frame = await self.transport.sent.get()
            self._answer(frame)

    def _answer(self, frame: bytes) -> None:
        tid = frame[0] & 0x0F
        cmd, cmd_len = SpinelCodec.parse_i(frame[1:])
        if cmd == SPINEL.CMD_RESET:
            self.transport.inject(status_frame(0, SPINEL.STATUS_RESET_SOFTWARE))
            return
        prop, prop_len = SpinelCodec.parse_i(frame[1 + cmd_len :])
        value = frame[1 + cmd_len + prop_len :]

        if cmd == SPINEL.CMD_PROP_VALUE_GET:
            stored = self.props.get(prop)
            if stored is None:
                self.transport.inject(status_frame(tid, SPINEL.STATUS_PROP_NOT_FOUND))
                return
            self.transport.inject(make_frame(tid, SPINEL.RSP_PROP_VALUE_IS, prop, stored))
        elif cmd == SPINEL.CMD_PROP_VALUE_SET:
            self.props[prop] = value
            self.transport.inject(make_frame(tid, SPINEL.RSP_PROP_VALUE_IS, prop, value))
        elif cmd == SPINEL.CMD_PROP_VALUE_INSERT:
            self._answer_insert(tid, prop, value)
        else:
            self.transport.inject(status_frame(tid, SPINEL.STATUS_INVALID_COMMAND))

    def _answer_insert(self, tid: int, prop: int, value: bytes) -> None:
        self.inserts.append(value)
        if self.insert_status is not None:
            self.transport.inject(status_frame(tid, self.insert_status))
            return
        (token,) = SpinelCodec.parse_fields(value, "S")
        if self.on_insert is not None:
            for frame in self.on_insert(token):
                self.transport.inject(frame)
        reply = SpinelCodec.encode_fields("S", token)
        self.transport.inject(make_frame(tid, SPINEL.RSP_PROP_VALUE_INSERTED, prop, reply))
