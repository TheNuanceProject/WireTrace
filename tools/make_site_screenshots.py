# SPDX-License-Identifier: GPL-3.0-or-later
"""Make the pictures of WireTrace for the project's website.

Like ``tools/make_guide_screenshots.py``, whose imagined devices it
shares, this starts WireTrace off screen with stand-ins for serial
ports, puts it in each state the website shows, and saves what the
window shows, once in the light theme and once in the dark:

- ``session``: three device tabs, the first logging, levels in colour.
- ``first-screen``: a new tab listing three serial ports.
- ``plot``: the live plot in panels, two cursors measuring a load step.
- ``connection-lost``: the bar after a lost connection.

Usage:
    python tools/make_site_screenshots.py OUTPUT-FOLDER

The pictures are written as ``wiretrace-<scene>-<theme>.png``. No
serial device is needed, no window appears, and your own settings are
neither read nor changed.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests_gui.offscreen import use_offscreen_display  # noqa: E402

use_offscreen_display()

from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

import app.config as config_module  # noqa: E402
import ui.device_tab as device_tab_module  # noqa: E402
import ui.themes.icons as icons_module  # noqa: E402
from app.config import ConfigManager  # noqa: E402
from app.constants import ThemeID  # noqa: E402
from core.transport import PortInfo, Transport  # noqa: E402
from tests_gui.support import LoopbackTransport, wait_until  # noqa: E402
from tools.make_guide_screenshots import (  # noqa: E402
    BOOT_LINES,
    CURSOR_SECONDS,
    PLOT_SAMPLES_PER_SECOND,
    PLOT_SECONDS,
    RANDOM_SEED,
    motor_line,
)
from ui.device_tab import DeviceTab  # noqa: E402
from ui.dialogs.new_log_dialog import NewLogDialog  # noqa: E402
from ui.formatting import format_count  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402
from ui.themes.fonts import register_bundled_fonts  # noqa: E402
from ui.themes.theme_manager import BASE_STYLE, ThemeManager  # noqa: E402
from ui.widgets.plot_view import PlotLayout  # noqa: E402
from ui.widgets.toast import Toast  # noqa: E402

#: Wide enough for every control of a tab, in the proportions of a
#: laptop screen.
WINDOW_SIZE = (1280, 760)
EVENT_ROUNDS = 40
BAUD = 115200

#: The three imagined devices: the port each is on, and its tab's name.
MOTOR = PortInfo("COM7", "USB Serial Device", "Example Devices")
SENSOR = PortInfo("COM3", "USB-SERIAL CH340", "wch.cn")
SUPPLY = PortInfo("COM9", "Silicon Labs CP210x", "Silicon Labs")
PORTS = (MOTOR, SENSOR, SUPPLY)
TAB_NAMES = {MOTOR.device: "Motor controller", SENSOR.device: "ESP32 sensor", SUPPLY.device: "Bench PSU"}

#: Readings that arrive before the log starts, and while it runs. Together
#: with the boot lines they fill the console without scrolling it.
READINGS_BEFORE_LOG = 6
READINGS_WHILE_LOGGING = 8
#: The spacing of readings while logging: long enough that the status bar
#: shows a line rate, which it measures once a second.
READING_INTERVAL_MS = 150
#: The spacing of the other devices' lines.
LINE_INTERVAL_MS = 30
SENSOR_READINGS = 14
SUPPLY_READINGS = 8


def sensor_lines() -> list[str]:
    """An ESP32 starting up in ESP-IDF's log format, then its readings."""
    noise = random.Random(RANDOM_SEED)
    lines = [
        "I (312) cpu_start: Starting scheduler on PRO CPU.",
        "I (318) wifi: mode: station",
        "W (402) wifi: access point not found, retrying",
        "I (1210) mqtt: connected to broker",
        "E (1534) i2c: bus is busy, retrying",
        "I (1541) i2c: bus recovered",
    ]
    for second in range(SENSOR_READINGS):
        lines.append(
            f"I ({2000 + second * 1000}) sensor: temp={24.6 + noise.uniform(-0.3, 0.3):.1f} "
            f"hum={41.2 + noise.uniform(-1, 1):.1f} pressure={1008.4 + noise.uniform(-0.4, 0.4):.1f}"
        )
    return lines


def supply_lines() -> list[str]:
    """A bench power supply reporting its output."""
    noise = random.Random(RANDOM_SEED)
    lines = []
    for _ in range(SUPPLY_READINGS):
        volts, amps = 12.0 + noise.uniform(-0.02, 0.02), 0.8 + noise.uniform(-0.01, 0.01)
        lines.append(f"V={volts:.2f} I={amps:.3f} P={volts * amps:.2f}")
    return lines


class Ports:
    """Hands each tab a stand-in for its serial port, by port name."""

    def __init__(self) -> None:
        self.by_port: dict[str, LoopbackTransport] = {}

    def factory(self, port: str, baud: int) -> tuple[Transport | None, str]:
        transport = LoopbackTransport()
        transport.open(port, baud)
        self.by_port[port] = transport
        return transport, ""


