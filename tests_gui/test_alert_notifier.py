# SPDX-License-Identifier: GPL-3.0-or-later
"""The sound and desktop notification of alerts, shared by every tab."""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

import ui.alert_notifier as notifier_module
from app.config import ConfigManager
from app.constants import (
    ALERT_GATHER_MS,
    ALERT_NOTIFICATION_LINES,
    ALERT_PAUSE_SECONDS_DEFAULT,
    ALERT_PAUSE_SECONDS_MAX,
    ALERT_PAUSE_SECONDS_MIN,
)
from tests_gui.support import wait_until
from ui.alert_notifier import Alert, AlertKind, AlertNotifier, notification_text

_LOST = AlertKind.CONNECTION_LOST
_ERRORS = AlertKind.ERROR_LINES


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class FakeTray:
    """Stands in for the tray icon; records each notification."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def showMessage(self, title, body, icon, msecs) -> None:
        self.messages.append((title, body))


@pytest.fixture
def sounds() -> list[int]:
    return []


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def notifier(qapp, config: ConfigManager, sounds, clock):
    made = AlertNotifier(config, play_sound=lambda: sounds.append(1), clock=clock)
    yield made
    made.deleteLater()


@pytest.fixture
def tabs(qapp):
    made = [QWidget(), QWidget(), QWidget()]
    yield made
    for widget in made:
        widget.deleteLater()


def _away(monkeypatch: pytest.MonkeyPatch) -> None:
    """WireTrace is not the application in use."""

    class Away:
        @staticmethod
        def applicationState():
            return Qt.ApplicationState.ApplicationInactive

    monkeypatch.setattr(notifier_module, "QGuiApplication", Away)


def _wait_sent(qapp, notifier: AlertNotifier) -> None:
    wait_until(qapp, lambda: not notifier._timer.isActive())


def _end_pause(qapp, notifier: AlertNotifier, clock: Clock, config: ConfigManager) -> None:
    clock.now += config.alert_pause_seconds
    notifier._timer.start(0)
    _wait_sent(qapp, notifier)


class TestSound:
    def test_alerts_close_together_sound_once(self, qapp, notifier, tabs, sounds):
        notifier.alert(tabs[0], "COM3", _LOST, "Connection lost")
        notifier.alert(tabs[1], "COM7", _LOST, "Connection lost")

        assert sounds == []
        assert notifier._timer.interval() == ALERT_GATHER_MS
        _wait_sent(qapp, notifier)
        assert sounds == [1]

    def test_nothing_more_sounds_until_the_time_between_alerts_has_passed(
        self, qapp, notifier, tabs, sounds, clock, config,
    ):
        notifier.alert(tabs[0], "COM3", _ERRORS, "1 new error line")
        _wait_sent(qapp, notifier)

        clock.now += config.alert_pause_seconds - 1
        notifier.alert(tabs[0], "COM3", _ERRORS, "2 new error lines")
        notifier.alert(tabs[0], "COM3", _ERRORS, "9 new error lines")
        assert notifier._timer.isActive()
        assert notifier._timer.interval() >= 1_000 - ALERT_GATHER_MS

        _end_pause(qapp, notifier, clock, config)
        assert sounds == [1, 1]

    def test_a_shorter_time_between_alerts_applies_to_alerts_already_waiting(
        self, qapp, notifier, tabs, sounds, clock, config,
    ):
        config.alert_pause_seconds = 600
        notifier.alert(tabs[0], "COM3", _ERRORS, "1 new error line")
        _wait_sent(qapp, notifier)
        notifier.alert(tabs[0], "COM3", _ERRORS, "2 new error lines")
        assert notifier._timer.interval() >= 599_000

        clock.now += 10
        config.alert_pause_seconds = 30
        notifier.apply_preferences()

        assert 19_000 <= notifier._timer.interval() <= 20_000

    def test_with_the_sound_off_nothing_is_played(self, qapp, notifier, tabs, sounds, config):
        config.alert_sound = False

        notifier.alert(tabs[0], "COM3", _LOST, "Connection lost")
        _wait_sent(qapp, notifier)

        assert sounds == []

    def test_a_closed_tab_leaves_nothing_waiting(self, qapp, notifier, tabs, sounds):
        notifier.alert(tabs[0], "COM3", _LOST, "Connection lost")
        notifier.forget(tabs[0])

        assert not notifier._timer.isActive()
        qapp.processEvents()
        assert sounds == []


class TestNotification:
    @pytest.fixture
    def tray(self, notifier) -> FakeTray:
        fake = FakeTray()
        notifier._tray = fake
        return fake

    def test_shown_only_while_another_application_is_in_use(
        self, qapp, monkeypatch, notifier, tray, tabs,
    ):
        class InUse:
            @staticmethod
            def applicationState():
                return Qt.ApplicationState.ApplicationActive

        monkeypatch.setattr(notifier_module, "QGuiApplication", InUse)
        notifier.alert(tabs[0], "COM3", _LOST, "Connection lost")
        _wait_sent(qapp, notifier)
        assert tray.messages == []

    def test_names_the_tab_and_what_happened(self, qapp, monkeypatch, notifier, tray, tabs):
        _away(monkeypatch)

        shown: list[bool] = []
        notifier.notified.connect(lambda: shown.append(True))
        notifier.alert(tabs[0], "COM3 · Pump", _LOST, "Connection lost")
        _wait_sent(qapp, notifier)

        assert tray.messages == [("COM3 · Pump", "Connection lost")]
        assert shown == [True]

    def test_alerts_kept_meanwhile_come_together_with_the_latest_of_each(
        self, qapp, monkeypatch, notifier, tray, tabs, clock, config,
    ):
        _away(monkeypatch)
        notifier.alert(tabs[0], "COM3", _ERRORS, "1 new error line")
        _wait_sent(qapp, notifier)

        notifier.alert(tabs[0], "COM3", _ERRORS, "4 new error lines")
        notifier.alert(tabs[1], "COM7", _LOST, "Connection lost")
        notifier.alert(tabs[0], "COM3", _ERRORS, "12 new error lines")
        _end_pause(qapp, notifier, clock, config)

        assert tray.messages[-1] == (
            "2 alerts", "COM3: 12 new error lines\nCOM7: Connection lost",
        )

    @pytest.mark.parametrize("brings_sound, played", [(True, []), (False, [1])])
    def test_never_two_sounds_for_one_alert(
        self, qapp, monkeypatch, notifier, tray, tabs, sounds, brings_sound, played,
    ):
        _away(monkeypatch)
        monkeypatch.setattr(notifier_module, "NOTIFICATION_BRINGS_SOUND", brings_sound)

        notifier.alert(tabs[0], "COM3", _LOST, "Connection lost")
        _wait_sent(qapp, notifier)

        assert len(tray.messages) == 1 and sounds == played

    def test_a_click_asks_for_the_tab_it_came_from(self, qapp, monkeypatch, notifier, tray, tabs):
        _away(monkeypatch)
        asked: list[object] = []
        notifier.show_requested.connect(asked.append)
        notifier.alert(tabs[1], "COM7", _LOST, "Connection lost")
        _wait_sent(qapp, notifier)

        notifier._on_message_clicked()

        assert asked == [tabs[1]]


class TestTrayIcon:
    def test_shown_only_while_notifications_are_chosen(self, qapp, monkeypatch, notifier, config):
        monkeypatch.setattr(notifier_module, "notifications_unavailable", lambda: "")
        assert not notifier.tray_shown

        config.alert_notification = True
        notifier.apply_preferences()
        assert notifier.tray_shown

        config.alert_notification = False
        notifier.apply_preferences()
        assert not notifier.tray_shown

    def test_never_shown_where_notifications_cannot_be(self, qapp, monkeypatch, notifier, config):
        monkeypatch.setattr(notifier_module, "notifications_unavailable", lambda: "No area.")
        config.alert_notification = True

        notifier.apply_preferences()

        assert not notifier.tray_shown


class TestText:
    def test_more_alerts_than_lines_say_how_many_more(self, qapp, tabs):
        alerts = [
            Alert(tabs[0], _LOST, f"COM{n}", "Connection lost")
            for n in range(ALERT_NOTIFICATION_LINES + 2)
        ]

        title, body = notification_text(alerts)

        assert title == f"{len(alerts)} alerts"
        assert body.splitlines()[-1] == "And 2 more."
        assert len(body.splitlines()) == ALERT_NOTIFICATION_LINES + 1


class TestSettings:
    def test_defaults(self, config):
        assert config.alert_sound is True
        assert config.alert_notification is False
        assert config.alert_pause_seconds == ALERT_PAUSE_SECONDS_DEFAULT

    @pytest.mark.parametrize("stored, read", [
        ("1", ALERT_PAUSE_SECONDS_MIN),
        ("999999", ALERT_PAUSE_SECONDS_MAX),
        ("nonsense", ALERT_PAUSE_SECONDS_DEFAULT),
    ])
    def test_the_time_between_alerts_stays_in_range(self, config, stored, read):
        config.set("Alerts", "pause_seconds", stored)

        assert config.alert_pause_seconds == read


def test_the_system_sound_never_waits(monkeypatch):
    played: list[str] = []

    class FakeApplication:
        @staticmethod
        def beep():
            played.append("beep")

    monkeypatch.setattr(notifier_module, "QApplication", FakeApplication)
    notifier_module.play_alert_sound("linux")

    assert played == ["beep"]


def test_on_windows_the_notification_sound_is_played_without_waiting(monkeypatch):
    import sys
    import types

    calls: list[tuple[str, int]] = []
    fake = types.SimpleNamespace(
        SND_ALIAS=0x10000, SND_ASYNC=0x1, SND_NODEFAULT=0x2,
        PlaySound=lambda sound, flags: calls.append((sound, flags)),
    )
    monkeypatch.setitem(sys.modules, "winsound", fake)

    notifier_module.play_alert_sound("win32")

    assert calls == [("Notification.Default", 0x10000 | 0x1 | 0x2)]


def test_on_windows_a_scheme_without_the_sound_stays_silent(monkeypatch):
    import sys
    import types

    def refuse(sound, flags):
        raise RuntimeError("Failed to play sound")

    fake = types.SimpleNamespace(SND_ALIAS=1, SND_ASYNC=2, SND_NODEFAULT=4, PlaySound=refuse)
    monkeypatch.setitem(sys.modules, "winsound", fake)

    assert notifier_module.play_alert_sound("win32") is None


def test_the_main_window_brings_forward_the_tab_an_alert_came_from(qapp, config, theme_manager):
    from tests_gui.screens import main_window

    with main_window(qapp, config, theme_manager) as (window, _port):
        first = window.current_tab()
        window.new_tab()
        assert window.current_tab() is not first

        window._alert_notifier.show_requested.emit(first)

        assert window.current_tab() is first
