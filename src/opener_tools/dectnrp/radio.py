# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Driver-level DECT NR+ radio control over Spinel."""

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from opener_tools.spinel import SPINEL, SpinelCodec
from opener_tools.spinel import dectnrp_constants as nrp
from opener_tools.spinel.client import (
    DeviceReset,
    SpinelClient,
    SpinelError,
    SpinelFrame,
    is_reset_status,
)
from opener_tools.spinel.dectnrp_constants import Reception
from opener_tools.transport import TransportClosed

from .events import Event, OpFinished, decode_event
from .timing import Timing

log = logging.getLogger(__name__)

TOKEN_MAX = 0xFFFF


@dataclass
class DrvCaps:
    """DRV_CAPS, in FMT_DRV_CAPS order; latencies in modem ticks."""

    hw_caps: int
    release: int
    power_class: int
    nss_rx: int
    diversity: int
    rx_gain_db: int
    mcs_max: int
    soft_buffer: int
    harq_max: int
    harq_delay: int
    mu_max: int
    beta_max: int
    ticks_per_symbol: int
    max_frame: int
    op_slots: int
    sched_op_transition: int
    sched_op_startup: int
    rx_startup: int
    rx_shutdown: int
    rssi_shutdown: int
    tx_startup: int
    tx_shutdown: int
    stop_to_rf_off: int

    @property
    def timing(self) -> Timing:
        return Timing(self.ticks_per_symbol)


@dataclass
class DrvStats:
    ops_accepted: int
    ops_rejected: int
    events_sent: int
    events_dropped: int
    frames_rx: int
    frames_rejected: int
    transport_errors: int


@dataclass
class TxParams:
    phy_header_type: int
    tbs: int
    mu: int
    beta: int
    mcs: int
    slots: int
    lbt_period: int = 0
    lbt_rssi_threshold: int = 0


class Op:
    """A scheduled driver operation.

    events collects the events of the operation as they arrive. Awaiting the
    Op returns its OpFinished, or raises DeviceReset if the device reset first.
    """

    def __init__(self, token: int):
        self.token = token
        self.events: list[Event] = []
        loop = asyncio.get_running_loop()
        self._finished: asyncio.Future[OpFinished] = loop.create_future()

    @property
    def done(self) -> bool:
        return self._finished.done()

    def __await__(self):
        return self._finished.__await__()

    def _add(self, event: Event) -> None:
        self.events.append(event)
        if isinstance(event, OpFinished):
            self._finished.set_result(event)

    def _fail(self, exc: Exception) -> None:
        if not self._finished.done():
            self._finished.set_exception(exc)
            # Nobody may await an Op dropped by its owner.
            self._finished.exception()