class Studio:
    """Runs the application off screen and saves pictures of it."""

    def __init__(self, application: QApplication, theme: ThemeID, workspace: Path, output: Path) -> None:
        self._app = application
        self._theme = theme
        self._output = output
        self.config = ConfigManager()
        self.config.default_log_directory = str(workspace / "Logs")
        self.themes = ThemeManager(application)
        self.themes.apply_theme(theme)
        self.ports = Ports()
        self.window = MainWindow(self.config, self.themes, transport_factory=self.ports.factory)
        self.window.resize(*WINDOW_SIZE)
        self.window.show()
        self.settle()

    def settle(self) -> None:
        for _ in range(EVENT_ROUNDS):
            self._app.processEvents()

    def wait(self, condition: Callable[[], bool]) -> None:
        wait_until(self._app, condition)

    def save(self, name: str) -> None:
        self.settle()
        # A message that finished is not part of what the picture shows.
        for toast in self.window.findChildren(Toast):
            toast.hide()
        self.settle()
        path = self._output / f"wiretrace-{name}-{self._theme.value}.png"
        if not self.window.grab().save(str(path)):
            raise OSError(f"could not write {path}")
        print(f"  {path}")

    def connect(self, tab: DeviceTab, port: PortInfo) -> None:
        tab.refresh_ports()
        self.settle()
        tab._on_connect(port.device, BAUD)
        self.wait(lambda: tab.is_connected)
        tab.rename(TAB_NAMES[port.device])

    def feed(self, tab: DeviceTab, port: PortInfo, lines: Iterable[str], interval_ms: int) -> None:
        """Send lines one at a time, as a device does, so each has a time of its own."""
        for line in lines:
            expected = tab.console.total_count + 1
            self.ports.by_port[port.device].feed(f"{line}\n".encode())
            self.wait(lambda expected=expected: tab.console.total_count == expected)
            QTest.qWait(interval_ms)

    # ── Scenes ───────────────────────────────────────────────────────────

    def first_screen(self) -> DeviceTab:
        tab = self.window.current_tab()
        tab.refresh_ports()
        self.save("first-screen")
        return tab

    def session(self, motor: DeviceTab) -> None:
        noise = random.Random(RANDOM_SEED)
        self.connect(motor, MOTOR)
        motor.start_log()
        self.settle()
        motor.saved_commands.replace(["status", "reset", "get config"])
        readings = [motor_line(second, noise) for second in range(READINGS_BEFORE_LOG + READINGS_WHILE_LOGGING)]
        self.feed(motor, MOTOR, [*BOOT_LINES, *readings[:READINGS_BEFORE_LOG]], LINE_INTERVAL_MS)

        sensor = self.window.new_tab()
        self.connect(sensor, SENSOR)
        self.feed(sensor, SENSOR, sensor_lines(), LINE_INTERVAL_MS)
        supply = self.window.new_tab()
        self.connect(supply, SUPPLY)
        self.feed(supply, SUPPLY, supply_lines(), LINE_INTERVAL_MS)

        self.window._tabs.setCurrentWidget(motor)
        self.feed(motor, MOTOR, readings[READINGS_BEFORE_LOG:], READING_INTERVAL_MS)
        shown = format_count(motor.console.total_count, "line")
        self.wait(lambda: shown in motor._status_bar.texts())
        self.save("session")

    def plot(self, motor: DeviceTab) -> None:
        motor.toggle_plot()
        self.settle()
        view = motor._plot_view
        # The plot is given a minute of samples of its own, spread over
        # time, as in the user guide's picture.
        view.reset_session()
        noise = random.Random(RANDOM_SEED)
        start = datetime.now() - timedelta(seconds=PLOT_SECONDS)
        for index in range(PLOT_SECONDS * PLOT_SAMPLES_PER_SECOND):
            second = index / PLOT_SAMPLES_PER_SECOND
            motor._plot_engine.process(motor_line(second, noise), start + timedelta(seconds=second))
        view.set_layout_mode(PlotLayout.PANELS)
        view._pause_btn.setChecked(True)
        view._redraw(force=True)
        view.fit()
        view.set_measuring(True)
        for index, second in enumerate(CURSOR_SECONDS):
            view.set_cursor(index, second)
        view._update_figures()
        self.save("plot")
        motor.toggle_plot()

    def connection_lost(self, motor: DeviceTab) -> None:
        self.ports.by_port[MOTOR.device].drop()
        self.wait(lambda: not motor.is_connected)
        self.save("connection-lost")

    def close(self) -> None:
        self.window.shutdown_all_tabs()
        self.window.close()
        self.window.deleteLater()
        self.settle()


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path, help="the folder to write the pictures to")
    options = parser.parse_args(arguments)
    options.output.mkdir(parents=True, exist_ok=True)

    if QApplication.instance() is not None:
        raise RuntimeError("the pictures must be made in a process of their own, before any QApplication exists")
    # The pictures are made at the window's own size, whatever scale the
    # system or the environment would set; the website sizes them itself.
    # Set here, not at import, so importing this module changes nothing.
    os.environ["QT_SCALE_FACTOR"] = "1"
    application = QApplication(sys.argv[:1])
    # As in the application: Qt's cross-platform style and the bundled fonts.
    application.setStyle(BASE_STYLE)
    register_bundled_fonts()
    # The log is started without its dialog, with the dialog's own defaults.
    NewLogDialog.exec = lambda dialog: QDialog.DialogCode.Accepted
    device_tab_module.available_ports = lambda: list(PORTS)
    with tempfile.TemporaryDirectory(prefix="wiretrace-site-") as folder:
        for theme in ThemeID:
            # Each theme starts from fresh settings, so nothing the first
            # theme's run remembered (recent logs, ports seen) shows in the next.
            workspace = Path(folder) / theme.value
            config_module.get_config_dir = lambda workspace=workspace: str(workspace / "config")
            for module in (device_tab_module, icons_module):
                module.get_cache_dir = lambda workspace=workspace: str(workspace / "cache")
            print(f"{theme.value}:")
            studio = Studio(application, theme, workspace, options.output)
            motor = studio.first_screen()
            studio.session(motor)
            studio.plot(motor)
            studio.connection_lost(motor)
            studio.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
