# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the modules of a tab: switched on and off, guarded, fed their own lines."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

import ui.module_host as module_host
from app.config import ConfigManager
from app.constants import ThemeID
from app.modules import PLOT, ModuleSettings, ModuleSpec
from tests_gui.support import LoopbackPort, log_body, wait_until
from ui.device_tab import DeviceTab
from ui.themes.theme_manager import ThemeManager
from ui.widgets.banner import BannerArea
from ui.widgets.tab_toolbar import TabToolbar

_RECORDING = ModuleSpec("recording", "Recording", "Keeps what it is told.",
                        "tests_gui.fake_modules:RecordingModule")
_FAILING = ModuleSpec("failing", "Failing", "Raises.", "tests_gui.fake_modules:FailingModule")
_SLOT_FAILING = ModuleSpec("slot", "Bell", "Raises in a slot.",
                           "tests_gui.fake_modules:SlotFailingModule", default_enabled=False)
_NEVER = ModuleSpec("never", "Never", "Stays unloaded.",
                    "tests_gui.fake_module_never_loaded:NeverLoadedModule", default_enabled=False)


@pytest.fixture
def specs(monkeypatch: pytest.MonkeyPatch) -> list[ModuleSpec]:
    registry = [PLOT, _RECORDING, _FAILING, _NEVER, _SLOT_FAILING]
    monkeypatch.setattr(module_host, "MODULES", registry)
    return registry


@pytest.fixture
def port() -> LoopbackPort:
    return LoopbackPort()


@pytest.fixture
def make_tab(qapp: QApplication, config: ConfigManager, theme_manager: ThemeManager,
             port: LoopbackPort):
    tabs: list[DeviceTab] = []
    theme_manager.apply_theme(ThemeID.LIGHT)

    def factory() -> DeviceTab:
        tab = DeviceTab(theme_manager, config, transport_factory=port.factory)
        tab.show()
        qapp.processEvents()
        tabs.append(tab)
        return tab

    yield factory
    for tab in tabs:
        tab.ordered_shutdown()
        tab.close()
        tab.deleteLater()
    qapp.processEvents()


def _switch(config: ConfigManager, spec: ModuleSpec, enabled: bool) -> None:
    ModuleSettings(config).set_enabled(spec, enabled)


def _connect(qapp: QApplication, tab: DeviceTab) -> None:
    tab._on_connect("PORT0", 115200)
    wait_until(qapp, lambda: tab.is_connected)


def test_a_module_switched_off_is_never_imported(make_tab, specs, config):
    sys.modules.pop("tests_gui.fake_module_never_loaded", None)
    _switch(config, _FAILING, False)

    tab = make_tab()

    assert tab._modules.module(_NEVER) is None
    assert "tests_gui.fake_module_never_loaded" not in sys.modules


def test_a_module_reads_every_line_of_the_session(qapp, make_tab, specs, config, port):
    _switch(config, _FAILING, False)
    tab = make_tab()
    _connect(qapp, tab)
    module = tab._modules.module(_RECORDING)

    port.feed(b"".join(b"line %d\n" % n for n in range(500)))
    wait_until(qapp, lambda: len(module.texts) == 500)

    assert module.texts == [f"line {n}" for n in range(500)]
    assert module.events[:2] == ["start", "session_started"]
    tab.disconnect_device()
    wait_until(qapp, lambda: not tab.is_connected)
    assert module.events[-1] == "session_ended:False"


def test_switching_a_module_off_and_on_applies_at_once(qapp, make_tab, specs, config, port):
    _switch(config, _FAILING, False)
    tab = make_tab()
    _connect(qapp, tab)
    first = tab._modules.module(_RECORDING)

    _switch(config, _RECORDING, False)
    tab.apply_preferences()
    assert tab._modules.module(_RECORDING) is None
    assert first.events[-1] == "stop"

    _switch(config, _RECORDING, True)
    tab.apply_preferences()
    again = tab._modules.module(_RECORDING)
    port.feed(b"after\n")
    wait_until(qapp, lambda: again.texts == ["after"])
    assert again.events[:2] == ["start", "session_started"]


def test_a_failing_module_is_detached_and_logging_carries_on(
    qapp, make_tab, specs, config, port,
):
    tab = make_tab()
    _connect(qapp, tab)

    port.feed(b"first\n")
    wait_until(qapp, lambda: tab._modules.module(_FAILING) is None)
    port.feed(b"second\n")
    wait_until(qapp, lambda: tab._modules.module(_RECORDING).texts == ["first", "second"])

    banner = tab.findChild(BannerArea).banner("module")
    assert banner is not None
    assert "Failing stopped after an error" in banner.title_label.text()
    assert tab.is_connected
    tab.apply_preferences()
    assert tab._modules.module(_FAILING) is None


def test_a_module_whose_slot_raises_is_detached(qapp, make_tab, specs, config):
    _switch(config, _FAILING, False)
    _switch(config, _SLOT_FAILING, True)
    tab = make_tab()
    module = tab._modules.module(_SLOT_FAILING)
    bell = module.bell

    bell.rung.emit(1)
    assert tab._modules.module(_SLOT_FAILING) is module
    bell.rung.emit(2)
    wait_until(qapp, lambda: tab._modules.module(_SLOT_FAILING) is None)
    qapp.processEvents()
    bell.rung.emit(3)

    assert module.rings == [1, 2]
    assert "Bell stopped after an error" in tab.findChild(BannerArea).banner("module").title_label.text()


def test_a_slot_guarded_again_reuses_its_wrapper(qapp, make_tab, specs, config):
    _switch(config, _FAILING, False)
    _switch(config, _SLOT_FAILING, True)
    module = make_tab()._modules.module(_SLOT_FAILING)
    before = len(module.children())

    wrappers = {module.guarded(module._ring) for _ in range(100)}

    assert len(wrappers) == 1
    assert len(module.children()) == before


def test_the_plot_switched_off_leaves_no_trace_in_the_tab(qapp, make_tab, config, port):
    _switch(config, PLOT, False)
    tab = make_tab()
    _connect(qapp, tab)

    assert tab.plot is None
    assert tab.findChild(TabToolbar)._plot.isHidden()
    tab.toggle_plot()
    assert tab.is_plot_visible() is False

    _switch(config, PLOT, True)
    tab.apply_preferences()
    tab.toggle_plot()
    port.feed(b"".join(b"temp: %d\n" % n for n in range(60)))
    wait_until(qapp, lambda: tab.plot.engine.columns == ["temp"])
    assert tab.is_plot_visible() is True


def test_the_log_holds_every_line_whatever_a_module_does(
    qapp, make_tab, specs, config, port, tmp_path: Path,
):
    from core.log_writer import LogRequest

    tab = make_tab()
    _connect(qapp, tab)
    log = tmp_path / "all.txt"
    tab._pipeline.start_log(LogRequest(
        text_path=str(log), csv_path=None, session_name="all", port_name="PORT0",
        baud_rate=115200, description="", split_bytes=0,
    ))

    port.feed(b"".join(b"%d\n" % n for n in range(1000)))
    wait_until(qapp, lambda: tab.console.total_count == 1000)
    tab._pipeline.stop_log()

    assert log_body(str(log))[-1].endswith("] 999")
    assert len([line for line in log_body(str(log)) if not line.split("] ", 1)[1].startswith("[")]) == 1000
