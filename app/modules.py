# SPDX-License-Identifier: GPL-3.0-or-later
"""The features of WireTrace that can be switched on and off.

Each feature is a module: its code, its settings, and what it adds to a
tab. It is described here once, and its code is imported only while it
is switched on, so a feature that is off costs no memory, no start-up
time and no work.

Reading the port, writing the log, framing, classifying and the console
are not modules: without them WireTrace does not log.

The choice of each module is kept in ``preferences.ini`` under
``[Modules]``, one key per module. Restoring the defaults returns every
module to its default.

This module does not touch the UI, the serial port or log files.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import ConfigManager

SECTION = "Modules"


@dataclass(frozen=True)
class ModuleSpec:
    """One feature that can be switched on and off.

    ``import_path`` names its class as ``package.module:Class``; it is
    imported only when the module is switched on. A feature built into
    the tab's reader or console (run on every line at a small, bounded
    cost) has none: the tab switches it on and off itself.
    """

    id: str
    title: str
    summary: str
    import_path: str | None
    default_enabled: bool = True


PLOT = ModuleSpec(
    id="plot",
    title="Live plot",
    summary="Draws the numbers in received lines as traces against time.",
    import_path="ui.features.plot_feature:PlotFeature",
)

RESTARTS = ModuleSpec(
    id="restarts",
    title="Restart detection",
    summary="Marks and counts each restart of the board, from the boot lines of "
            "ESP32 and Zephyr boards.",
    import_path=None,
)

REPORT = ModuleSpec(
    id="report",
    title="Session report",
    summary="Sums up a session: running time, restarts, lines received and not "
            "recorded, errors and warnings.",
    import_path="ui.features.report_feature:ReportFeature",
)

HIGHLIGHTS = ModuleSpec(
    id="highlights",
    title="Highlight rules",
    summary="Draws the lines that hold a text you choose in a colour you choose.",
    import_path=None,
)

ANSI_COLOURS = ModuleSpec(
    id="ansi",
    title="ANSI colours",
    summary="Shows the colours a device sets with ANSI codes. Logs and exports "
            "stay plain text.",
    import_path=None,
    default_enabled=False,
)

ALERTS = ModuleSpec(
    id="alerts",
    title="Alerts",
    summary="Tells you when an error line arrives, the connection is lost, or the "
            "board is quiet for longer than you set.",
    import_path="ui.features.alerts_feature:AlertsFeature",
    default_enabled=False,
)

#: Every module, in the order the Features page lists them.
MODULES: tuple[ModuleSpec, ...] = (
    PLOT, RESTARTS, REPORT, ALERTS, HIGHLIGHTS, ANSI_COLOURS,
)


def module_defaults() -> dict[str, str]:
    """The stored default of every module, for the defaults of ``[Modules]``."""
    return {spec.id: "true" if spec.default_enabled else "false" for spec in MODULES}


class ModuleSettings:
    """Reads and writes which modules are switched on."""

    def __init__(self, config: ConfigManager) -> None:
        self._config = config

    def is_enabled(self, spec: ModuleSpec) -> bool:
        return self._config.get_bool(SECTION, spec.id, fallback=spec.default_enabled)

    def set_enabled(self, spec: ModuleSpec, enabled: bool) -> None:
        self._config.set(SECTION, spec.id, "true" if enabled else "false")

    def enabled(self) -> tuple[ModuleSpec, ...]:
        """The modules switched on, in registry order."""
        return tuple(spec for spec in MODULES if self.is_enabled(spec))
