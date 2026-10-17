# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared setup for the real-Qt GUI smoke suite.

Unlike ``tests/``, nothing here is stubbed: these tests import the real
PySide6 and pyqtgraph and construct real widgets. They run headless
through Qt's ``offscreen`` platform plugin, so they need no display
server and behave the same on Linux, macOS, and Windows runners.

Run with:  python -m pytest tests_gui
"""

from __future__ import annotations

import gc
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Select the headless platform plugin, with its test display, before Qt is
# imported. A developer who wants to watch the widgets can choose another
# platform from the environment.
from tests_gui.offscreen import use_offscreen_display  # noqa: E402

use_offscreen_display()

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import app.config as config_module  # noqa: E402
from app.config import ConfigManager  # noqa: E402
from app.main_thread_gc import MainThreadCollector  # noqa: E402
from core.transport import PortInfo  # noqa: E402
from ui.themes.fonts import register_bundled_fonts  # noqa: E402
from ui.themes.theme_manager import BASE_STYLE, ThemeManager  # noqa: E402


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:
    """The single QApplication shared by every test in the session."""
    instance = QApplication.instance() or QApplication([])
    # As in the application: Qt's cross-platform style and the bundled fonts.
    instance.setStyle(BASE_STYLE)
    register_bundled_fonts()
    # As in the application: no worker thread may destroy an interface object.
    collector = MainThreadCollector(instance)
    # Everything loaded so far lives for the whole session. Setting it
    # aside lets the collection after each test look at that test's
    # objects only.
    gc.collect()
    gc.freeze()
    yield instance
    gc.unfreeze()
    collector.stop()
    instance.closeAllWindows()
    instance.processEvents()


@pytest.fixture(autouse=True)
def no_leftover_windows(qapp: QApplication) -> Iterator[None]:
    """Destroy every window a test leaves behind.

    A window that outlives its test is restyled by every later theme
    change, which makes the suite slower with each test that runs.
    """
    yield
    for widget in qapp.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()
    gc.collect()


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ConfigManager:
    """A ConfigManager whose files live under the test's temp directory.

    Redirects both the preferences directory and the default log
    directory, so a test run never reads or writes the real user
    profile on any platform.
    """
    config_dir = tmp_path / "config"
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(config_module, "get_config_dir", lambda: str(config_dir))
    monkeypatch.setattr(config_module, "get_default_log_dir", lambda: str(log_dir))
    return ConfigManager()


@pytest.fixture(autouse=True)
def isolated_cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep session recordings, coloured icons and downloads out of the
    real user cache."""
    import ui.device_tab as device_tab_module
    import ui.themes.icons as icons_module
    import updater.download as download_module

    cache_dir = tmp_path / "cache"
    for module in (device_tab_module, icons_module, download_module):
        monkeypatch.setattr(module, "get_cache_dir", lambda: str(cache_dir))
    return cache_dir


@pytest.fixture(autouse=True)
def ports(monkeypatch: pytest.MonkeyPatch) -> list[PortInfo]:
    """The serial ports the interface finds: none, until a test adds some.

    A test run never lists, and so never depends on, the ports of the
    machine it runs on.
    """
    import ui.device_tab as device_tab_module

    found: list[PortInfo] = []
    monkeypatch.setattr(device_tab_module, "available_ports", lambda: list(found))
    return found


@pytest.fixture
def theme_manager(qapp: QApplication) -> ThemeManager:
    """A ThemeManager bound to the shared application."""
    return ThemeManager(qapp)
