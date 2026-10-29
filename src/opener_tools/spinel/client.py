# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Asyncio Spinel client: TID allocation, request/reply matching, timeouts,
LAST_STATUS errors and fan-out of the unsolicited (TID 0) frames."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from opener_tools.transport import Transport, TransportClosed

from . import dectnrp_constants as nrp
from .codec import SpinelCodec
from .const import SPINEL

log = logging.getLogger(__name__)

# Header byte: FLG = 0b10, IID = 0, TID in the low nibble.
HEADER_FLAG = 0x80
HEADER_FLAG_MASK = 0xC0
HEADER_IID_MASK = 0x30
HEADER_TID_MASK = 0x0F
TID_COUNT = 15

# Requests waiting for a reply: the device holds
# CONFIG_DECTNRP_SPINEL_RX_BUFS (4 by default) frames from the host and drops
# the frames received while all are in use.
MAX_IN_FLIGHT = 4

# The device waits up to CONFIG_DECTNRP_SPINEL_RESET_TIMEOUT_MS (1 s by
# default) for the operations in flight before it answers CMD_RESET.
RESET_TIMEOUT = 2.0

# Commands followed by a property id.
PROP_COMMANDS = range(SPINEL.CMD_PROP_VALUE_GET, SPINEL.RSP_PROP_VALUE_REMOVED + 1)

STATUS_NAMES = {
    value: name
    for name, value in vars(SPINEL).items()
    if name.startswith("STATUS_") and "__" not in name
}


def status_name(status: int) -> str:
    if nrp.STATUS_ERRNO_BASE <= status <= nrp.STATUS_ERRNO_LAST:
        errno = status - nrp.STATUS_ERRNO_BASE
        return f"errno {errno}"
    name = STATUS_NAMES.get(status)
    if name is None:
        return f"status {status:#x}"
    return name


def is_reset_status(status: int) -> bool:
    return SPINEL.STATUS_RESET__BEGIN <= status < SPINEL.STATUS_RESET__END


@dataclass(frozen=True)
class SpinelFrame:
    tid: int
    cmd: int
    prop: int | None
    value: bytes

    @classmethod
    def parse(cls, data: bytes) -> "SpinelFrame":
        """Parse a received frame; raise ValueError if it is malformed."""
        if len(data) < 2:
            raise ValueError("frame too short")
        header = data[0]
        if header & HEADER_FLAG_MASK != HEADER_FLAG:
            raise ValueError(f"bad header {header:#04x}")
        if header & HEADER_IID_MASK != 0:
            raise ValueError(f"unsupported IID in header {header:#04x}")
        tid = header & HEADER_TID_MASK
        try:
            cmd, cmd_len = SpinelCodec.parse_i(data[1:])
            pos = 1 + cmd_len
            prop = None
            if cmd in PROP_COMMANDS:
                prop, prop_len = SpinelCodec.parse_i(data[pos:])
                pos += prop_len
        except IndexError:
            raise ValueError("truncated frame") from None
        return cls(tid, cmd, prop, data[pos:])

    def status(self) -> int | None:
        """The status of a LAST_STATUS frame, None for any other frame."""
        if self.cmd != SPINEL.RSP_PROP_VALUE_IS:
            return None
        if self.prop != SPINEL.PROP_LAST_STATUS:
            return None
        try:
            status, _ = SpinelCodec.parse_i(self.value)
        except IndexError:
            return SPINEL.STATUS_PARSE_ERROR
        return status


class SpinelError(Exception):
    """The device answered with a LAST_STATUS other than OK."""

    def __init__(self, status: int):
        self.status = status
        name = status_name(status)
        super().__init__(f"Spinel {name} ({status})")


class SpinelTimeout(TimeoutError):
    """The device did not answer in time."""


class DeviceReset(Exception):
    """The device reset while a request was in flight."""

    def __init__(self, status: int):
        self.status = status
        name = status_name(status)
        super().__init__(f"device reset: {name}")


