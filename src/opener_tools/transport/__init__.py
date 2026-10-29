# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Device transports, selected by URL."""

from urllib.parse import parse_qs, urlsplit

from .base import Transport, TransportClosed
from .tcp import TcpTransport
from .uart import DEFAULT_BAUD, UartTransport

__all__ = ["TcpTransport", "Transport", "TransportClosed", "UartTransport", "open_transport"]


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


def _open_tcp(host: str | None, port: int | None) -> TcpTransport:
    if not host or port is None:
        raise ValueError("tcp URL needs a host and a port")
    return TcpTransport(host, port)


def open_transport(url: str) -> Transport:
    """Create (not open) the transport for a URL.

    uart:///dev/ttyACM0?baud=1000000, or a bare /dev/... path; tcp://host:port.
    """
    if url.startswith("/dev/"):
        return UartTransport(url)
    parts = urlsplit(url)
    if parts.scheme == "uart":
        return _open_uart(parts.path, parts.query)
    if parts.scheme == "tcp":
        return _open_tcp(parts.hostname, parts.port)
    if parts.scheme == "spi":
        raise NotImplementedError("SPI transport comes with plan 3")
    raise ValueError(f"unsupported transport URL: {url}")
