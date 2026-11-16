# SPDX-License-Identifier: GPL-3.0-or-later
"""The sound and the desktop notification of alerts, shared by every tab.

A tab's alert shows its banner and flashes the window at once
(``ui/features/alerts_feature.py``). What reaches the user away from
the window, the sound and the desktop notification, comes through here,
so that the alerts of every tab are paced together:

  Alerts that arrive within ``ALERT_GATHER_MS`` of each other go out as
  one: a hub pulled out loses its boards together.

  After that, nothing more sounds or notifies for the time between
  alerts set in Preferences. What happens meanwhile is kept, the latest
  of each kind for each tab, and goes out as one when that time ends.

The sound is the system's own and follows the user's sound settings, so
a system set to play no sounds stays silent: on Windows, the
Notification sound of the user's sound scheme; elsewhere, the system's
alert sound. A desktop notification is shown only while WireTrace is
not the application in use. On Windows it brings the Notification sound
with it (Qt does not ask the system to keep it silent), so no second
sound is played.

A notification names the tab and what happened, never the text of a
line: a notification can show on a locked screen.
"""

from __future__ import annotations

import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import shiboken6
from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QWidget

from app.constants import (
    ALERT_GATHER_MS,
    ALERT_NOTIFICATION_LINES,
    ALERT_NOTIFICATION_MS,
    MS_PER_SECOND,
)
from app.icon_loader import app_icon
from version import APP_NAME

if TYPE_CHECKING:
    from app.config import ConfigManager

logger = logging.getLogger(__name__)

#: The event of the Windows sound scheme an alert plays.
WINDOWS_ALERT_SOUND = "Notification.Default"
#: The name Windows gives that sound in its sound settings.
WINDOWS_ALERT_SOUND_NAME = "Notification"
#: Whether the system's desktop notification plays a sound of its own.
NOTIFICATION_BRINGS_SOUND = sys.platform == "win32"


class AlertKind(Enum):
    """What an alert is about; a tab has at most one waiting of each kind."""

    ERROR_LINES = "error lines"
    CONNECTION_LOST = "connection lost"
    QUIET = "quiet board"


@dataclass(frozen=True)
class Alert:
    """One tab's alert: which tab, what about, and what to say."""

    source: QWidget
    kind: AlertKind
    title: str
    message: str


def play_alert_sound(platform: str = sys.platform) -> None:
    """Play the system's alert sound, as the user's sound settings have it.

    Never waits for the sound: on Windows it is played asynchronously,
    and a scheme with no sound for the event plays nothing.
    """
    if platform == "win32":
        import winsound

        try:
            winsound.PlaySound(
                WINDOWS_ALERT_SOUND,
                winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
            )
        except RuntimeError:
            logger.debug("The sound scheme has no %s sound", WINDOWS_ALERT_SOUND)
        return
    QApplication.beep()


def notifications_unavailable() -> str:
    """Why desktop notifications cannot be shown here, or "" if they can."""
    if sys.platform == "darwin":
        return "Not yet available on macOS."
    if not QSystemTrayIcon.isSystemTrayAvailable() or not QSystemTrayIcon.supportsMessages():
        return f"This desktop has no notification area for {APP_NAME} to use."
    return ""


def notification_text(alerts: list[Alert]) -> tuple[str, str]:
    """The title and body of the notification of ``alerts`` (at least one)."""
    if len(alerts) == 1:
        alert = alerts[0]
        return alert.title, alert.message
    lines = [f"{alert.title}: {alert.message}" for alert in alerts[:ALERT_NOTIFICATION_LINES]]
    more = len(alerts) - ALERT_NOTIFICATION_LINES
    if more > 0:
        lines.append(f"And {more:,} more.")
    return f"{len(alerts):,} alerts", "\n".join(lines)


