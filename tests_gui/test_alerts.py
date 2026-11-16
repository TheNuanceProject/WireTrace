# SPDX-License-Identifier: GPL-3.0-or-later
"""Alerts: error lines, a lost connection and a quiet board, in a tab."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

import ui.alert_notifier as notifier_module
import ui.features.alerts_feature as alerts_module
from app.config import ConfigManager
from app.constants import ALERT_QUOTE_CHARS_MAX, ThemeID
from app.modules import ALERTS, ModuleSettings
from tests_gui.support import LoopbackPort, wait_until
from ui.device_tab import DeviceTab
from ui.themes.theme_manager import ThemeManager
from ui.widgets.banner import BannerArea
from ui.widgets.connect_page import ConnectPage
from updater.usage import Feature


class Attention:
    """Records what the alerts asked the system for."""

    def __init__(self) -> None:
        self.flashes = 0
        self.sounds = 0

    def sound(self) -> None:
        self.sounds += 1


@pytest.fixture
def attention(monkeypatch: pytest.MonkeyPatch) -> Attention:
    seen = Attention()

    class FakeApplication:
        @staticmethod
        def alert(widget, duration=0):
            seen.flashes += 1

    monkeypatch.setattr(alerts_module, "QApplication", FakeApplication)
    return seen


class Clock:
    """The time the alerts module reads: it moves only when a test moves it."""

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Give the alerts module a clock of its own, so no test depends on how fast it runs."""
    frozen = Clock()
    monkeypatch.setattr(alerts_module, "time", frozen)
    return frozen


@pytest.fixture
def port() -> LoopbackPort:
    return LoopbackPort()


@pytest.fixture
def make_tab(
    qapp: QApplication, config: ConfigManager, theme_manager: ThemeManager, port, attention,
):
    tabs: list[DeviceTab] = []
    theme_manager.apply_theme(ThemeID.LIGHT)
    ModuleSettings(config).set_enabled(ALERTS, True)

    def factory() -> DeviceTab:
        # The tab's own notifier, as a tab makes for itself, with the
        # system's sound recorded instead of played.
        notifier = notifier_module.AlertNotifier(config, play_sound=attention.sound)
        tab = DeviceTab(
            theme_manager, config, transport_factory=port.factory, read_ports=port.identities,
            alert_notifier=notifier,
        )
        notifier.setParent(tab)
        tab.show()
        tab.findChild(ConnectPage).connect_requested.emit("PORT0", 115200)
        wait_until(qapp, lambda: tab.is_connected)
        tabs.append(tab)
        return tab

    yield factory
    for tab in tabs:
        tab.ordered_shutdown()
        tab.close()
        tab.deleteLater()
    qapp.processEvents()


def _banner(tab: DeviceTab, key: str):
    return tab.findChild(BannerArea).banner(key)


def test_new_error_lines_are_counted_until_dismissed(qapp, make_tab, port, config, attention):
    config.alert_error_lines = True
    tab = make_tab()
    noted: list[object] = []
    tab.usage_noted.connect(noted.append)

    port.feed(b"E (10) wifi: timeout\nall fine\n[ERROR] sensor missing\n")
    wait_until(qapp, lambda: _banner(tab, "alert-errors") is not None)
    banner = _banner(tab, "alert-errors")
    assert banner.title_label.text() == "2 new error lines"
    assert Feature.ALERTS in noted
    assert banner.detail_label.text().endswith(": [ERROR] sensor missing")
    assert attention.flashes == 1

    banner.close_button.click()
    port.feed(b"CRITICAL: brownout\n")
    wait_until(qapp, lambda: _banner(tab, "alert-errors") is not None)
    assert _banner(tab, "alert-errors").title_label.text() == "1 new error line"


def test_a_long_error_line_is_quoted_shortened(qapp, make_tab, port, config, attention):
    config.alert_error_lines = True
    tab = make_tab()

    port.feed(b"[ERROR] " + b"overflow " * 1_000 + b"\n")
    wait_until(qapp, lambda: _banner(tab, "alert-errors") is not None)
    quote = _banner(tab, "alert-errors").detail_label.text().split(": ", 1)[1]

    assert len(quote) <= ALERT_QUOTE_CHARS_MAX
    assert quote.startswith("[ERROR] overflow") and quote.endswith("…")


def test_error_alerts_can_be_switched_off(qapp, make_tab, port, config, attention):
    config.alert_error_lines = False
    tab = make_tab()

    port.feed(b"E (10) wifi: timeout\n")
    wait_until(qapp, lambda: tab.session.total_lines == 1)
    qapp.processEvents()
    tab._modules.module(ALERTS)._check()

    assert _banner(tab, "alert-errors") is None
    assert attention.flashes == 0


def test_a_quiet_board_is_reported_until_lines_arrive(qapp, make_tab, port, config, attention):
    config.alert_silence = True
    config.alert_silence_seconds = 1
    tab = make_tab()

    wait_until(qapp, lambda: _banner(tab, "alert-quiet") is not None, timeout=5)
    assert _banner(tab, "alert-quiet").title_label.text() == "No line for 1 second"

    port.feed(b"back\n")
    wait_until(qapp, lambda: _banner(tab, "alert-quiet") is None)


