# SPDX-License-Identifier: GPL-3.0-or-later
"""Finding a lost board again, and asking when it cannot be told apart.

When a tab connects, the identity of its port is recorded
(``core/port_identity.py``). When the board is lost and comes back, it
is taken back automatically only when one of three rules holds and
exactly one present port matches:

  A. Serial number: the same vendor, product, interface and serial
     number. The board may come back in another socket.
  B. USB port: no serial number to go by. The same vendor, product and
     interface, in the same USB location.
  C. Port name: the port has no USB identity at all (a built-in COM
     port, a PCI card, a Bluetooth serial port), so its name, fixed by
     the system, is its identity. This rule can be switched off.

A serial number is trusted only when no other port of the same model
had the same one, at connect or when the board was lost: some clone
boards share theirs. A board whose serial number is not trusted falls
to rule B.

When no rule gives exactly one port, nothing is chosen: a port of the
same model that could be the board is offered to the user to decide,
one at a time, never taken by itself. A port another tab holds is never
offered, and neither is a port that was already present, as it is, when
the board was lost: that one is another board.

Two boards that report the same serial number and are never present
together cannot be told apart by their USB details; only a board's own
software could.

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass

from core.port_identity import PortIdentity


class Basis(enum.Enum):
    """How a returning board was told apart, as the notice says it."""

    SERIAL_NUMBER = "identified by serial number"
    USB_PORT = "identified by USB port"
    PORT_NAME = "identified by port name; this port has no USB identity"
    CONFIRMED = "confirmed as the same board by you"


@dataclass(frozen=True)
class Board:
    """The board a tab was connected to, as recorded at connect.

    ``serial_trusted`` is False when another present port of the same
    model had the same serial number at connect.
    """

    identity: PortIdentity
    serial_trusted: bool

    def describe(self) -> str:
        return self.identity.describe()


@dataclass(frozen=True)
class Found:
    """The board is back, on ``identity.port``."""

    identity: PortIdentity
    basis: Basis

    def describe(self) -> str:
        """For the notice: "identified by serial number 0001A2F3"."""
        if self.basis is Basis.SERIAL_NUMBER:
            return f"{self.basis.value} {self.identity.serial}"
        if self.basis is Basis.USB_PORT:
            return f"{self.basis.value} {self.identity.location}"
        return self.basis.value


@dataclass(frozen=True)
class Ask:
    """A port of the same model appeared; only the user can say whether it is the board."""

    identity: PortIdentity


def record_board(identity: PortIdentity, present: Iterable[PortIdentity]) -> Board:
    """The board connected on ``identity``, given the ports present at connect."""
    return Board(identity, _serial_unique(identity, list(present)))


def find_board(
    board: Board,
    present: Iterable[PortIdentity],
    *,
    held_elsewhere: frozenset[str] = frozenset(),
    declined: frozenset[str] = frozenset(),
    present_at_loss: frozenset[PortIdentity] = frozenset(),
    by_port_name: bool = True,
) -> Found | Ask | None:
    """Look for ``board`` among the ports ``present``.

    Args:
        held_elsewhere: Ports other tabs hold; never matched or offered.
        declined: Ports the user said are not the board; never offered again.
        present_at_loss: Ports present when the board was lost; never offered.
        by_port_name: Whether rule C may be used.
    """
    present = list(present)
    recorded = board.identity
    free = [identity for identity in present if identity.port not in held_elsewhere]

    if not recorded.is_usb:
        if by_port_name:
            for identity in free:
                if identity.port == recorded.port and not identity.is_usb:
                    return Found(identity, Basis.PORT_NAME)
        return None

    same_model = [identity for identity in present if identity.same_model(recorded)]
    # A board already present at the loss with the same serial number is
    # another board that shares it: the number cannot be trusted.
    serial_trusted = board.serial_trusted and not any(
        identity.same_model(recorded) and identity.serial == recorded.serial
        for identity in present_at_loss
    )
    if recorded.serial and serial_trusted:
        with_serial = [identity for identity in same_model if identity.serial == recorded.serial]
        if len(with_serial) == 1:
            identity = with_serial[0]
            if identity.port in held_elsewhere:
                return None
            return Found(identity, Basis.SERIAL_NUMBER)
        pool = [identity for identity in with_serial if identity.port not in held_elsewhere]
    else:
        pool = [
            identity for identity in same_model
            if identity.port not in held_elsewhere and identity.serial == recorded.serial
        ]

    if recorded.location:
        in_place = [identity for identity in pool if identity.location == recorded.location]
        if len(in_place) == 1:
            return Found(in_place[0], Basis.USB_PORT)

    for identity in pool:
        if identity.port not in declined and identity not in present_at_loss:
            return Ask(identity)
    return None


def still_the_board(found: PortIdentity, present: Iterable[PortIdentity]) -> bool:
    """True if the port of ``found`` still shows the same device as when it was found."""
    return any(identity == found for identity in present)


def _serial_unique(identity: PortIdentity, present: list[PortIdentity]) -> bool:
    if not identity.serial:
        return False
    sharing = [
        other for other in present
        if other.same_model(identity) and other.serial == identity.serial
    ]
    return len(sharing) <= 1
