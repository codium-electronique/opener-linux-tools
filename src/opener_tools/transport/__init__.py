# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Device transports, selected by URL."""

from collections.abc import Callable
from urllib.parse import SplitResult, parse_qs, urlsplit

from .base import Transport, TransportClosed
from .spi import DEFAULT_SPEED, SpiTransport
from .tcp import TcpTransport
from .uart import DEFAULT_BAUD, UartTransport

__all__ = [
    "SpiTransport",
    "TcpTransport",
    "Transport",
    "TransportClosed",
    "UartTransport",
    "open_transport",
    "register_scheme",
]

_schemes: dict[str, Callable[[SplitResult], Transport]] = {}


def register_scheme(scheme: str, factory: Callable[[SplitResult], Transport]) -> None:
    """Make open_transport create the transport of scheme URLs with factory,
    which gets the split URL; it takes precedence over the built-in schemes."""
    _schemes[scheme] = factory


def _open_uart(path: str, query: str) -> UartTransport:
    if not path:
        raise ValueError("uart URL without a device path")
    params = parse_qs(query, strict_parsing=bool(query))
    unknown = set(params) - {"baud"}
    if unknown:
        raise ValueError(f"unknown uart parameters: {', '.join(sorted(unknown))}")
    baud = DEFAULT_BAUD
    if "baud" in params:
        baud = int(params["baud"][-1])
    return UartTransport(path, baud)


def _parse_line(name: str, params: dict[str, list[str]]) -> tuple[str, int]:
    if name not in params:
        raise ValueError(f"spi URL without {name}=<gpiochip>:<line>")
    value = params[name][-1]
    chip, sep, line = value.partition(":")
    numeric = line.isdigit()
    if not chip or not sep or not numeric:
        raise ValueError(f"spi {name} is not <gpiochip>:<line>: {value}")
    return chip, int(line)


def _open_spi(path: str, query: str) -> SpiTransport:
    if not path:
        raise ValueError("spi URL without a device path")
    params = parse_qs(query, strict_parsing=bool(query))
    unknown = set(params) - {"irq", "busy", "speed"}
    if unknown:
        raise ValueError(f"unknown spi parameters: {', '.join(sorted(unknown))}")
    irq = _parse_line("irq", params)
    busy = _parse_line("busy", params)
    speed = DEFAULT_SPEED
    if "speed" in params:
        speed = int(params["speed"][-1])
    return SpiTransport(path, irq, busy, speed)


def _open_tcp(host: str | None, port: int | None) -> TcpTransport:
    if not host or port is None:
        raise ValueError("tcp URL needs a host and a port")
    return TcpTransport(host, port)


def open_transport(url: str) -> Transport:
    """Create (not open) the transport for a URL.

    uart:///dev/ttyACM0?baud=1000000, or a bare /dev/... path; tcp://host:port;
    spi:///dev/spidev3.0?irq=gpiochip4:0&busy=gpiochip1:1&speed=8000000.
    """
    if url.startswith("/dev/"):
        return UartTransport(url)
    parts = urlsplit(url)
    factory = _schemes.get(parts.scheme)
    if factory is not None:
        return factory(parts)
    if parts.scheme == "uart":
        return _open_uart(parts.path, parts.query)
    if parts.scheme == "tcp":
        return _open_tcp(parts.hostname, parts.port)
    if parts.scheme == "spi":
        return _open_spi(parts.path, parts.query)
    raise ValueError(f"unsupported transport URL: {url}")
