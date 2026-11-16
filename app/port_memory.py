# SPDX-License-Identifier: GPL-3.0-or-later
"""What each serial port was last used with.

For every port the user has connected to, WireTrace remembers the baud
rate, the line settings, the line ending for commands, the plot profile,
where its plot window was and the name given to its tab, and offers
them again the next time that port is chosen.

The settings are kept in ``preferences.ini`` under ``[Ports]`` as one
JSON value keyed by port name. They never leave the machine.

This module does not touch the UI or the disk directly: persistence flows
through the injected ``ConfigManager``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.constants import (
    BAUD_RATE_MAX,
    BAUD_RATE_MIN,
    PORT_MEMORY_LIMIT,
    PORT_PLOT_WINDOW_MAX,
    PORT_TAB_NAME_MAX,
    LineEnding,
)
from core.serial_settings import LineSettings

if TYPE_CHECKING:
    from app.config import ConfigManager

logger = logging.getLogger(__name__)

SECTION = "Ports"
KEY = "remembered_json"

_BASE64 = re.compile(r"[A-Za-z0-9+/]*={0,2}")


@dataclass(frozen=True)
class PortSettings:
    """Settings last used with one port. An empty value means "not set"."""

    baud_rate: int = 0
    line_ending: str = ""       # name of a ``LineEnding`` member
    plot_profile: str = ""
    tab_name: str = ""
    line: LineSettings = field(default_factory=LineSettings)
    plot_window: str = ""       # the plot window's geometry, as Qt saves it, in base64

    def line_ending_choice(self) -> LineEnding | None:
        return LineEnding.__members__.get(self.line_ending)

    def to_dict(self) -> dict[str, object]:
        return {
            "baud_rate": self.baud_rate,
            "line_ending": self.line_ending,
            "plot_profile": self.plot_profile,
            "tab_name": self.tab_name,
            "line": self.line.to_dict(),
            "plot_window": self.plot_window,
        }


def _clean(raw: object) -> PortSettings | None:
    """Build settings from stored data, dropping anything not valid."""
    if not isinstance(raw, dict):
        return None
    baud = raw.get("baud_rate")
    ending = raw.get("line_ending")
    profile = raw.get("plot_profile")
    name = raw.get("tab_name")
    line = raw.get("line")
    window = raw.get("plot_window")
    return PortSettings(
        baud_rate=baud if isinstance(baud, int) and BAUD_RATE_MIN <= baud <= BAUD_RATE_MAX else 0,
        line_ending=ending if isinstance(ending, str) and ending in LineEnding.__members__ else "",
        plot_profile=profile if isinstance(profile, str) else "",
        tab_name=name[:PORT_TAB_NAME_MAX] if isinstance(name, str) else "",
        line=line if isinstance(line, LineSettings) else LineSettings.from_dict(line),
        plot_window=window if _is_geometry(window) else "",
    )


def _is_geometry(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= PORT_PLOT_WINDOW_MAX
        and _BASE64.fullmatch(value) is not None
    )


class PortMemory:
    """Reads and updates the remembered settings of every port."""

    def __init__(self, config: ConfigManager) -> None:
        self._config = config

    def get(self, port: str) -> PortSettings | None:
        """Settings last used with ``port``, or None if it was never used."""
        return self._load().get(port)

    def remember(self, port: str, **changes: object) -> None:
        """Update some settings of ``port`` and make it the most recent."""
        if not port:
            return
        ports = self._load()
        current = ports.pop(port, PortSettings())
        cleaned = _clean({**current.to_dict(), **changes})
        if cleaned is None:
            return
        ports[port] = cleaned
        while len(ports) > PORT_MEMORY_LIMIT:
            # The least recently used port is the first one.
            ports.pop(next(iter(ports)))
        self._config.set(
            SECTION, KEY,
            json.dumps({name: settings.to_dict() for name, settings in ports.items()}),
        )

    def note(self, port: str, line_ending_names: dict[LineEnding, str]) -> str:
        """One line saying what ``port`` was last used with, or ""."""
        settings = self.get(port)
        if settings is None or not settings.baud_rate:
            return ""
        parts = [f"Last used at {settings.baud_rate}"]
        if not settings.line.has_usual_framing():
            parts.append(settings.line.describe())
        ending = settings.line_ending_choice()
        if ending is not None:
            parts.append(line_ending_names[ending])
        if settings.plot_profile:
            parts.append(f"plot profile {settings.plot_profile}")
        return ", ".join(parts)

    def _load(self) -> dict[str, PortSettings]:
        raw = self._config.get(SECTION, KEY, fallback="")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            logger.warning("Remembered port settings are unreadable and are ignored: %s", exc)
            return {}
        if not isinstance(data, dict):
            return {}
        ports: dict[str, PortSettings] = {}
        for name, stored in data.items():
            settings = _clean(stored)
            if isinstance(name, str) and name and settings is not None:
                ports[name] = settings
        return ports