class Radio:
    """Driver-level API: properties, scheduled operations and their events.

    It must be created with the event loop running: it reads the client's
    unsolicited frames from a task, routes each event to its Op and to the
    events() streams, and fails the pending Ops when the device resets. Reset
    notifications come in the same stream as the events, so the events sent
    before a reset reach their Op first. When the transport is lost, the
    pending Ops and the events() streams raise TransportClosed.
    """

    def __init__(self, client: SpinelClient):
        self.client = client
        self.caps: DrvCaps | None = None
        self._ops: dict[int, Op] = {}
        self._next_token = 1
        self._streams: list[asyncio.Queue[Event | TransportClosed]] = []
        self._frames = client.subscribe()
        self._reset_seen = asyncio.Event()
        self._router = asyncio.create_task(self._route())
        client.on_close.append(self._on_close)

    async def close(self) -> None:
        self._router.cancel()
        try:
            await self._router
        except asyncio.CancelledError:
            pass
        self.client.unsubscribe(self._frames)
        self.client.on_close.remove(self._on_close)

    async def init(self) -> DrvCaps:
        """Check the device identity and read its capabilities."""
        value = await self.client.get(SPINEL.PROP_INTERFACE_TYPE)
        interface_type, _ = SpinelCodec.parse_i(value)
        if interface_type != nrp.INTERFACE_TYPE:
            raise RuntimeError(f"not a DECT NR+ device: interface type {interface_type:#x}")

        value = await self.client.get(SPINEL.PROP_CAPS)
        caps = SpinelCodec.parse_fields(value, "A(i)")
        cap_values = [cap for (cap,) in caps[0]]
        if nrp.CAP_DRV not in cap_values:
            raise RuntimeError("device does not expose the driver level")

        value = await self.client.get(nrp.PROP_DRV_CAPS)
        fields = SpinelCodec.parse_fields(value, nrp.FMT_DRV_CAPS)
        self.caps = DrvCaps(*fields)
        return self.caps

    async def reset(self) -> None:
        """Reset the Spinel interface; the pending Ops fail with DeviceReset."""
        self._reset_seen.clear()
        await self.client.reset()
        await self._reset_seen.wait()

    async def enable(self, on: bool = True) -> None:
        value = SpinelCodec.encode_fields("b", on)
        await self.client.set(nrp.PROP_DRV_ENABLED, value)

    async def time(self) -> int:
        value = await self.client.get(nrp.PROP_DRV_TIME)
        (ticks,) = SpinelCodec.parse_fields(value, "X")
        return ticks

    async def set_network_id(self, nwid: int) -> None:
        value = SpinelCodec.encode_fields("L", nwid)
        await self.client.set(nrp.PROP_DRV_NETWORK_ID, value)

    async def set_filter_short_nwid(self, active: bool, value: int = 0) -> None:
        encoded = SpinelCodec.encode_fields("bC", active, value)
        await self.client.set(nrp.PROP_DRV_FILTER_SHORT_NWID, encoded)

    async def set_filter_short_rxaddr(self, active: bool, value: int = 0) -> None:
        encoded = SpinelCodec.encode_fields("bS", active, value)
        await self.client.set(nrp.PROP_DRV_FILTER_SHORT_RXADDR, encoded)

    async def stats(self) -> DrvStats:
        value = await self.client.get(nrp.PROP_DRV_STATS)
        fields = SpinelCodec.parse_fields(value, nrp.FMT_DRV_STATS)
        return DrvStats(*fields)

    async def schedule_rx(
        self,
        channel: int,
        duration: int,
        start: int | None = None,
        expected_rssi: int = 0,
        beacon: bool = False,
        reception: Reception = Reception.SINGLE_SHOT,
    ) -> Op:
        """Schedule an RX of duration modem ticks; start None means now."""
        flags = reception << nrp.OP_FLAG_RX_RECEPTION_SHIFT
        if beacon:
            flags |= nrp.OP_FLAG_BEACON
        suffix = SpinelCodec.encode_fields(nrp.FMT_OP_RX, duration, expected_rssi)
        return await self._schedule(nrp.OP_RX, channel, flags, start, suffix)

    async def schedule_tx(
        self,
        channel: int,
        data: bytes,
        params: TxParams,
        start: int | None = None,
        lbt: bool = False,
        harq: bool = False,
        beacon: bool = False,
    ) -> Op:
        """Schedule a TX of data, the physical header followed by the payload."""
        flags = 0
        if lbt:
            flags |= nrp.OP_FLAG_LBT
        if harq:
            flags |= nrp.OP_FLAG_HARQ
        if beacon:
            flags |= nrp.OP_FLAG_BEACON
        suffix = SpinelCodec.encode_fields(
            nrp.FMT_OP_TX,
            params.phy_header_type,
            params.tbs,
            params.mu,
            params.beta,
            params.mcs,
            params.slots,
            params.lbt_period,
            params.lbt_rssi_threshold,
            data,
        )
        return await self._schedule(nrp.OP_TX, channel, flags, start, suffix)

    async def schedule_rssi1(self, channel: int, subslots: int, start: int | None = None) -> Op:
        suffix = SpinelCodec.encode_fields(nrp.FMT_OP_RSSI1, subslots)
        return await self._schedule(nrp.OP_RSSI1, channel, 0, start, suffix)

    def events(self) -> AsyncIterator[Event]:
        """Return a stream of every event, including the ones of the Ops.

        The stream receives the events from this call on.
        """
        queue: asyncio.Queue[Event | TransportClosed] = asyncio.Queue()
        self._streams.append(queue)
        return self._stream(queue)

    async def _stream(self, queue: asyncio.Queue[Event | TransportClosed]) -> AsyncIterator[Event]:
        try:
            while True:
                item = await queue.get()
                if isinstance(item, TransportClosed):
                    raise item
                yield item
        finally:
            self._streams.remove(queue)

    def _new_token(self) -> int:
        for _ in range(TOKEN_MAX):
            token = self._next_token
            self._next_token = token % TOKEN_MAX + 1
            if token not in self._ops:
                return token
        raise RuntimeError("all operation tokens in flight")

    async def _schedule(
        self, op_type: int, channel: int, flags: int, start: int | None, suffix: bytes
    ) -> Op:
        start_time = 0
        if start is not None:
            flags |= nrp.OP_FLAG_SCHEDULED
            start_time = start
        token = self._new_token()
        prefix = SpinelCodec.encode_fields(
            nrp.FMT_OP_PREFIX, token, op_type, channel, flags, start_time
        )

        # Registered before the insert: the device may send events for the
        # token before its INSERTED reply.
        op = Op(token)
        self._ops[token] = op
        try:
            reply = await self.client.insert(nrp.PROP_DRV_OP_QUEUE, prefix + suffix)
        except Exception:
            self._ops.pop(token, None)
            raise

        (accepted,) = SpinelCodec.parse_fields(reply, "S")
        if accepted != token:
            self._ops.pop(token, None)
            raise SpinelError(SPINEL.STATUS_PARSE_ERROR)
        return op

    async def _route(self) -> None:
        while True:
            frame = await self._frames.get()
            self._route_frame(frame)

    def _route_frame(self, frame: SpinelFrame) -> None:
        status = frame.status()
        if status is not None and is_reset_status(status):
            self._fail_ops(DeviceReset(status))
            self._reset_seen.set()
            return
        if frame.cmd != SPINEL.RSP_PROP_VALUE_IS or frame.prop != nrp.PROP_DRV_EVENT:
            return

        try:
            event = decode_event(frame.value)
        except ValueError as exc:
            log.warning("dropped event %s: %s", frame.value.hex(), exc)
            return

        op = self._ops.get(event.token)
        if op is not None:
            if isinstance(event, OpFinished):
                del self._ops[event.token]
            op._add(event)
        for queue in self._streams:
            queue.put_nowait(event)

    def _on_close(self, exc: TransportClosed) -> None:
        self._fail_ops(exc)
        for queue in self._streams:
            queue.put_nowait(exc)

    def _fail_ops(self, exc: Exception) -> None:
        ops = list(self._ops.values())
        self._ops.clear()
        for op in ops:
            op._fail(exc)
