# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Test doubles."""

import asyncio
import dataclasses
from collections.abc import AsyncIterator, Callable

from opener_tools.dectnrp import DrvCaps, Event, Op, OpFinished
from opener_tools.spinel import SPINEL, SpinelCodec
from opener_tools.spinel import dectnrp_constants as nrp
from opener_tools.spinel.client import DeviceReset, SpinelError
from opener_tools.spinel.dectnrp_constants import Reception
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

    def inject_closed(self) -> None:
        """Make recv() raise TransportClosed, as when the device is unplugged."""
        self._received.put_nowait(None)

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


def make_caps(**fields: int) -> DrvCaps:
    """DrvCaps with every field 0 but ticks_per_symbol 2880 and op_slots 8,
    overridden by fields."""
    values = {field.name: 0 for field in dataclasses.fields(DrvCaps)}
    values["ticks_per_symbol"] = 2880
    values["op_slots"] = 8
    values.update(fields)
    return DrvCaps(**values)


class FakeRadio:
    """Radio double with a virtual modem clock the test advances.

    schedule_rx records (op, start, duration, reception) in scheduled and
    returns an Op the test feeds with emit() and finish(). It raises
    reject_error (a SpinelError by default) while reject is non-zero,
    decrementing it.
    """

    def __init__(self, caps: DrvCaps):
        self.caps = caps
        self.clock = 0
        self.reject = 0
        self.reject_error: Exception = SpinelError(SPINEL.STATUS_FAILURE)
        self.scheduled: list[tuple[Op, int | None, int, Reception]] = []
        self._next_token = 1
        self._pending: dict[int, Op] = {}
        self._streams: list[asyncio.Queue[Event]] = []

    async def time(self) -> int:
        return self.clock

    async def schedule_rx(
        self,
        channel: int,
        duration: int,
        start: int | None = None,
        reception: Reception = Reception.SINGLE_SHOT,
    ) -> Op:
        if self.reject > 0:
            self.reject -= 1
            raise self.reject_error
        token = self._next_token
        self._next_token += 1
        op = Op(token)
        self._pending[token] = op
        self.scheduled.append((op, start, duration, reception))
        return op

    def events(self) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._streams.append(queue)
        return self._stream(queue)

    def emit(self, event: Event) -> None:
        op = self._pending.get(event.token)
        if op is not None:
            if isinstance(event, OpFinished):
                del self._pending[event.token]
            op._add(event)
        for queue in self._streams:
            queue.put_nowait(event)

    def finish(self, op: Op, status: int = 0) -> None:
        self.emit(OpFinished(op.token, status, 0))

    def device_reset(self, status: int = SPINEL.STATUS_RESET_POWER_ON) -> None:
        ops = list(self._pending.values())
        self._pending.clear()
        for op in ops:
            op._fail(DeviceReset(status))

    async def _stream(self, queue: asyncio.Queue[Event]) -> AsyncIterator[Event]:
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            self._streams.remove(queue)


