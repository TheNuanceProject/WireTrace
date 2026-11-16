# SPDX-License-Identifier: GPL-3.0-or-later
"""How a serial port is set up: framing, flow control, and the output
lines set when it opens.

Only choices the system can carry out are offered. Qt supports 1.5 stop
bits on Windows only, and mark and space parity need the CMSPAR flag,
which Windows and Linux have and macOS does not. A choice the operating
system or the driver still refuses is reported as the reason the port
could not be opened, never changed silently.

This module does not touch the GUI, the disk or the serial port.
"""

from __future__ import annotations

import enum
import sys
from dataclasses import dataclass, replace

#: The data bits a character can have.
DATA_BITS: tuple[int, ...] = (5, 6, 7, 8)


class Parity(enum.Enum):
    NONE = "none"
    EVEN = "even"
    ODD = "odd"
    MARK = "mark"
    SPACE = "space"


class StopBits(enum.Enum):
    ONE = "1"
    ONE_AND_HALF = "1.5"
    TWO = "2"


class FlowControl(enum.Enum):
    NONE = "none"
    HARDWARE = "hardware"
    SOFTWARE = "software"


PARITY_NAMES: dict[Parity, str] = {
    Parity.NONE: "None",
    Parity.EVEN: "Even",
    Parity.ODD: "Odd",
    Parity.MARK: "Mark",
    Parity.SPACE: "Space",
}

FLOW_CONTROL_NAMES: dict[FlowControl, str] = {
    FlowControl.NONE: "None",
    FlowControl.HARDWARE: "Hardware (RTS/CTS)",
    FlowControl.SOFTWARE: "Software (XON/XOFF)",
}

_PARITY_LETTERS: dict[Parity, str] = {
    Parity.NONE: "N",
    Parity.EVEN: "E",
    Parity.ODD: "O",
    Parity.MARK: "M",
    Parity.SPACE: "S",
}

_FLOW_CONTROL_PHRASES: dict[FlowControl, str] = {
    FlowControl.NONE: "no flow control",
    FlowControl.HARDWARE: "RTS/CTS flow control",
    FlowControl.SOFTWARE: "XON/XOFF flow control",
}


def parities_for(platform: str = sys.platform) -> tuple[Parity, ...]:
    """The parities a port can be given on ``platform``."""
    if platform == "win32" or platform.startswith("linux"):
        return tuple(Parity)
    return (Parity.NONE, Parity.EVEN, Parity.ODD)


def stop_bits_for(platform: str = sys.platform) -> tuple[StopBits, ...]:
    """The stop bits a port can be given on ``platform``."""
    if platform == "win32":
        return tuple(StopBits)
    return (StopBits.ONE, StopBits.TWO)


@dataclass(frozen=True, slots=True)
class LineSettings:
    """Everything a port is opened with apart from its baud rate.

    ``dtr_on_open`` and ``rts_on_open`` are the states the two output
    lines are set to as soon as the port is open. With hardware flow
    control the driver owns RTS, so ``rts_on_open`` is not applied.
    """

    data_bits: int = 8
    parity: Parity = Parity.NONE
    stop_bits: StopBits = StopBits.ONE
    flow_control: FlowControl = FlowControl.NONE
    dtr_on_open: bool = True
    rts_on_open: bool = True

    @property
    def framing(self) -> str:
        """The usual short form, for example ``8N1`` or ``7E2``."""
        return f"{self.data_bits}{_PARITY_LETTERS[self.parity]}{self.stop_bits.value}"

    @property
    def rts_is_manual(self) -> bool:
        """Whether RTS can be set by the user: not under hardware flow control."""
        return self.flow_control is not FlowControl.HARDWARE

    def describe(self) -> str:
        """For example ``8N1, no flow control``."""
        return f"{self.framing}, {_FLOW_CONTROL_PHRASES[self.flow_control]}"

    def has_usual_framing(self) -> bool:
        """Whether framing and flow control are the usual 8N1 without flow control."""
        usual = LineSettings()
        return (self.framing, self.flow_control) == (usual.framing, usual.flow_control)

    def supported_on(self, platform: str = sys.platform) -> bool:
        return (
            self.data_bits in DATA_BITS
            and self.parity in parities_for(platform)
            and self.stop_bits in stop_bits_for(platform)
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "data_bits": self.data_bits,
            "parity": self.parity.value,
            "stop_bits": self.stop_bits.value,
            "flow_control": self.flow_control.value,
            "dtr_on_open": self.dtr_on_open,
            "rts_on_open": self.rts_on_open,
        }

    @classmethod
    def from_dict(cls, raw: object, platform: str = sys.platform) -> LineSettings:
        """Settings from stored data. Each value that is missing, not
        valid, or not supported on ``platform`` takes its default."""
        settings = cls()
        if not isinstance(raw, dict):
            return settings
        data_bits = raw.get("data_bits")
        if isinstance(data_bits, int) and not isinstance(data_bits, bool) and data_bits in DATA_BITS:
            settings = replace(settings, data_bits=data_bits)
        parity = _member(Parity, raw.get("parity"))
        if parity is not None and parity in parities_for(platform):
            settings = replace(settings, parity=parity)
        stop_bits = _member(StopBits, raw.get("stop_bits"))
        if stop_bits is not None and stop_bits in stop_bits_for(platform):
            settings = replace(settings, stop_bits=stop_bits)
        flow_control = _member(FlowControl, raw.get("flow_control"))
        if flow_control is not None:
            settings = replace(settings, flow_control=flow_control)
        for name in ("dtr_on_open", "rts_on_open"):
            value = raw.get(name)
            if isinstance(value, bool):
                settings = replace(settings, **{name: value})
        return settings


@dataclass(frozen=True, slots=True)
class InputLines:
    """The input lines of a port, read from the port.

    ``None`` means the driver could not report that line.
    """

    cts: bool | None = None
    dsr: bool | None = None
    dcd: bool | None = None
    ri: bool | None = None


@dataclass(frozen=True, slots=True)
class LineStates:
    """The control lines of an open port.

    ``rts`` is None while hardware flow control drives RTS.
    """

    dtr: bool
    rts: bool | None
    inputs: InputLines


def _member(kind: type[enum.Enum], value: object) -> enum.Enum | None:
    if not isinstance(value, str):
        return None
    try:
        return kind(value)
    except ValueError:
        return None
