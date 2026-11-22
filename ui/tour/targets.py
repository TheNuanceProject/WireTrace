# SPDX-License-Identifier: GPL-3.0-or-later
"""The controls the tour points at.

A widget, or an entry of a menu, is marked with the name of what it is
(``mark``) where it is made; the tour finds it by that name (``find``,
``find_action``), so the tour never reaches into a widget's private
parts and a control that is moved or renamed stays found. Several
widgets may carry one name (the level buttons): the tour points at all
of them that are shown.
"""

from __future__ import annotations

import enum
from typing import TypeVar

from PySide6.QtCore import QObject
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QWidget

from app.modules import ALERTS, ANSI_COLOURS, HIGHLIGHTS, PLOT, REPORT, RESTARTS

_Marked = TypeVar("_Marked", bound=QObject)

#: The dynamic property a marked widget carries.
TOUR_TARGET = "tourTarget"


class Target(str, enum.Enum):
    """Every control the tour can point at."""

    # The first screen of a tab
    PORTS = "ports"
    BAUD = "baud"
    PORT_SETTINGS = "portSettings"
    CONNECT = "connect"
    NEW_TAB = "newTab"
    # A tab's toolbar
    START_LOG = "startLog"
    PAUSE_LOG = "pauseLog"
    CLEAR = "clear"
    EXPORT = "export"
    PLOT = "plot"
    CONTROL_LINES = "controlLines"
    TEXT_HEX = "textHex"
    FIND = "find"
    MORE = "more"
    # Above the console, the console, and below it
    LEVELS = "levels"
    FILTER = "filter"
    TIMESTAMPS = "timestamps"
    FOLLOWING = "following"
    CONSOLE = "console"
    COMMAND = "command"
    LINE_ENDING = "lineEnding"
    SAVED_COMMANDS = "savedCommands"
    # The status bar
    STATUS_BAR = "statusBar"
    INPUT_LINES = "inputLines"
    LOG_STATE = "logState"
    # The plot
    PLOT_VALUES = "plotValues"
    PLOT_CHART = "plotChart"
    PLOT_LAYOUT = "plotLayout"
    PLOT_MEASURE = "plotMeasure"
    PLOT_TIME = "plotTime"
    PLOT_HOLD = "plotHold"
    PLOT_CONFIGURE = "plotConfigure"
    PLOT_SAVE = "plotSave"
    # Entries of the menu bar, and its menus
    MENU_LOG_FOLDER = "menuLogFolder"
    MENU_PREFERENCES = "menuPreferences"
    MENU_THEME = "menuTheme"
    MENU_RECONNECT = "menuReconnect"
    MENU_ALERTS = "menuAlerts"
    MENU_SESSION_REPORT = "menuSessionReport"
    MENU_HELP = "menuHelp"
    MENU_RETURN = "menuReturn"
    MENU_DIAGNOSTIC_LOGS = "menuDiagnosticLogs"
    # Preferences
    PREFERENCE_PAGES = "preferencePages"
    LOG_SETTINGS = "logSettings"
    RECONNECT_SETTINGS = "reconnectSettings"
    ALERT_SETTINGS = "alertSettings"
    HIGHLIGHT_RULES = "highlightRules"
    FEATURE_PLOT = "featurePlot"
    FEATURE_RESTARTS = "featureRestarts"
    FEATURE_REPORT = "featureReport"
    FEATURE_HIGHLIGHTS = "featureHighlights"
    FEATURE_ANSI = "featureAnsi"
    FEATURE_ALERTS = "featureAlerts"
    # The session report
    REPORT_ACTIONS = "reportActions"


#: The switch of each feature on the Features page of Preferences, by the
#: feature's id.
FEATURE_TARGETS: dict[str, Target] = {
    PLOT.id: Target.FEATURE_PLOT,
    RESTARTS.id: Target.FEATURE_RESTARTS,
    REPORT.id: Target.FEATURE_REPORT,
    HIGHLIGHTS.id: Target.FEATURE_HIGHLIGHTS,
    ANSI_COLOURS.id: Target.FEATURE_ANSI,
    ALERTS.id: Target.FEATURE_ALERTS,
}


def mark(item: _Marked, target: Target) -> _Marked:
    """Mark ``item``, a widget or a menu's entry, as ``target``. Returns it."""
    item.setProperty(TOUR_TARGET, target.value)
    return item


def find(root: QWidget, target: Target) -> list[QWidget]:
    """The widgets under ``root`` marked as ``target`` that are shown."""
    return [
        widget for widget in root.findChildren(QWidget)
        if widget.property(TOUR_TARGET) == target.value and widget.isVisible()
    ]


def find_action(root: QWidget, target: Target) -> QAction | None:
    """The entry under ``root`` marked as ``target``: an entry of a menu,
    or the entry that opens a menu."""
    for action in root.findChildren(QAction):
        if action.property(TOUR_TARGET) == target.value:
            return action
    return None