class AlertNotifier(QObject):
    """Sounds and notifies the alerts of every tab, paced together.

    Signals:
        show_requested(object):  The user clicked a notification of an alert
                                 from this tab (a ``QWidget``), or the tray
                                 icon (None); its window should come forward.
        notified():              A desktop notification was shown.
    """

    show_requested = Signal(object)
    notified = Signal()

    def __init__(
        self,
        config: ConfigManager,
        parent: QObject | None = None,
        play_sound: Callable[[], None] = play_alert_sound,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._play_sound = play_sound
        self._clock = clock
        # Alerts waiting to go out, the latest of each kind for each tab,
        # in the order they first arrived.
        self._pending: dict[tuple[QWidget, AlertKind], Alert] = {}
        # When the latest sound or notification went out (of ``clock``);
        # nothing more goes out until the time between alerts has passed.
        self._sent_at = -float("inf")
        # The tabs of the latest notification, for a click on it.
        self._notified: list[QWidget] = []
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._send)
        self._tray: QSystemTrayIcon | None = None
        self.apply_preferences()

    # ── Public API ───────────────────────────────────────────────────────

    def alert(self, source: QWidget, title: str, kind: AlertKind, message: str) -> None:
        """Sound and notify an alert of the tab ``source``, now or once the
        time between alerts has passed."""
        # A newer alert of the same kind replaces the one waiting, in its place.
        self._pending[(source, kind)] = Alert(source, kind, title, message)
        if not self._timer.isActive():
            self._start_timer()

    def forget(self, source: QWidget) -> None:
        """Drop what the tab ``source`` has waiting; it is closing."""
        for key in [key for key in self._pending if key[0] is source]:
            del self._pending[key]
        self._notified = [tab for tab in self._notified if tab is not source]
        if not self._pending:
            self._timer.stop()

    def apply_preferences(self) -> None:
        """Show the tray icon while desktop notifications are chosen and can
        be shown; alerts waiting go out by the time between alerts now set."""
        if self._timer.isActive():
            self._start_timer()
        wanted = self._config.alert_notification and not notifications_unavailable()
        if wanted and self._tray is None:
            tray = QSystemTrayIcon(app_icon(), self)
            tray.setToolTip(APP_NAME)
            tray.messageClicked.connect(self._on_message_clicked)
            tray.activated.connect(self._on_tray_activated)
            tray.show()
            self._tray = tray
        elif not wanted and self._tray is not None:
            tray, self._tray = self._tray, None
            tray.hide()
            tray.deleteLater()

    @property
    def tray_shown(self) -> bool:
        return self._tray is not None

    # ── Internal ─────────────────────────────────────────────────────────

    def _start_timer(self) -> None:
        """Wait at least ``ALERT_GATHER_MS``, and until the time between
        alerts has passed since the latest went out."""
        paused_for = self._sent_at + self._config.alert_pause_seconds - self._clock()
        wait_ms = round(paused_for * MS_PER_SECOND) if paused_for > 0 else 0
        self._timer.start(max(ALERT_GATHER_MS, wait_ms))

    @Slot()
    def _send(self) -> None:
        alerts = [alert for alert in self._pending.values() if shiboken6.isValid(alert.source)]
        self._pending.clear()
        if not alerts:
            return
        self._sent_at = self._clock()
        notified = self._notify(alerts)
        if self._config.alert_sound and not (notified and NOTIFICATION_BRINGS_SOUND):
            self._play_sound()

    def _notify(self, alerts: list[Alert]) -> bool:
        """Show the desktop notification of ``alerts``; returns whether it was shown."""
        if self._tray is None:
            return False
        if QGuiApplication.applicationState() == Qt.ApplicationState.ApplicationActive:
            return False
        title, body = notification_text(alerts)
        self._notified = [alert.source for alert in alerts]
        self._tray.showMessage(title, body, app_icon(), ALERT_NOTIFICATION_MS)
        self.notified.emit()
        return True

    @Slot()
    def _on_message_clicked(self) -> None:
        source = next((tab for tab in self._notified if shiboken6.isValid(tab)), None)
        self.show_requested.emit(source)

    @Slot(QSystemTrayIcon.ActivationReason)
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_requested.emit(None)
