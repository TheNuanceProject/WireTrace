# SPDX-License-Identifier: GPL-3.0-or-later
"""The benchmark tool (tools/benchmark.py), run small.

The release measures WireTrace with this tool on GitHub's machines; these
tests run each of its measurements at a fraction of its size on every
system the suite runs on, so that the tool itself is proven before a
release depends on it. The figures are not checked here, only that each
measurement is made, every line arrives, and nothing leaves the
temporary home folders.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psutil
import pytest
from PySide6.QtWidgets import QApplication

import ui.device_tab as device_tab_module
from app.config import ConfigManager
from app.constants import MAX_CONSOLE_LINES_MIN
from tests_gui.support import LoopbackPort, wait_until
from ui.device_tab import DeviceTab
from ui.widgets.connect_page import ConnectPage

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import benchmark  # noqa: E402

_SMALL_LINES = 2_000
_SMALL_PIECE = 200
_SHORT_SECONDS = 0.5
_SMALL_RATE = 100
# How far above the timetable's rate the measured rate may come, from the
# moments the pieces were fed.
_TIMETABLE_SLACK = 1.05


@pytest.fixture
def small(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(benchmark, "CAPACITY_LINES", _SMALL_LINES)
    monkeypatch.setattr(benchmark, "CAPACITY_LINES_PER_FEED", _SMALL_PIECE)
    monkeypatch.setattr(benchmark, "PTY_LINES", _SMALL_LINES)
    monkeypatch.setattr(benchmark, "PTY_LINES_PER_WRITE", _SMALL_PIECE)
    monkeypatch.setattr(benchmark, "SETTLE_SECONDS", _SHORT_SECONDS)
    monkeypatch.setattr(benchmark, "MEASURE_SECONDS", _SHORT_SECONDS)


@pytest.fixture
def home(tmp_path: Path):
    environment = benchmark.isolated_environment(tmp_path / "home")
    with benchmark.environment_of(environment):
        yield tmp_path


def test_every_wiretrace_folder_is_in_the_home_given(tmp_path: Path) -> None:
    from app.constants import (
        get_cache_dir,
        get_config_dir,
        get_default_log_dir,
        get_diagnostic_log_dir,
    )

    home = tmp_path / "home"
    with benchmark.environment_of(benchmark.isolated_environment(home)):
        folders = [get_config_dir(), get_cache_dir(), get_default_log_dir(), get_diagnostic_log_dir()]

    for folder in folders:
        assert Path(folder).is_relative_to(home), folder


def test_preferences_are_written_with_update_checks_off(home: Path) -> None:
    from app.config import ConfigManager

    benchmark.prepare_preferences(dict(os.environ))
    config = ConfigManager()

    assert not config.first_run
    assert not config.check_updates_on_startup
    assert not config.usage_statistics


def test_capacity_logs_every_line(qapp: QApplication, home: Path, small: None) -> None:
    lines_per_second, bytes_per_second = benchmark.measure_capacity(home)

    assert lines_per_second > 0 and bytes_per_second > lines_per_second


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="this system has no pseudo-terminals")
def test_a_pseudo_terminal_logs_every_line(qapp: QApplication, home: Path, small: None) -> None:
    lines_per_second, bytes_per_second = benchmark.measure_pseudo_terminal(home)

    assert lines_per_second > 0 and bytes_per_second > lines_per_second


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="this system has no pseudo-terminals")
def test_a_port_pair_sends_on_one_and_logs_every_line_from_the_other(
    qapp: QApplication, home: Path, small: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import serial

    # A pseudo-terminal stands for the pair: its device end is the port
    # the lines are sent on, its terminal end the port WireTrace reads.
    joined = benchmark._PseudoTerminal()
    opened: list[tuple[str, int]] = []

    class SendingPort:
        def __init__(self, port: str, baud: int, write_timeout: float) -> None:
            opened.append((port, baud))

        def write(self, data: bytes) -> None:
            joined.write(data)

        def close(self) -> None:
            joined.close()

    monkeypatch.setattr(serial, "Serial", SendingPort)

    lines_per_second, _ = benchmark.measure_port_pair(home, "SENDING", joined.port)

    assert lines_per_second > 0
    assert [port for port, _ in opened] == ["SENDING"]


def test_idle_and_load_are_measured_from_outside(home: Path, small: None) -> None:
    results = benchmark.Results()

    benchmark.measure_idle_and_load(home / "load", _SMALL_RATE, results, idle=True)

    names = set(results.measurements)
    assert {"idle_processor", "idle_memory"} <= names
    assert {f"load_{_SMALL_RATE}_processor", f"load_{_SMALL_RATE}_memory", f"load_{_SMALL_RATE}_sent"} <= names
    assert results.measurements["idle_memory"].values[0] > 0
    # The lines sent a second are those of the timetable, not more: the
    # first piece is not counted over the time after it.
    sent = results.measurements[f"load_{_SMALL_RATE}_sent"].values[0]
    assert 0 < sent <= benchmark.LOAD_TABS * _SMALL_RATE * _TIMETABLE_SLACK


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in launcher is a script run by its first line")
def test_the_process_measured_is_wiretraces_own_behind_a_launcher(
    home: Path, small: None, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A virtual environment's python.exe on Windows runs the real
    # interpreter as a process of its own; this launcher does the same.
    launcher = home / "launcher"
    launcher.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        f"sys.exit(subprocess.call([{sys.executable!r}, *sys.argv[1:]]))\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    monkeypatch.setattr(benchmark.sys, "executable", str(launcher))

    child = benchmark.Child("console", home / "console")
    try:
        assert child.process.pid != child._process.pid
        assert child.process.parent().pid == child._process.pid
        assert "--child" in child.process.cmdline()
        child.expect("full")
        child.tell("stop")
        child.expect("done")
    except BaseException:
        for process in psutil.Process(child._process.pid).children(recursive=True):
            process.kill()
        child.end_now()
        raise
    child.finish()


def test_a_process_named_outside_what_was_started_is_refused() -> None:
    started = psutil.Process(os.getpid())

    assert benchmark.process_named(started, str(os.getpid())) is started
    with pytest.raises(benchmark.BenchmarkFailure, match="not the one started"):
        benchmark.process_named(started, str(os.getppid()))
    with pytest.raises(benchmark.BenchmarkFailure, match="no process number"):
        benchmark.process_named(started, "connected")


def test_the_console_full_is_measured(home: Path, small: None) -> None:
    results = benchmark.Results()

    benchmark.measure_console_full(home / "console", results)

    assert results.measurements["console_full_memory"].values[0] > 0


def test_the_console_is_filled_even_when_it_skips_lines(
    qapp: QApplication, config: ConfigManager, theme_manager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A console given no time for lines draws one step each refresh and
    # skips the rest, as it does on a machine too busy to keep up.
    monkeypatch.setattr(device_tab_module, "DISPLAY_TICK_BUDGET_FRACTION", 0.0)
    config.max_console_lines = MAX_CONSOLE_LINES_MIN
    port = LoopbackPort()
    tab = DeviceTab(theme_manager, config, transport_factory=port.factory)
    tab.show()
    tab.findChild(ConnectPage).connect_requested.emit("PORT0", 115200)
    wait_until(qapp, lambda: tab.is_connected)
    try:
        sent = benchmark.fill_console(qapp, tab, port.feed, config.max_console_lines)

        assert tab.console.total_count == config.max_console_lines
        assert sent > config.max_console_lines
        assert any("lines were not shown" in line for line in tab.console.display_lines())
        assert tab.lines_waiting_for_console() == 0
        assert tab.session_figures().lines == sent
    finally:
        tab.ordered_shutdown()


def test_a_start_is_timed_from_wiretraces_own_log(home: Path) -> None:
    seconds = benchmark.measure_start(home / "start", 1)

    assert 0 < seconds < benchmark.WAIT_SECONDS


def test_results_of_one_version_are_merged_and_of_two_refused(tmp_path: Path) -> None:
    def report(machine: str, commit: str) -> Path:
        path = tmp_path / f"{machine}.json"
        path.write_text(json.dumps({
            "format": benchmark.RESULTS_FORMAT, "wiretrace": "2.2.0", "commit": commit,
            "method": benchmark.method(), "machine": {"name": machine},
        }))
        return path

    merged = benchmark.merge([report("a", "1"), report("b", "1")])

    assert [entry["machine"]["name"] for entry in merged["machines"]] == ["a", "b"]
    with pytest.raises(SystemExit):
        benchmark.merge([report("c", "1"), report("d", "2")])


@pytest.mark.parametrize(("text", "processors"), [
    ("100000 100000", 1.0),
    ("50000 100000", 0.5),
    ("200000 100000", 2.0),
    ("max 100000", None),
    (None, None),
    ("", None),
    ("0 100000", None),
    ("100000 0", None),
])
def test_a_containers_processor_limit_is_read_as_processors(text: str | None, processors: float | None) -> None:
    assert benchmark.container_processor_limit(text) == processors


@pytest.mark.parametrize(("text", "limit"), [
    ("5368709120", 5_368_709_120),
    ("max", None),
    (None, None),
    ("0", None),
    ("lots", None),
])
def test_a_containers_memory_limit_is_read_as_bytes(text: str | None, limit: int | None) -> None:
    assert benchmark.container_memory_limit(text) == limit


def test_a_container_is_described_by_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    files = {"/sys/fs/cgroup/cpu.max": "100000 100000", "/sys/fs/cgroup/memory.max": "5368709120"}
    original = benchmark._read
    monkeypatch.setattr(benchmark, "_read", lambda path: files.get(path, original(path)))

    machine = benchmark.describe_machine("container")

    assert machine["container_processor_limit"] == 1.0
    assert machine["container_memory_limit"] == 5_368_709_120


def test_a_measurement_that_could_not_be_made_in_one_run_is_given_in_none() -> None:
    results = benchmark.Results()
    results.add("pty_lines", "Through a pseudo-terminal: lines a second", "lines/s", 1000.0)
    results.add("pty_bytes", "Through a pseudo-terminal: bytes a second", "bytes/s", 9000.0)
    results.add("capacity_lines", "Capacity: lines a second", "lines/s", 5000.0)

    results.cannot_measure("pseudo_terminal", "no pseudo-terminal", benchmark.PTY_KEYS)

    assert set(results.summaries()) == {"capacity_lines"}
    assert results.not_measured == {"pseudo_terminal": "no pseudo-terminal"}


def test_the_method_names_what_every_run_does() -> None:
    method = benchmark.method()

    assert method == {
        "capacity_lines": benchmark.CAPACITY_LINES,
        "lines_through": benchmark.PTY_LINES,
        "load_tabs": benchmark.LOAD_TABS,
        "load_rates": list(benchmark.LOAD_RATES),
        "measure_seconds": benchmark.MEASURE_SECONDS,
        "display": {"width": 2560, "height": 1440},
    }


def test_results_of_another_method_are_not_merged(tmp_path: Path) -> None:
    def report(machine: str, lines: int) -> Path:
        path = tmp_path / f"{machine}.json"
        path.write_text(json.dumps({
            "format": benchmark.RESULTS_FORMAT, "wiretrace": "2.2.0", "commit": "1",
            "method": {**benchmark.method(), "capacity_lines": lines}, "machine": {"name": machine},
        }))
        return path

    with pytest.raises(SystemExit):
        benchmark.merge([report("a", 1), report("b", 2)])
