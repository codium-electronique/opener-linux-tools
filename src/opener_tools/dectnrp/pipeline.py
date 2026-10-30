# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Gapless scheduled reception: keeps depth RX windows queued on the modem."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from time import monotonic

from opener_tools.spinel.client import SpinelError, SpinelTimeout
from opener_tools.spinel.dectnrp_constants import Reception

from .events import Event, MsgError, MsgReceived, OpFinished
from .radio import Op, Radio

# Shorter fill windows are not worth an operation.
FILL_MIN_MS = 1.0
# The host and modem clocks drift apart (tens of ppm): the modem time is read
# again when the last reading is older than this.
ANCHOR_MAX_AGE_S = 1.0


@dataclass(frozen=True)
class Gap:
    """Modem time not covered by a window: start and length in modem ticks."""

    start: int
    length: int
    reason: str


@dataclass
class PipelineStats:
    windows: int = 0
    early_finishes: int = 0
    fills: int = 0
    fill_rejects: int = 0
    gaps: int = 0
    gap_ticks: int = 0
    dropped_events: int = 0


def _retrieve_exception(task: asyncio.Future) -> None:
    # Every window fails on a device reset; the pipeline raises one error.
    if not task.cancelled():
        task.exception()


@dataclass
class _Window:
    op: Op
    task: asyncio.Future
    start: int
    fill: bool = field(default=False)
    # A frame, received or failing its PDC CRC, ended the window.
    received: bool = field(default=False)


