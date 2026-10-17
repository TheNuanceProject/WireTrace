# SPDX-License-Identifier: GPL-3.0-or-later
"""Make the pictures of the user guide from the running application.

The pictures in ``resources/help/images`` are not drawn by hand: this
script starts WireTrace with a stand-in for the serial port, puts it in
each state the guide describes, and saves what the window shows, once
in the light theme and once in the dark. Run it again after the
interface changes, on the system whose look the guide should have.

Usage:
    python tools/make_guide_screenshots.py

No serial device is needed and no window appears: the application is
drawn off screen. Preferences and logs go to a temporary folder, so
your own settings are neither read nor changed.
"""

from __future__ import annotations

import math
import random
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests_gui.offscreen import use_offscreen_display  # noqa: E402

use_offscreen_display()

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

import app.config as config_module  # noqa: E402
import ui.device_tab as device_tab_module  # noqa: E402
import ui.themes.icons as icons_module  # noqa: E402
from app.config import ConfigManager  # noqa: E402
from app.constants import ExportFormat, ThemeID  # noqa: E402
from core.transport import PortInfo  # noqa: E402
from tests_gui.support import LoopbackPort, wait_until  # noqa: E402
from ui.dialogs.new_log_dialog import NewLogDialog  # noqa: E402
from ui.dialogs.preferences_dialog import PreferencesDialog  # noqa: E402
from ui.formatting import format_count  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.themes.fonts import register_bundled_fonts  # noqa: E402
from ui.themes.theme_manager import BASE_STYLE, ThemeManager  # noqa: E402
from ui.widgets.plot_view import PlotLayout  # noqa: E402

IMAGES_DIR = ROOT / "resources" / "help" / "images"

WINDOW_SIZE = (1180, 720)
EVENT_ROUNDS = 30

#: The ports the first screen lists, and the one the pictures connect to.
PORTS = (
    PortInfo("COM7", "USB Serial Device", "Example Devices"),
    PortInfo("COM3", "USB-SERIAL CH340", "wch.cn"),
)
PORT = PORTS[0].device

#: The log folder the dialogs show. The pictures are made in a temporary
#: folder, whose name would mean nothing to a reader.
LOG_FOLDER = "~/Documents/WireTrace/Logs"

BOOT_LINES = (
    "[INFO] UART1 initialized at 9600 baud",
    "[DEBUG] I2C0 scanning bus...",
    "[WARNING] I2C0: no response at 0x29",
    "[ERROR] SD: write failed (card is read-only)",
    "[INFO] Motor controller ready",
)
LINE_INTERVAL_MS = 120
PLOT_SECONDS = 60
PLOT_SAMPLES_PER_SECOND = 10
STEP_SECONDS = (30, 36)
CURSOR_SECONDS = (29.0, 33.0)
RANDOM_SEED = 4


def motor_line(second: float, noise: random.Random) -> str:
    """One line of an imagined motor test, with a load step in the middle."""
    loaded = STEP_SECONDS[0] < second < STEP_SECONDS[1]
    rpm = 1460 + 14 * math.sin(second / 4) + noise.uniform(-3, 3) - (40 if loaded else 0)
    current = 2.4 + (1.6 if loaded else 0) + noise.uniform(-0.05, 0.05)
    voltage = 12.5 - (0.25 if loaded else 0) + noise.uniform(-0.02, 0.02)
    temperature = 52.2 - second * 0.15 + noise.uniform(-0.1, 0.1)
    return (
        f"RPM: {rpm:.0f}, Current: {current:.2f}, "
        f"Voltage: {voltage:.2f}, MotorTemp: {temperature:.1f}"
    )