class SpinelClient:
    """Spinel requests over a Transport.

    Requests use TIDs 1 to 15; a TID is not reused while its request is
    pending, nor for `timeout` seconds after it timed out, so a late reply
    cannot answer a newer request. At most `max_in_flight` requests wait for
    a reply at once; the others wait for a TID. Unsolicited frames (TID 0) go to every
    subscriber queue. An unsolicited reset fails the pending requests with
    DeviceReset and calls the on_reset callbacks.
    """

    def __init__(
        self, transport: Transport, timeout: float = 1.0, max_in_flight: int = MAX_IN_FLIGHT
    ):
        self.transport = transport
        self.timeout = timeout
        self.max_in_flight = max_in_flight
        self.on_reset: list[Callable[[int], None]] = []
        self._pending: dict[int, asyncio.Future[SpinelFrame]] = {}
        self._quarantine: dict[int, float] = {}
        self._next_tid = 1
        self._tid_released = asyncio.Condition()
        self._subscribers: list[asyncio.Queue[SpinelFrame]] = []
        self._reset_waiter: asyncio.Future[int] | None = None
        self._reader: asyncio.Task | None = None

    async def start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        self._fail_pending(TransportClosed("client closed"))

    async def request(self, cmd: int, prop: int | None = None, value: bytes = b"") -> SpinelFrame:
        """Send a command and return its reply.

        Raise SpinelError if the reply is a LAST_STATUS other than OK,
        SpinelTimeout if there is no reply, DeviceReset if the device resets.
        """
        tid = await self._acquire_tid()
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[tid] = future

        payload = b""
        if prop is not None:
            payload = SpinelCodec.encode_i(prop)
        frame = SpinelCodec.encode_packet(cmd, payload + value, tid=HEADER_FLAG | tid)

        try:
            await self.transport.send(frame)
            reply = await asyncio.wait_for(future, self.timeout)
        except TimeoutError:
            self._quarantine[tid] = loop.time() + self.timeout
            raise SpinelTimeout(f"no reply to command {cmd} (TID {tid})") from None
        finally:
            del self._pending[tid]
            async with self._tid_released:
                self._tid_released.notify()

        status = reply.status()
        if status is not None and status != SPINEL.STATUS_OK:
            raise SpinelError(status)
        return reply

    async def get(self, prop: int) -> bytes:
        reply = await self.request(SPINEL.CMD_PROP_VALUE_GET, prop)
        return self._check_prop(reply, prop)

    async def set(self, prop: int, value: bytes) -> bytes:
        reply = await self.request(SPINEL.CMD_PROP_VALUE_SET, prop, value)
        return self._check_prop(reply, prop)

    async def insert(self, prop: int, value: bytes) -> bytes:
        reply = await self.request(SPINEL.CMD_PROP_VALUE_INSERT, prop, value)
        return self._check_prop(reply, prop)

    async def reset(self, timeout: float = RESET_TIMEOUT) -> None:
        """Send CMD_RESET and wait for the RESET_* notification.

        The device answers with TID 0 whatever the request TID, so the
        command goes out with TID 0 too.
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._reset_waiter = future
        frame = SpinelCodec.encode_packet(SPINEL.CMD_RESET, tid=HEADER_FLAG)
        try:
            await self.transport.send(frame)
            await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise SpinelTimeout("no reset notification") from None
        finally:
            self._reset_waiter = None

    def subscribe(self) -> asyncio.Queue[SpinelFrame]:
        """Return a queue receiving every unsolicited (TID 0) frame."""
        queue: asyncio.Queue[SpinelFrame] = asyncio.Queue()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[SpinelFrame]) -> None:
        self._subscribers.remove(queue)

    @staticmethod
    def _check_prop(reply: SpinelFrame, prop: int) -> bytes:
        if reply.prop != prop:
            raise SpinelError(SPINEL.STATUS_PARSE_ERROR)
        return reply.value

    def _free_tid(self) -> int | None:
        if len(self._pending) >= self.max_in_flight:
            return None
        now = asyncio.get_running_loop().time()
        for _ in range(TID_COUNT):
            tid = self._next_tid
            self._next_tid = tid % TID_COUNT + 1
            if tid in self._pending:
                continue
            until = self._quarantine.get(tid)
            if until is not None:
                if now < until:
                    continue
                del self._quarantine[tid]
            return tid
        return None

    def _next_release(self) -> float | None:
        """Seconds until the first quarantined TID is free, None if none is."""
        if not self._quarantine:
            return None
        now = asyncio.get_running_loop().time()
        first = min(self._quarantine.values())
        return max(first - now, 0.0)

    async def _acquire_tid(self) -> int:
        async with self._tid_released:
            tid = self._free_tid()
            while tid is None:
                delay = self._next_release()
                try:
                    await asyncio.wait_for(self._tid_released.wait(), delay)
                except TimeoutError:
                    pass
                tid = self._free_tid()
            return tid

    async def _read_loop(self) -> None:
        try:
            while True:
                data = await self.transport.recv()
                self._dispatch(data)
        except TransportClosed as exc:
            self._fail_pending(exc)

    def _dispatch(self, data: bytes) -> None:
        try:
            frame = SpinelFrame.parse(data)
        except ValueError as exc:
            log.warning("dropped frame %s: %s", data.hex(), exc)
            return

        if frame.tid != 0:
            future = self._pending.get(frame.tid)
            if future is None or future.done():
                log.debug("dropped stale reply on TID %d", frame.tid)
                return
            future.set_result(frame)
            return

        status = frame.status()
        if status is not None and is_reset_status(status):
            self._on_device_reset(status)
        for queue in self._subscribers:
            queue.put_nowait(frame)

    def _on_device_reset(self, status: int) -> None:
        self._fail_pending(DeviceReset(status))
        waiter = self._reset_waiter
        if waiter is not None and not waiter.done():
            waiter.set_result(status)
            return
        for callback in self.on_reset:
            try:
                callback(status)
            except Exception:
                log.exception("on_reset callback failed")

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