class RxPipeline:
    """Iterating the pipeline schedules depth RX windows, back to back, and
    one more each time a window finishes. It yields the MsgReceived and
    MsgError events of its windows in arrival order, and a Gap each time it
    has to skip ahead: when the next window would start too late
    ("resync"), or when the device rejects it or does not answer
    ("rejected"). A window whose operation fails on the device (start time
    late, scheduling conflict...) is yielded as a Gap too ("failed, status
    N"). A DeviceReset or a lost transport ends the iteration.

    Consecutive windows are separated by the device's sched_op_transition,
    since the modem rejects exactly adjacent windows (1 ms if unknown).

    In single-shot mode a frame ends its window early. The pipeline then
    schedules a fill window over the rest of it: from lead after now up to
    the first queued window, if that leaves FILL_MIN_MS at least. A fill
    ended by a frame gets a fill in turn; fills do not extend the queue.
    One operation slot is kept for them.
    """

    def __init__(
        self,
        radio: Radio,
        channel: int,
        window_ms: float,
        depth: int = 4,
        lead_ms: float = 20,
        reception: Reception = Reception.SINGLE_SHOT,
    ):
        caps = radio.caps
        if depth >= caps.op_slots:
            raise ValueError(
                f"depth {depth} leaves no fill slot in the {caps.op_slots} operation slots"
            )
        timing = caps.timing
        self.radio = radio
        self.channel = channel
        self.depth = depth
        self.reception = reception
        self.stats = PipelineStats()
        self._ticks_per_s = timing.ticks_per_ms * 1000
        self._window = timing.ms_to_ticks(window_ms)
        lead = timing.ms_to_ticks(lead_ms)
        self._lead = max(lead, caps.sched_op_startup + caps.rx_startup)
        transition = caps.sched_op_transition
        if transition == 0:
            transition = timing.ms_to_ticks(1)
        self._transition = transition
        self._spacing = self._window + transition
        self._fill_min = timing.ms_to_ticks(FILL_MIN_MS)
        self._next_start = 0
        self._modem_anchor = 0
        self._host_anchor = 0.0
        self._windows: dict[int, _Window] = {}
        self._stopping = False
        self._schedule_lock = asyncio.Lock()

    def __aiter__(self) -> AsyncIterator[MsgReceived | MsgError | Gap]:
        return self._run()

    async def stop(self) -> None:
        """Schedule no more windows and wait until the queued ones finish."""
        self._stopping = True
        # A window being inserted is in _windows once the lock is free.
        async with self._schedule_lock:
            pass
        while True:
            pending = [window.task for window in self._windows.values() if not window.task.done()]
            if not pending:
                return
            await asyncio.wait(pending)

    async def _run(self) -> AsyncIterator[MsgReceived | MsgError | Gap]:
        # Subscribed first: a window may raise events before its insert
        # returns.
        stream = self.radio.events()
        next_event = asyncio.ensure_future(anext(stream))
        try:
            await self._sync()
            self._next_start = self._modem_anchor + self._lead
            for _ in range(self.depth):
                gaps = await self._schedule_next()
                for gap in gaps:
                    yield gap

            while self._windows:
                event = await self._wait_event(next_event)
                next_event = asyncio.ensure_future(anext(stream))
                # Drops are reported on the next event sent, whatever its op.
                self.stats.dropped_events += event.dropped
                window = self._windows.get(event.token)
                if window is None:
                    continue
                if isinstance(event, OpFinished):
                    del self._windows[event.token]
                    single_shot = self.reception == Reception.SINGLE_SHOT
                    if window.received and single_shot:
                        self.stats.early_finishes += 1
                        await self._schedule_fill()
                    failed = event.status != 0
                    if window.fill:
                        if failed:
                            self.stats.fill_rejects += 1
                        continue
                    if failed:
                        yield self._failed(window, event.status)
                    gaps = await self._schedule_next()
                    for gap in gaps:
                        yield gap
                    continue
                if isinstance(event, MsgReceived | MsgError):
                    window.received = True
                if isinstance(event, MsgReceived | MsgError):
                    yield event
        finally:
            # The pending read runs the stream: end it before closing it.
            next_event.cancel()
            await asyncio.wait({next_event})
            await stream.aclose()

    async def _wait_event(self, next_event: asyncio.Future) -> Event:
        """Return the next event; raise the error of a failed window
        (DeviceReset)."""
        while True:
            waiting = {next_event}
            for window in self._windows.values():
                if not window.task.done():
                    waiting.add(window.task)
            done, _ = await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
            for window in self._windows.values():
                if window.task in done:
                    exc = window.task.exception()
                    if exc is not None:
                        raise exc
            if next_event.done():
                return next_event.result()

    async def _sync(self) -> None:
        self._modem_anchor = await self.radio.time()
        self._host_anchor = monotonic()

    async def _now_estimate(self) -> int:
        elapsed = monotonic() - self._host_anchor
        if elapsed > ANCHOR_MAX_AGE_S:
            await self._sync()
            elapsed = 0.0
        return self._modem_anchor + round(elapsed * self._ticks_per_s)

    def _failed(self, window: _Window, status: int) -> Gap:
        gap = Gap(window.start, self._window, f"failed, status {status}")
        self.stats.gaps += 1
        self.stats.gap_ticks += gap.length
        return gap

    async def _resync(self, reason: str, earliest: int) -> Gap:
        """Move the next start to now + lead, or to earliest if later."""
        await self._sync()
        new_start = max(self._modem_anchor + self._lead, earliest)
        gap = Gap(self._next_start, new_start - self._next_start, reason)
        self.stats.gaps += 1
        self.stats.gap_ticks += gap.length
        self._next_start = new_start
        return gap

    async def _schedule_next(self) -> list[Gap]:
        """Schedule a window at the next start, resynchronising first when it
        is too late, and once more when the device rejects it; return the
        gaps of the resyncs."""
        gaps = []
        async with self._schedule_lock:
            if self._stopping:
                return gaps
            now = await self._now_estimate()
            deadline = now + self._lead // 2
            if self._next_start < deadline:
                gap = await self._resync("resync", self._next_start)
                gaps.append(gap)
            try:
                op = await self._insert()
            except (SpinelError, SpinelTimeout):
                # Skip the rejected window: an earlier start would overlap
                # the windows already queued. Without a reply, the device may
                # run it: its events have an unknown token.
                skipped = self._next_start + self._spacing
                gap = await self._resync("rejected", skipped)
                gaps.append(gap)
                op = await self._insert()
            self._add_window(op, self._next_start, fill=False)
            self._next_start += self._spacing
        return gaps

    async def _schedule_fill(self) -> None:
        """Schedule a window from lead after now up to the first queued one."""
        async with self._schedule_lock:
            now = await self._now_estimate()
            start = now + self._lead
            # The next window is either queued or the next one to queue;
            # when stopping, only a queued one, so the drain is no longer. A
            # queued window starting before the fill would has started or is
            # about to: it covers that time, a fill would overlap it.
            ends = [window.start for window in self._windows.values()]
            if not self._stopping:
                ends.append(self._next_start)
            if not ends:
                return
            end = min(ends)
            duration = end - self._transition - start
            if duration < self._fill_min:
                return
            try:
                op = await self.radio.schedule_rx(
                    self.channel, duration, start=start, reception=self.reception
                )
            except (SpinelError, SpinelTimeout):
                # The rest of the window stays uncovered, as without fills.
                self.stats.fill_rejects += 1
                return
            self._add_window(op, start, fill=True)
            self.stats.fills += 1

    def _add_window(self, op: Op, start: int, fill: bool) -> None:
        task = asyncio.ensure_future(op)
        task.add_done_callback(_retrieve_exception)
        self._windows[op.token] = _Window(op, task, start, fill)
        self.stats.windows += 1

    async def _insert(self) -> Op:
        return await self.radio.schedule_rx(
            self.channel, self._window, start=self._next_start, reception=self.reception
        )