class Studio:
    """Runs the application off screen and saves pictures of it."""

    def __init__(self, application: QApplication, theme: ThemeID, workspace: Path) -> None:
        self._app = application
        self._theme = theme
        self._found: list[PortInfo] = []
        device_tab_module.available_ports = lambda: list(self._found)
        self.config = ConfigManager()
        self.config.default_log_directory = str(workspace / "Logs")
        self.themes = ThemeManager(application)
        self.themes.apply_theme(theme)
        self.port = LoopbackPort()
        self.window = MainWindow(self.config, self.themes, transport_factory=self.port.factory)
        self.window.resize(*WINDOW_SIZE)
        self.window.show()
        self.settle()

    def settle(self) -> None:
        for _ in range(EVENT_ROUNDS):
            self._app.processEvents()

    def save(self, widget: QWidget, name: str) -> None:
        self.settle()
        path = IMAGES_DIR / f"{name}-{self._theme.value}.png"
        if not widget.grab().save(str(path)):
            raise OSError(f"could not write {path}")
        print(f"  {path.relative_to(ROOT)}")

    def wait(self, condition: Callable[[], bool]) -> None:
        wait_until(self._app, condition)

    # ── Scenes ───────────────────────────────────────────────────────────

    def first_screen(self) -> None:
        self._found[:] = PORTS
        self.window.current_tab().refresh_ports()
        self.save(self.window, "first-screen")

    def session(self) -> None:
        tab = self.window.current_tab()
        tab.connect_device()
        self.wait(lambda: tab.is_connected)
        noise = random.Random(RANDOM_SEED)
        lines = [*BOOT_LINES, *(motor_line(second, noise) for second in range(12))]
        # One line at a time, as a device sends them, so that each line
        # shows a time of its own.
        for count, line in enumerate(lines, start=1):
            self.port.feed(f"{line}\n".encode())
            self.wait(lambda count=count: tab.console.total_count == count)
            QTest.qWait(LINE_INTERVAL_MS)
        shown = format_count(len(lines), "line")
        self.wait(lambda: shown in tab._status_bar.texts())
        tab.saved_commands.replace(["status", "reset", "get config"])
        self.save(self.window, "session")

    def plot(self) -> None:
        tab = self.window.current_tab()
        tab.toggle_plot()
        self.settle()
        view = tab._plot_view
        # The lines in the console all arrived in one instant. The plot
        # is given a minute of samples of its own, spread over time.
        view.reset_session()
        noise = random.Random(RANDOM_SEED)
        start = datetime.now() - timedelta(seconds=PLOT_SECONDS)
        for index in range(PLOT_SECONDS * PLOT_SAMPLES_PER_SECOND):
            second = index / PLOT_SAMPLES_PER_SECOND
            tab._plot_engine.process(motor_line(second, noise), start + timedelta(seconds=second))
        view.set_layout_mode(PlotLayout.PANELS)
        view._pause_btn.setChecked(True)
        view._redraw(force=True)
        view.fit()
        view.set_measuring(True)
        for index, second in enumerate(CURSOR_SECONDS):
            view.set_cursor(index, second)
        view._update_figures()
        self.save(self.window, "plot")
        tab.toggle_plot()

    def connection_lost(self) -> None:
        tab = self.window.current_tab()
        self.port.transport.drop()
        self.wait(lambda: not tab.is_connected)
        self.save(self.window, "connection-lost")

    def dialogs(self) -> None:
        self.config.default_log_directory = LOG_FOLDER
        log = NewLogDialog(
            PORT, self.config.default_log_directory, ExportFormat.BOTH,
            self.config.log_split_bytes, self.window,
        )
        log.show()
        self.save(log, "start-a-log")
        log.close()

        preferences = PreferencesDialog(self.config, self.themes, self.window)
        preferences.show_page("Logging")
        preferences.show()
        self.save(preferences, "preferences")
        preferences.close()

    def close(self) -> None:
        self.window.shutdown_all_tabs()
        self.window.close()
        self.window.deleteLater()
        self.settle()


def main() -> int:
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    application = QApplication.instance() or QApplication(sys.argv[:1])
    # As in the application: Qt's cross-platform style and the bundled fonts.
    application.setStyle(BASE_STYLE)
    register_bundled_fonts()
    with tempfile.TemporaryDirectory(prefix="wiretrace-guide-") as folder:
        workspace = Path(folder)
        config_module.get_config_dir = lambda: str(workspace / "config")
        for module in (device_tab_module, icons_module):
            module.get_cache_dir = lambda: str(workspace / "cache")
        for theme in ThemeID:
            print(f"{theme.value}:")
            studio = Studio(application, theme, workspace)
            studio.first_screen()
            studio.session()
            studio.plot()
            studio.connection_lost()
            studio.dialogs()
            studio.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
