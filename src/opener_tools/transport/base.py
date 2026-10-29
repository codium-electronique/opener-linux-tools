# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Codium Electronique

"""Transport interface: whole Spinel frames to and from the device."""

import abc


class TransportClosed(Exception):
    """The transport is closed, or the peer went away."""


class Transport(abc.ABC):
    """A link carrying whole Spinel frames.

    errors counts the frames dropped by the link layer (bad FCS, overflow).
    """

    errors: int = 0

    @abc.abstractmethod
    async def open(self) -> None: ...

    @abc.abstractmethod
    async def close(self) -> None: ...

    @abc.abstractmethod
    async def send(self, frame: bytes) -> None: ...

    @abc.abstractmethod
    async def recv(self) -> bytes:
        """Return the next received frame; raise TransportClosed once closed."""

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()
