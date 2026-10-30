# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

import pytest

from opener_tools import transport
from opener_tools.transport import (
    TcpTransport,
    UartTransport,
    open_transport,
    register_scheme,
)


def test_uart_url_default_baud():
    transport = open_transport("uart:///dev/ttyACM0")
    assert isinstance(transport, UartTransport)
    assert transport.path == "/dev/ttyACM0"
    assert transport.baud == 1_000_000


def test_uart_url_baud():
    transport = open_transport("uart:///dev/ttyACM0?baud=115200")
    assert isinstance(transport, UartTransport)
    assert transport.path == "/dev/ttyACM0"
    assert transport.baud == 115200


def test_bare_dev_path_is_uart():
    transport = open_transport("/dev/ttyACM2")
    assert isinstance(transport, UartTransport)
    assert transport.path == "/dev/ttyACM2"
    assert transport.baud == 1_000_000


def test_tcp_url():
    transport = open_transport("tcp://172.16.1.134:5555")
    assert isinstance(transport, TcpTransport)
    assert transport.host == "172.16.1.134"
    assert transport.port == 5555


def test_spi_url_not_implemented():
    with pytest.raises(NotImplementedError, match="plan 3"):
        open_transport("spi:///dev/spidev3.0?irq=gpiochip4:0")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "ttyACM0",
        "uart://",
        "uart:///dev/ttyACM0?baud=fast",
        "uart:///dev/ttyACM0?speed=115200",
        "tcp://localhost",
        "tcp://:5555",
    ],
)
def test_bad_urls(url):
    with pytest.raises(ValueError):
        open_transport(url)


def test_registered_scheme(monkeypatch):
    monkeypatch.setattr(transport, "_schemes", {})
    opened = []
    tcp = TcpTransport("localhost", 1)

    def factory(parts):
        opened.append(parts)
        return tcp

    register_scheme("tcp", factory)

    assert open_transport("tcp://example:7") is tcp
    assert opened[0].hostname == "example"
