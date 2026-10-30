# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""nrp-sniffer: capture DECT NR+ frames into a DECT-NR-TAP pcap, from the
command line or as a Wireshark extcap."""

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import stat
import sys
from dataclasses import dataclass
from importlib.metadata import version
from typing import BinaryIO

from opener_tools.capture import PcapWriter, build_tap_record, extcap
from opener_tools.dectnrp import Gap, MsgError, Radio, RxPipeline
from opener_tools.spinel.client import DeviceReset, SpinelClient
from opener_tools.spinel.dectnrp_constants import Reception
from opener_tools.transport import open_transport

# Single-shot is the only mode in which the nRF91 receives every frame
# format; a received frame ends the window, short windows bound the time
# left uncovered.
DEFAULT_RX_MODE = Reception.SINGLE_SHOT
DEFAULT_WINDOW_MS = 20.0
# The dectnrp-driver sample values.
DEFAULT_CHANNEL = 1677
DEFAULT_NETWORK_ID = 0x32545279
DEFAULT_DEPTH = 4
DEFAULT_LEAD_MS = 20.0

RX_MODES = {
    "single": Reception.SINGLE_SHOT,
    "semi": Reception.SEMICONTINUOUS,
    "continuous": Reception.CONTINUOUS,
}
RX_MODE_NAMES = {mode: name for name, mode in RX_MODES.items()}
RX_MODE_DISPLAY = {"single": "single-shot", "semi": "semicontinuous", "continuous": "continuous"}


@dataclass
class SnifferStats:
    records: int = 0
    crc_errors: int = 0
    dropped_events: int = 0
    gaps: int = 0
    gap_ms: float = 0.0
    resets: int = 0


class _Sniffer:
    def __init__(
        self,
        radio: Radio,
        writer: PcapWriter,
        channel: int,
        network_id: int,
        reception: Reception,
        window_ms: float,
        depth: int,
        lead_ms: float,
        stop: asyncio.Event,
        log: logging.Logger,
    ):
        self.radio = radio
        self.writer = writer
        self.channel = channel
        self.network_id = network_id
        self.reception = reception
        self.window_ms = window_ms
        self.depth = depth
        self.lead_ms = lead_ms
        self.stop = stop
        self.log = log
        self.stats = SnifferStats()

    async def run(self) -> None:
        """Capture until stop is set or the output is closed, initialising
        the device again after each reset."""
        while not self.stop.is_set():
            try:
                await self._init()
                await self._capture()
                return
            except DeviceReset as exc:
                self.stats.resets += 1
                self.log.warning("%s, initialising it again", exc)

    async def _init(self) -> None:
        # Reset first: it drops the operations a previous host left queued.
        await self.radio.reset()
        await self.radio.init()
        await self.radio.set_network_id(self.network_id)
        await self.radio.enable(True)
        mode = RX_MODE_NAMES[self.reception]
        self.log.info(
            "capturing on channel %d, network ID %#010x, %s windows of %g ms",
            self.channel,
            self.network_id,
            mode,
            self.window_ms,
        )

    async def _capture(self) -> None:
        pipeline = RxPipeline(
            self.radio, self.channel, self.window_ms, self.depth, self.lead_ms, self.reception
        )
        stopper = asyncio.create_task(self._stop_when_set(pipeline))
        try:
            await self._write(pipeline)
        except BrokenPipeError:
            self.log.info("output closed, stopping")
            await pipeline.stop()
        finally:
            stopper.cancel()
            await asyncio.wait({stopper})
            self.stats.dropped_events += pipeline.stats.dropped_events

    async def _stop_when_set(self, pipeline: RxPipeline) -> None:
        await self.stop.wait()
        await pipeline.stop()

    async def _write(self, pipeline: RxPipeline) -> None:
        timing = self.radio.caps.timing
        items = aiter(pipeline)
        async with contextlib.aclosing(items):
            async for item in items:
                if isinstance(item, Gap):
                    gap_ms = timing.ticks_to_ms(item.length)
                    self.log.warning("%.1f ms not covered (%s)", gap_ms, item.reason)
                    self.stats.gaps += 1
                    self.stats.gap_ms += gap_ms
                    continue
                record = build_tap_record(item)
                self.writer.write(record)
                self.stats.records += 1
                if isinstance(item, MsgError):
                    self.stats.crc_errors += 1


def _close_writer(writer: PcapWriter) -> None:
    try:
        writer.close()
    except BrokenPipeError:
        # The reader is gone, and so is the use of the data left to flush.
        pass


async def run_sniffer(
    transport_url: str,
    out: BinaryIO,
    channel: int,
    network_id: int,
    reception: Reception,
    window_ms: float,
    depth: int,
    lead_ms: float,
    stop: asyncio.Event,
    log: logging.Logger,
    flush_each: bool = False,
) -> SnifferStats:
    """Write the frames received on channel to out as a DECT-NR-TAP pcap,
    until stop is set or the reader of out goes away.

    A device reset is logged and the device initialised again; the capture
    goes on in the same file. Before returning, the queued windows finish
    and the radio is disabled. out is closed in any case.
    """
    writer = PcapWriter(out, flush_each=flush_each)
    try:
        transport = open_transport(transport_url)
        async with transport:
            client = SpinelClient(transport)
            await client.start()
            radio = Radio(client)
            sniffer = _Sniffer(
                radio,
                writer,
                channel,
                network_id,
                reception,
                window_ms,
                depth,
                lead_ms,
                stop,
                log,
            )
            try:
                await sniffer.run()
                await radio.enable(False)
            finally:
                await radio.close()
                await client.close()
    finally:
        _close_writer(writer)
    return sniffer.stats


def _window_ms(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError(f"window must be > 0 ms, not {text}")
    return value


def _int_any_base(text: str) -> int:
    return int(text, 0)


def build_parser(extcap_mode: bool = False) -> argparse.ArgumentParser:
    """The command-line parser; in extcap mode it takes the Wireshark
    arguments too, and --transport is optional."""
    parser = argparse.ArgumentParser(
        prog="nrp-sniffer",
        description="Capture DECT NR+ frames with an Opener Spinel device into a pcap "
        "(link type DECT_NR_TAP, 304).",
    )
    transport_default = None
    if extcap_mode:
        transport_default = extcap.DEFAULT_TRANSPORT
    parser.add_argument(
        "--transport",
        required=not extcap_mode,
        default=transport_default,
        metavar="URL",
        help="device link: uart:///dev/ttyACM0?baud=1000000 or tcp://host:port",
    )
    parser.add_argument("--channel", type=int, default=DEFAULT_CHANNEL)
    parser.add_argument(
        "--network-id",
        type=_int_any_base,
        default=DEFAULT_NETWORK_ID,
        help="32-bit network ID, decimal or 0x hex (default %(default)#x)",
    )
    parser.add_argument(
        "--rx-mode",
        choices=list(RX_MODES),
        default=RX_MODE_NAMES[DEFAULT_RX_MODE],
        help="RX reception mode (default %(default)s)",
    )
    parser.add_argument(
        "--window",
        type=_window_ms,
        default=DEFAULT_WINDOW_MS,
        metavar="MS",
        help="RX window length in ms (default %(default)g)",
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=DEFAULT_DEPTH,
        help="RX windows queued on the device (default %(default)d)",
    )
    parser.add_argument(
        "--lead",
        type=float,
        default=DEFAULT_LEAD_MS,
        metavar="MS",
        help="how far ahead of the device time windows are scheduled (default %(default)g)",
    )
    parser.add_argument(
        "-w",
        "--write",
        default="-",
        metavar="FILE",
        help="pcap output, - for stdout (default)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logs")
    if extcap_mode:
        extcap.add_arguments(parser)
    return parser


def _open_output(path: str) -> tuple[BinaryIO, bool]:
    """Return the output and whether to flush each record: a pipe or a FIFO
    has a live reader."""
    if path == "-":
        return sys.stdout.buffer, True
    out = open(path, "wb")
    fd = out.fileno()
    status = os.fstat(fd)
    is_fifo = stat.S_ISFIFO(status.st_mode)
    return out, is_fifo


async def _run_until_signal(
    args: argparse.Namespace, out: BinaryIO, flush_each: bool, log: logging.Logger
) -> SnifferStats:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for signum in signals:
        loop.add_signal_handler(signum, stop.set)
    reception = RX_MODES[args.rx_mode]
    try:
        return await run_sniffer(
            args.transport,
            out,
            args.channel,
            args.network_id,
            reception,
            args.window,
            args.depth,
            args.lead,
            stop,
            log,
            flush_each=flush_each,
        )
    finally:
        for signum in signals:
            loop.remove_signal_handler(signum)


def _format_stats(stats: SnifferStats) -> str:
    return (
        f"{stats.records} records, {stats.crc_errors} CRC errors, "
        f"{stats.dropped_events} dropped events, {stats.gaps} gaps ({stats.gap_ms:.1f} ms), "
        f"{stats.resets} device resets"
    )


def _extcap_answer(args: argparse.Namespace) -> str | None:
    """Return the answer to a Wireshark query, None for a capture."""
    if args.extcap_dlts:
        return extcap.dlts()
    if args.extcap_config:
        rx_mode = RX_MODE_NAMES[DEFAULT_RX_MODE]
        return extcap.config(
            DEFAULT_CHANNEL,
            DEFAULT_NETWORK_ID,
            RX_MODE_DISPLAY,
            rx_mode,
            DEFAULT_WINDOW_MS,
            DEFAULT_DEPTH,
        )
    if args.capture:
        return None
    # --extcap-interfaces, or --extcap-version alone.
    package_version = version("opener-tools")
    return extcap.interfaces(package_version)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    extcap_mode = extcap.wants_extcap(argv)
    parser = build_parser(extcap_mode)
    if extcap_mode:
        # Wireshark versions add arguments over time: ignore the unknown ones.
        args, _ = parser.parse_known_args(argv)
        answer = _extcap_answer(args)
        if answer is not None:
            sys.stdout.write(answer)
            return 0
        if args.fifo is None:
            parser.error("--capture needs --fifo")
        output = args.fifo
    else:
        args = parser.parse_args(argv)
        output = args.write
    level = logging.INFO
    if args.verbose:
        level = logging.DEBUG
    logging.basicConfig(
        stream=sys.stderr, level=level, format="%(asctime)s %(levelname)s %(message)s"
    )
    log = logging.getLogger("nrp-sniffer")

    out, flush_each = _open_output(output)
    try:
        stats = asyncio.run(_run_until_signal(args, out, flush_each, log))
    except Exception as exc:
        log.error("%s", exc, exc_info=args.verbose)
        return 1
    summary = _format_stats(stats)
    print(summary, file=sys.stderr)
    return 0
