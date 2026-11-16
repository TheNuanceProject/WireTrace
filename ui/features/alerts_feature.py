# SPDX-License-Identifier: GPL-3.0-or-later
"""Alerts: telling the user when something needs a look.

Three things can alert, each chosen on the Alerts page of Preferences:

  An error line: the tab's exact count of error and critical lines
  (``core/session_counters.py``) rose. A banner says how many arrived
  since the user last dismissed it, and quotes the latest one. Like an
  alarm, it reminds the user while error lines keep arriving, at most
  once every time between alerts, until the banner is dismissed: that
  acknowledges them. Error lines that go on arriving after that only
  count on the banner, silently. Once none has arrived for the time
  between alerts, the next one is a new alert.

  The connection lost: the tab already says so in a banner, and waits
  for the board if reconnect is on; the alert draws attention to it.

  A quiet board: no line arrived for longer than the time set. A banner
  says since when, and goes once lines arrive again.

Drawing attention means asking the system to flash the window (its
taskbar button, or the dock icon), which it does only while the window
is not the one in use, and handing the alert to the tab's share of the
sound and the desktop notification (``ui/alert_notifier.py``), which
paces the alerts of every tab together.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QTimer, Slot
from PySide6.QtWidgets import QApplication

from app.constants import ALERT_CHECK_INTERVAL_MS, ALERT_QUOTE_CHARS_MAX, ERROR_LEVELS
from core.records import RecordOrigin
from ui.alert_notifier import AlertKind
from ui.formatting import format_count, format_time_of_day, shorten
from ui.module_host import TabContext, TabModule
from ui.widgets import banner as banners

if TYPE_CHECKING:
    from core.records import LineRecord
    from core.session_counters import SessionFigures

_BANNER_ERRORS = "alert-errors"
_BANNER_QUIET = "alert-quiet"
_CONNECTION_LOST = "Connection lost"


class AlertsFeature(TabModule):
    """The alerts of one tab."""

    reads_lines = True

    def __init__(self, context: TabContext) -> None:
        super().__init__(context)
        self._timer = QTimer(self)
        self._timer.setInterval(ALERT_CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.guarded(self._check))
        # Error lines: the count the user has seen (dismissed), the count
        # last alerted on, and the latest error line received.
        self._errors_seen = 0
        self._errors_alerted = 0
        self._latest_error: LineRecord | None = None
        # Whether the user dismissed the banner while error lines were
        # still arriving, and when the count last rose.
        self._acknowledged = False
        self._errors_rose_at = -float("inf")
        # A quiet board: the lines counted at the last check, when that
        # count last changed, and whether the banner is up.
        self._lines = 0
        self._lines_changed_at = 0.0
        self._quiet_since: datetime | None = None

    # ── Module ───────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.context.is_connected():
            self.session_started()

    def stop(self) -> None:
        self._timer.stop()
        self.context.banners.clear(_BANNER_ERRORS)
        self.context.banners.clear(_BANNER_QUIET)

    def take_records(self, records: list[LineRecord], skipped: int) -> None:
        for record in records:
            if record.origin is RecordOrigin.DEVICE and record.tag in ERROR_LEVELS:
                self._latest_error = record

    def session_started(self) -> None:
        figures = self.context.session_figures()
        self._errors_seen = self._errors_alerted = _errors(figures)
        self._latest_error = None
        self._acknowledged = False
        self._errors_rose_at = -float("inf")
        self._lines = figures.lines if figures is not None else 0
        self._lines_changed_at = time.monotonic()
        self._quiet_since = None
        self._timer.start()

    def session_ended(self, unexpected: bool) -> None:
        self._check()
        self._timer.stop()
        self._end_quiet()
        if unexpected and self.context.config.alert_connection_lost:
            self._draw_attention(AlertKind.CONNECTION_LOST, _CONNECTION_LOST)

    def connection_lost(self) -> None:
        # Quiet is expected while the board is away; the loss is the alert.
        self._timer.stop()
        self._end_quiet()
        if self.context.config.alert_connection_lost:
            self._draw_attention(AlertKind.CONNECTION_LOST, _CONNECTION_LOST)

    def connection_back(self, seconds_away: float) -> None:
        self._lines_changed_at = time.monotonic()
        self._timer.start()

    def apply_preferences(self) -> None:
        config = self.context.config
        if not config.alert_error_lines:
            self.context.banners.clear(_BANNER_ERRORS)
        if not config.alert_silence:
            self._end_quiet()

    # ── Checking ─────────────────────────────────────────────────────────

    @Slot()
    def _check(self) -> None:
        figures = self.context.session_figures()
        if figures is None:
            return
        config = self.context.config
        now = time.monotonic()
        errors = _errors(figures)
        if config.alert_error_lines and errors > self._errors_alerted:
            if self._acknowledged and now - self._errors_rose_at >= config.alert_pause_seconds:
                # None arrived for the time between alerts: a new alert.
                self._acknowledged = False
            self._errors_rose_at = now
            self._errors_alerted = errors
            title = self._show_errors(errors - self._errors_seen)
            if not self._acknowledged:
                self._draw_attention(AlertKind.ERROR_LINES, title)

        if figures.lines != self._lines:
            self._lines = figures.lines
            self._lines_changed_at = now
            self._end_quiet()
        elif (
            config.alert_silence
            and self._quiet_since is None
            and figures.ended is None
            and now - self._lines_changed_at >= config.alert_silence_seconds
        ):
            self._quiet_since = datetime.now()
            title = f"No line for {format_count(config.alert_silence_seconds, 'second')}"
            self.context.banners.show_banner(
                _BANNER_QUIET, banners.WARNING, title,
                "The board has sent nothing since "
                f"{format_time_of_day(self._quiet_since)}. This goes once lines arrive again.",
            )
            self._draw_attention(AlertKind.QUIET, title)

    def _show_errors(self, count: int) -> str:
        """Show the banner of ``count`` new error lines; returns its title."""
        latest = self._latest_error
        detail = (
            f"Latest, at {format_time_of_day(latest.timestamp)}: "
            f"{shorten(latest.text, ALERT_QUOTE_CHARS_MAX)}"
            if latest is not None else ""
        )
        title = format_count(count, "new error line")
        self.context.banners.show_banner(
            _BANNER_ERRORS, banners.WARNING, title, detail, on_dismiss=self._errors_dismissed,
        )
        return title

    @Slot()
    def _errors_dismissed(self) -> None:
        self._errors_seen = self._errors_alerted
        self._acknowledged = True

    def _end_quiet(self) -> None:
        if self._quiet_since is not None:
            self._quiet_since = None
            self.context.banners.clear(_BANNER_QUIET)

    def _draw_attention(self, kind: AlertKind, message: str) -> None:
        QApplication.alert(self.context.widget.window())
        self.context.alert(kind, message)


def _errors(figures: SessionFigures | None) -> int:
    if figures is None:
        return 0
    return sum(figures.by_level.get(level, 0) for level in ERROR_LEVELS)
