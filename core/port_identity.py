# SPDX-License-Identifier: GPL-3.0-or-later
"""Who a serial port is: the USB details that tell one board from another.

Read from pyserial's port list, which reports them on Windows, Linux and
macOS (``serial.tools.list_ports``): the USB vendor and product ID, the
device's own serial number when it has one, and its USB location, the
path through controller, hubs and socket to the board.

Each system's details come from its own source, and pyserial already
handles their quirks:

  Windows: the serial number is taken from the device instance ID, and
  an ID Windows made up for a device without one (it contains ``&``) is
  never taken as one. The location comes from the device's location
  paths; FTDI's own driver hides it.
  Linux: sysfs, where the location is the USB device path (``1-1.2``),
  or the interface path (``1-1.2:1.0``) for a device with several.
  macOS: IOKit; the location comes from ``locationID``.

The part of a location after ``:`` names the USB interface, which
tells the ports of a device with several apart.

Ports are named as Qt names them (``COM7``, ``ttyUSB0``,
``cu.usbserial-0001``). On macOS each device has a ``tty.`` and a
``cu.`` port; pyserial lists the ``cu.`` one, so a ``tty.`` port is
matched to its ``cu.`` twin.

This module does not touch the GUI, the disk or log files.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from serial.tools import list_ports

logger = logging.getLogger(__name__)

_INTERFACE_MARK = ":"
_MAC_CALL_IN = "tty."
_MAC_CALL_OUT = "cu."


@dataclass(frozen=True)
class PortIdentity:
    """One present port and the USB details of the device behind it.

    A port with no USB details (a built-in COM port, a PCI card, a
    Bluetooth serial port) has ``vid`` and ``pid`` None.
    """

    port: str
    vid: int | None = None
    pid: int | None = None
    serial: str | None = None
    location: str | None = None

    @property
    def is_usb(self) -> bool:
        return self.vid is not None and self.pid is not None

    @property
    def interface(self) -> str | None:
        """The USB interface part of the location, or None."""
        if self.location and _INTERFACE_MARK in self.location:
            return self.location.rsplit(_INTERFACE_MARK, 1)[1]
        return None

    def same_model(self, other: PortIdentity) -> bool:
        """Same vendor, product and interface."""
        return (self.vid, self.pid, self.interface) == (other.vid, other.pid, other.interface)

    def describe(self) -> str:
        """The device in a few words, for the notices.

        For example "USB 10C4:EA60, serial number 0001, USB port 1-1.2".
        """
        if not self.is_usb:
            return self.port
        text = f"USB {self.vid:04X}:{self.pid:04X}"
        if self.serial:
            text += f", serial number {self.serial}"
        if self.location:
            text += f", USB port {self.location}"
        return text


#: Reads the identity of every port present now.
PortLister = Callable[[], list[PortIdentity]]


def read_identities(
    platform: str = sys.platform,
    comports: Callable[[], Iterable[object]] = list_ports.comports,
) -> list[PortIdentity]:
    """The identity of every port present now. Never raises: a failure to
    read the list is logged and gives no ports."""
    try:
        listed = list(comports())
    except Exception:
        logger.exception("The list of serial ports could not be read")
        return []
    identities = [
        PortIdentity(
            port=_qt_name(getattr(info, "device", "") or "", platform),
            vid=getattr(info, "vid", None),
            pid=getattr(info, "pid", None),
            serial=getattr(info, "serial_number", None) or None,
            location=getattr(info, "location", None) or None,
        )
        for info in listed
    ]
    return [identity for identity in identities if identity.port]


def identity_of(port: str, identities: Iterable[PortIdentity], platform: str = sys.platform) -> PortIdentity:
    """The identity of ``port`` (a Qt port name) among ``identities``; a port
    pyserial does not list is one without USB details."""
    names = {port}
    if platform == "darwin" and port.startswith(_MAC_CALL_IN):
        names.add(_MAC_CALL_OUT + port.removeprefix(_MAC_CALL_IN))
    for identity in identities:
        if identity.port in names:
            return PortIdentity(port, identity.vid, identity.pid, identity.serial, identity.location)
    return PortIdentity(port)


def _qt_name(device: str, platform: str) -> str:
    """The name Qt gives the port pyserial lists as ``device``."""
    if platform == "win32":
        return device
    return device.rsplit("/", 1)[-1]