class SniffDevice(ScriptedDevice):
    """ScriptedDevice whose RX operations run on a modem clock that follows
    the host clock.

    The device answers INTERFACE_TYPE, CAPS and DRV_CAPS (caps), and DRV_TIME
    from its clock. An inserted operation starts at its start time (now if
    unscheduled) and emits there the next frame of script, if any: script
    holds functions returning a frame for a token. It sends OP_FINISHED at its
    end. requests logs every request as (cmd, prop, value), prop None for
    RESET. after_insert(count), if set, is called with the number of inserts
    so far before each INSERTED reply.
    """

    def __init__(self, transport: FakeTransport, caps: DrvCaps):
        super().__init__(transport)
        self.caps = caps
        self.requests: list[tuple[int, int | None, bytes]] = []
        self.ops: list[tuple[int, int, int, int]] = []
        self.script: list[Callable[[int], bytes]] = []
        self.emitted = 0
        self.after_insert: Callable[[int], None] | None = None
        self.running_at_disable: int | None = None
        self._watches: list[list] = []
        self._timers: dict[int, list[asyncio.TimerHandle]] = {}
        self._origin = 0.0
        self._set_defaults()

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._origin = loop.time()
        super().start()

    async def stop(self) -> None:
        self._abort_ops()
        await super().stop()

    def clock(self) -> int:
        loop = asyncio.get_running_loop()
        elapsed = loop.time() - self._origin
        ticks_per_s = self.caps.timing.ticks_per_ms * 1000
        return round(elapsed * ticks_per_s)

    def when_written(self, n: int, after: int, callback: Callable[[], None]) -> None:
        """Call callback once the host has handled the n-th emitted frame: at
        the after-th insert following its emission. With after set to the
        pipeline depth, the windows queued before it have all been replaced."""
        self._watches.append([n, after, callback, 0])

    def power_on_reset(self) -> None:
        """Reboot: abort the operations, forget the settings, notify."""
        self._abort_ops()
        self._set_defaults()
        self.transport.inject(status_frame(0, SPINEL.STATUS_RESET_POWER_ON))

    def _set_defaults(self) -> None:
        caps_fields = dataclasses.astuple(self.caps)
        self.props = {
            SPINEL.PROP_INTERFACE_TYPE: SpinelCodec.encode_i(nrp.INTERFACE_TYPE),
            SPINEL.PROP_CAPS: SpinelCodec.encode_i(nrp.CAP_DRV),
            nrp.PROP_DRV_CAPS: SpinelCodec.encode_fields(nrp.FMT_DRV_CAPS, *caps_fields),
        }

    def _answer(self, frame: bytes) -> None:
        cmd, cmd_len = SpinelCodec.parse_i(frame[1:])
        prop = None
        value = b""
        if cmd != SPINEL.CMD_RESET:
            prop, prop_len = SpinelCodec.parse_i(frame[1 + cmd_len :])
            value = frame[1 + cmd_len + prop_len :]
        self.requests.append((cmd, prop, value))

        if cmd == SPINEL.CMD_RESET:
            self._abort_ops()
        if cmd == SPINEL.CMD_PROP_VALUE_GET and prop == nrp.PROP_DRV_TIME:
            now = self.clock()
            self.props[nrp.PROP_DRV_TIME] = SpinelCodec.encode_fields("X", now)
        if cmd == SPINEL.CMD_PROP_VALUE_SET and prop == nrp.PROP_DRV_ENABLED:
            if value == b"\x00":
                self.running_at_disable = len(self._timers)
        super()._answer(frame)

    def _answer_insert(self, tid: int, prop: int, value: bytes) -> None:
        self.inserts.append(value)
        fields = SpinelCodec.parse_fields(value, nrp.FMT_OP_PREFIX + nrp.FMT_OP_RX)
        token, _, _, flags, start, duration, _ = fields
        self.ops.append((token, flags, start, duration))
        self._check_watches()
        if self.after_insert is not None:
            self.after_insert(len(self.inserts))

        reply = SpinelCodec.encode_fields("S", token)
        self.transport.inject(make_frame(tid, SPINEL.RSP_PROP_VALUE_INSERTED, prop, reply))
        self._run_op(token, start, duration)

    def _check_watches(self) -> None:
        for watch in list(self._watches):
            n, after, callback, seen = watch
            if self.emitted < n:
                continue
            watch[3] = seen + 1
            if watch[3] == after:
                self._watches.remove(watch)
                callback()

    def _run_op(self, token: int, start: int, duration: int) -> None:
        loop = asyncio.get_running_loop()
        now = self.clock()
        ticks_per_s = self.caps.timing.ticks_per_ms * 1000
        start_delay = max(start - now, 0) / ticks_per_s
        end_delay = max(start + duration - now, 0) / ticks_per_s
        emit = loop.call_later(start_delay, self._emit, token)
        finish = loop.call_later(end_delay, self._finish, token)
        self._timers[token] = [emit, finish]

    def _emit(self, token: int) -> None:
        if not self.script:
            return
        make_event = self.script.pop(0)
        frame = make_event(token)
        self.transport.inject(frame)
        self.emitted += 1

    def _finish(self, token: int) -> None:
        del self._timers[token]
        frame = event_frame(token, nrp.EVENT_OP_FINISHED)
        self.transport.inject(frame)

    def _abort_ops(self) -> None:
        for handles in self._timers.values():
            for handle in handles:
                handle.cancel()
        self._timers.clear()


class DeviceTransport(FakeTransport):
    """FakeTransport that starts its device when opened and stops it when
    closed, for code that opens the transport itself."""

    def __init__(self):
        super().__init__()
        self.device: ScriptedDevice | None = None

    async def open(self) -> None:
        await super().open()
        self.device.start()

    async def close(self) -> None:
        await super().close()
        await self.device.stop()