def test_a_lost_connection_draws_attention_with_the_sound_chosen(
    qapp, make_tab, port, config, attention,
):
    config.alert_sound = True
    tab = make_tab()

    port.transport.drop()
    wait_until(qapp, lambda: tab.is_waiting)
    assert attention.flashes == 1
    wait_until(qapp, lambda: attention.sounds == 1)

    tab.stop_waiting()
    wait_until(qapp, lambda: not tab.is_connected)
    qapp.processEvents()

    assert (attention.flashes, attention.sounds) == (1, 1)


def test_a_stream_of_error_lines_sounds_once_until_the_time_between_alerts(
    qapp, make_tab, port, config, attention,
):
    config.alert_error_lines = True
    tab = make_tab()
    alerts = tab.findChild(notifier_module.AlertNotifier)

    for n in range(6):
        port.feed(b"E (%d) wifi: timeout\n" % n)
        wait_until(qapp, lambda n=n: tab.session.total_lines == n + 1)
        tab._modules.module(ALERTS)._check()
    wait_until(qapp, lambda: attention.sounds == 1)
    qapp.processEvents()

    assert attention.sounds == 1
    assert alerts._timer.isActive() and alerts._timer.interval() > 1_000


def test_an_alert_never_carries_the_text_of_a_line(
    qapp, make_tab, port, config, attention, monkeypatch,
):
    config.alert_error_lines = True
    tab = make_tab()
    alerts = tab.findChild(notifier_module.AlertNotifier)
    sent: list[tuple] = []
    monkeypatch.setattr(alerts, "alert", lambda *alert: sent.append(alert))

    port.feed(b"E (10) secret token 1234\n")
    wait_until(qapp, lambda: bool(sent))

    assert sent == [(tab, tab.title, notifier_module.AlertKind.ERROR_LINES, "1 new error line")]


def test_with_alerts_off_nothing_alerts(qapp, make_tab, port, config, attention):
    tab = make_tab()
    ModuleSettings(config).set_enabled(ALERTS, False)
    tab.apply_preferences()

    port.feed(b"E (10) wifi: timeout\n")
    port.transport.drop()
    wait_until(qapp, lambda: tab.is_waiting)

    assert _banner(tab, "alert-errors") is None
    assert attention.flashes == 0


def test_a_tab_switched_off_does_not_alert_while_others_do(qapp, make_tab, port, attention):
    tab = make_tab()
    changes: list[bool] = []
    tab.alerts_changed.connect(changes.append)

    tab.set_alerts(False)
    port.feed(b"E (10) wifi: timeout\n")
    wait_until(qapp, lambda: tab.session.total_lines == 1)
    qapp.processEvents()

    assert changes == [False]
    assert tab.alerts_enabled is False
    assert _banner(tab, "alert-errors") is None
    assert attention.flashes == 0

    tab.apply_preferences()
    assert tab.alerts_enabled is False


def test_a_tab_can_alert_while_alerts_are_off_in_preferences(
    qapp, make_tab, port, config, attention,
):
    config.alert_error_lines = True
    tab = make_tab()
    ModuleSettings(config).set_enabled(ALERTS, False)
    tab.apply_preferences()
    assert tab.alerts_enabled is False

    tab.set_alerts(True)
    port.feed(b"E (10) wifi: timeout\n")

    wait_until(qapp, lambda: _banner(tab, "alert-errors") is not None)
    assert tab.alerts_enabled is True


class TestErrorReminders:
    """Error lines remind until the banner is dismissed, like an alarm."""

    @pytest.fixture
    def sent(self, qapp, clock, make_tab, port, config, attention, monkeypatch):
        config.alert_error_lines = True
        tab = make_tab()
        alerts = tab.findChild(notifier_module.AlertNotifier)
        recorded: list[str] = []
        monkeypatch.setattr(alerts, "alert", lambda source, title, kind, message: recorded.append(message))
        return tab, recorded

    @staticmethod
    def _error(qapp, tab, port, number: int) -> None:
        before = tab.session.total_lines
        port.feed(b"E (%d) wifi: timeout\n" % number)
        wait_until(qapp, lambda: tab.session.total_lines == before + 1)
        qapp.processEvents()
        tab._modules.module(ALERTS)._check()

    def test_unacknowledged_error_lines_keep_reminding(self, qapp, port, attention, sent):
        tab, recorded = sent

        for number in range(3):
            self._error(qapp, tab, port, number)

        assert recorded == ["1 new error line", "2 new error lines", "3 new error lines"]
        assert attention.flashes == 3

    def test_dismissing_the_banner_acknowledges_them(self, qapp, port, attention, sent):
        tab, recorded = sent
        self._error(qapp, tab, port, 0)
        _banner(tab, "alert-errors").close_button.click()

        self._error(qapp, tab, port, 1)
        self._error(qapp, tab, port, 2)

        assert recorded == ["1 new error line"]
        assert attention.flashes == 1
        assert _banner(tab, "alert-errors").title_label.text() == "2 new error lines"

    def test_errors_back_after_the_time_between_alerts_alert_again(
        self, qapp, clock, port, config, attention, sent,
    ):
        tab, recorded = sent
        self._error(qapp, tab, port, 0)
        _banner(tab, "alert-errors").close_button.click()

        clock.now += config.alert_pause_seconds - 1
        self._error(qapp, tab, port, 1)
        assert recorded == ["1 new error line"]

        # A new alert, counting every line since the banner was dismissed.
        clock.now += config.alert_pause_seconds
        self._error(qapp, tab, port, 2)
        assert recorded == ["1 new error line", "2 new error lines"]
        assert attention.flashes == 2
