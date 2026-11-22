# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure WireTrace on the computer this runs on.

What is measured, each several times (``--runs``), with the median, the
lowest and the highest kept:

  capacity        Lines fed straight into a tab's pipeline, as fast as it
                  takes them, through framing, levels and the console's
                  buffer to the log file. The burst is held in memory
                  until written, so the writer's queue does not limit it;
                  the console and the log are at their default settings.
  pseudo-terminal Numbered lines written by another thread to a
                  pseudo-terminal, which WireTrace opens as a serial port
                  through its own serial driver, to the log file. Only
                  where the system has pseudo-terminals (Linux, macOS).
  port pair       The same through two serial ports joined to each other
                  (``--pair``, a com0com pair on Windows): the lines are
                  sent on one with pyserial, WireTrace reads the other.
  idle            WireTrace's processor and memory use with two tabs
                  connected and logging, every feature on and the plot
                  of each open, while nothing arrives.
  load            The same while each tab receives a steady number of
                  lines a second (``LOAD_RATES``), as a port delivers
                  them. Every line must reach its log.
  console full    Memory with one tab whose console holds as many lines
                  as it keeps by default.
  start           From starting the program to its window built and
                  shown, read from its own diagnostic log, with
                  preferences already in place (a returning user). The
                  first start is not counted: it compiles what Python
                  caches.

Every run checks that each line arrived, in order, once. A run that loses
or reorders a line fails the benchmark (exit status 1).

Processor use is the time the process spent on any processor, as a share
of one processor's time. Memory is the resident set (the working set on
Windows). Both are read from outside the measured process, with psutil.
The measured process names its own process number, and is measured only
if it is the process started or one it started: the python.exe of a
virtual environment on Windows is a launcher that runs the real
interpreter as a process of its own, and measuring the launcher would
give its own few megabytes and no processor time.
The lines fed in the idle, load and console measurements are made in the
measured process, ahead of time, and fed by a timer; that work is
included.

Nothing here reads or writes the user's own preferences, logs or caches:
every WireTrace process started here runs in a temporary home folder of
its own, with update checks off and usage statistics off, so nothing is
sent anywhere.

Usage:
    python tools/benchmark.py [--runs N] [--machine NAME] [--pair SENDING READING] [--output FILE]
    python tools/benchmark.py --merge FILE [FILE ...] --output FILE

``--machine`` names the computer in the results (for GitHub's machines,
the workflow's runner label). ``--merge`` puts the results of several
machines, for the same version and commit, into one file.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import platform
import queue
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QObject, Qt, Signal  # noqa: E402

from tests_gui.offscreen import display_size, offscreen_platform  # noqa: E402

#: The version of the results' format.
RESULTS_FORMAT = 1
DEFAULT_RUNS = 5

#: Capacity: lines in one burst, and lines per piece fed.
CAPACITY_LINES = 200_000
CAPACITY_LINES_PER_FEED = 2_000
#: Pseudo-terminal: lines written, and lines per write.
PTY_LINES = 100_000
PTY_LINES_PER_WRITE = 500
#: How often the console's buffer is read to see whether every line has
#: arrived through the pseudo-terminal.
PTY_POLL_MS = 5

#: Idle and load: tabs, the lines a second each tab receives, and how long
#: each is measured after a settling time.
LOAD_TABS = 2
LOAD_RATES = (1_000, 10_000)
LOAD_FEED_INTERVAL_MS = 100
SETTLE_SECONDS = 2.0
MEASURE_SECONDS = 10.0
#: How often memory is read while measuring.
SAMPLE_SECONDS = 0.25

#: The longest any single wait here may last.
WAIT_SECONDS = 120.0
#: How long a WireTrace process is given to close when asked.
CLOSE_SECONDS = 10.0
#: The message WireTrace logs once its window is built (app/application.py).
WINDOW_BUILT = "initialized successfully"

#: A line of the kind ESP-IDF firmware prints, numbered so that order can
#: be checked. Its values vary, so the plot and the levels do real work.
LINE_FORMAT = "I ({number}) sensor: temp={temp:.1f} rpm={rpm} status=ok"
#: Serial speeds the capacity is shown beside, in baud, with the bits each
#: byte takes on the wire at 8N1 (a start bit, eight data bits, a stop bit).
SERIAL_SPEEDS = (115_200, 921_600)
BITS_PER_BYTE_8N1 = 10


def line(number: int) -> str:
    return LINE_FORMAT.format(number=number, temp=20 + (number % 100) / 10, rpm=number % 3000)


def lines(count: int, first: int = 0) -> list[str]:
    return [line(number) for number in range(first, first + count)]


def pieces(texts: list[str], per_piece: int) -> list[bytes]:
    return [
        "".join(f"{text}\n" for text in texts[start:start + per_piece]).encode()
        for start in range(0, len(texts), per_piece)
    ]


# ── Results ──────────────────────────────────────────────────────────────


@dataclass
class Measurement:
    """One quantity, measured once per run."""

    title: str
    unit: str
    values: list[float] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "title": self.title,
            "unit": self.unit,
            "values": self.values,
            "median": statistics.median(self.values),
            "lowest": min(self.values),
            "highest": max(self.values),
        }


class BenchmarkFailure(Exception):
    """A run lost or reordered a line, or could not be measured."""


class Results:
    def __init__(self) -> None:
        self.measurements: dict[str, Measurement] = {}
        self.not_measured: dict[str, str] = {}
        self._unmade: set[str] = set()

    def add(self, key: str, title: str, unit: str, value: float) -> None:
        self.measurements.setdefault(key, Measurement(title, unit)).values.append(value)

    def cannot_measure(self, name: str, reason: str, keys: tuple[str, ...]) -> None:
        """Record that ``name`` could not be measured, in this run or any
        other: its measurements ``keys`` are then given in no run, since a
        figure made from some of the runs is not the figure of all."""
        self.not_measured.setdefault(name, reason)
        self._unmade.update(keys)

    def summaries(self) -> dict[str, dict[str, object]]:
        return {
            key: value.summary() for key, value in self.measurements.items() if key not in self._unmade
        }


# ── Isolation: a home folder of its own for every WireTrace process ───────


def isolated_environment(home: Path) -> dict[str, str]:
    """The environment of a process whose every WireTrace folder is under
    ``home``: preferences, caches, logs and the diagnostic log."""
    environment = dict(os.environ)
    roaming = home / "AppData" / "Roaming"
    local = home / "AppData" / "Local"
    for folder in (roaming, local):
        folder.mkdir(parents=True, exist_ok=True)
    environment.update({
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(roaming),
        "LOCALAPPDATA": str(local),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "QT_QPA_PLATFORM": offscreen_platform(ROOT),
    })
    # The display scale of the machine running this, not of a test.
    environment.pop("QT_SCALE_FACTOR", None)
    return environment


@contextlib.contextmanager
def environment_of(environment: dict[str, str]) -> Iterator[None]:
    """Run with ``environment`` as this process's, then put it back."""
    saved = dict(os.environ)
    os.environ.clear()
    os.environ.update(environment)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved)


def prepare_preferences(environment: dict[str, str]) -> None:
    """Write preferences in the isolated home: update checks off, so that
    nothing is sent. Usage statistics are off unless switched on."""
    with environment_of(environment):
        from app.config import ConfigManager

        config = ConfigManager()
        config.check_updates_on_startup = False
        config.usage_statistics = False
        config.save()


def diagnostic_log_folder(environment: dict[str, str]) -> Path:
    with environment_of(environment):
        from app.constants import get_diagnostic_log_dir

        return Path(get_diagnostic_log_dir())


# ── The machine ──────────────────────────────────────────────────────────


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return None


def processor_name() -> str:
    """The processor's name as the system gives it."""
    if sys.platform.startswith("linux"):
        for entry in (_read("/proc/cpuinfo") or "").splitlines():
            if entry.startswith("model name"):
                return entry.split(":", 1)[1].strip()
    elif sys.platform == "win32":
        import winreg

        key_path = r"HARDWARE\DESCRIPTION\System\CentralProcessor\0"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    elif sys.platform == "darwin":
        answer = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, check=False,
        )
        if answer.returncode == 0:
            return answer.stdout.strip()
    return platform.processor()


def container_processor_limit(text: str | None) -> float | None:
    """The processors a container may use, from cgroup v2's ``cpu.max``
    ("quota period" in microseconds, or "max" for no limit)."""
    if text is None:
        return None
    quota, _, period = text.partition(" ")
    if quota == "max":
        return None
    try:
        processors = int(quota) / int(period)
    except (ValueError, ZeroDivisionError):
        return None
    return processors if processors > 0 else None


def container_memory_limit(text: str | None) -> int | None:
    """The bytes a container may hold, from cgroup v2's ``memory.max``
    (a number of bytes, or "max" for no limit)."""
    if text is None or text == "max":
        return None
    try:
        limit = int(text)
    except ValueError:
        return None
    return limit if limit > 0 else None


def describe_machine(name: str) -> dict[str, object]:
    """The facts of the machine, as the system reports them. In a
    container, the processors seen may be the host's: the processors this
    process may use, and any limit the container sets, are given too."""
    import psutil
    import PySide6
    from PySide6.QtCore import qVersion

    usable = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None
    return {
        "name": name,
        "system": platform.platform(),
        "processor": processor_name(),
        "processors_seen": psutil.cpu_count(logical=True),
        "processors_usable": usable,
        "container_processor_limit": container_processor_limit(_read("/sys/fs/cgroup/cpu.max")),
        "memory_bytes": psutil.virtual_memory().total,
        "container_memory_limit": container_memory_limit(_read("/sys/fs/cgroup/memory.max")),
        # GitHub's names for the image of its machines.
        "runner_image": os.environ.get("ImageOS"),  # noqa: SIM112
        "runner_image_version": os.environ.get("ImageVersion"),  # noqa: SIM112
        "python": platform.python_version(),
        "pyside6": PySide6.__version__,
        "qt": qVersion(),
    }


def commit() -> str | None:
    if os.environ.get("GITHUB_SHA"):
        return os.environ["GITHUB_SHA"]
    answer = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return answer.stdout.strip() if answer.returncode == 0 else None


# ── In this process: capacity and the pseudo-terminal ────────────────────


def _application():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([sys.argv[0]])


def _pipeline(spool: Path, max_pending: int, transport_factory=None):
    from app.constants import LOG_FLUSH_INTERVAL_MS, MAX_CONSOLE_LINES
    from core.device_pipeline import DevicePipeline

    options = {} if transport_factory is None else {"transport_factory": transport_factory}
    return DevicePipeline(
        spool_root=str(spool),
        display_capacity=MAX_CONSOLE_LINES,
        max_pending_lines=max_pending,
        flush_interval_ms=LOG_FLUSH_INTERVAL_MS,
        **options,
    )


def _check_log(path: Path, expected: list[str]) -> None:
    from tests_gui.support import line_text, log_body

    logged = [line_text(entry) for entry in log_body(str(path))]
    if logged != expected:
        missing = len(expected) - len(logged)
        raise BenchmarkFailure(
            f"the log does not hold every line in order ({len(logged)} of {len(expected)},"
            f" {missing} missing)" if missing else "the log's lines are not the lines sent, in order",
        )


def _log_request(path: Path, port: str):
    from app.constants import DEFAULT_BAUD_RATE
    from core.log_writer import LogRequest

    return LogRequest(str(path), None, "benchmark", port, DEFAULT_BAUD_RATE, "", 0)


def measure_capacity(work: Path) -> tuple[float, float]:
    """Lines and bytes a second from the first piece fed to the log
    closed, holding every line."""
    from app.constants import DEFAULT_BAUD_RATE
    from tests_gui.support import LoopbackPort, wait_until

    application = _application()
    expected = lines(CAPACITY_LINES)
    feeds = pieces(expected, CAPACITY_LINES_PER_FEED)
    size = sum(len(feed) for feed in feeds)
    port = LoopbackPort()
    pipeline = _pipeline(work / "spool", CAPACITY_LINES, port.factory)
    connected: list[object] = []
    closed: list[tuple] = []
    pipeline.connected.connect(lambda *args: connected.append(args))
    pipeline.disconnected.connect(lambda *args: closed.append(args))
    log = work / "capacity.txt"
    try:
        pipeline.open("BENCH0", DEFAULT_BAUD_RATE)
        wait_until(application, lambda: bool(connected), timeout=WAIT_SECONDS)
        pipeline.start_log(_log_request(log, "BENCH0"))
        started = time.perf_counter()
        for feed in feeds:
            port.feed(feed)
        pipeline.close()
        wait_until(application, lambda: bool(closed), timeout=WAIT_SECONDS)
        elapsed = time.perf_counter() - started
    finally:
        pipeline.shutdown()
    summary = closed[0][2]
    if summary.lines_lost:
        raise BenchmarkFailure(f"{summary.lines_lost} lines were not written")
    _check_log(log, expected)
    return CAPACITY_LINES / elapsed, size / elapsed


class _Sender:
    """The other end of a port WireTrace reads: writes bytes to it."""

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class _PseudoTerminal(_Sender):
    """A pseudo-terminal: WireTrace opens its terminal end; lines are
    written to its device end."""

    def __init__(self) -> None:
        import tty

        self._device, self._terminal = os.openpty()
        tty.setraw(self._terminal)
        self.port = os.ttyname(self._terminal)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            view = view[os.write(self._device, view):]

    def close(self) -> None:
        os.close(self._device)
        os.close(self._terminal)


class _PortPair(_Sender):
    """Two serial ports joined to each other (a com0com pair on Windows):
    lines are written to one with pyserial, WireTrace reads the other."""

    def __init__(self, sending: str, reading: str) -> None:
        import serial

        from app.constants import DEFAULT_BAUD_RATE

        self._port = serial.Serial(sending, DEFAULT_BAUD_RATE, write_timeout=WAIT_SECONDS)
        self.port = reading

    def write(self, data: bytes) -> None:
        self._port.write(data)

    def close(self) -> None:
        self._port.close()


def measure_through(work: Path, sender: _Sender, line_count: int) -> tuple[float, float]:
    """Lines and bytes a second written to ``sender`` by another thread and
    read by WireTrace's own serial driver at ``sender.port``, from the
    first write to the log closed with every line."""
    from PySide6.QtCore import QElapsedTimer

    from app.constants import DEFAULT_BAUD_RATE
    from tests_gui.support import wait_until

    application = _application()
    expected = lines(line_count)
    writes = pieces(expected, PTY_LINES_PER_WRITE)
    size = sum(len(piece) for piece in writes)
    pipeline = _pipeline(work / "spool", line_count)
    connected: list[object] = []
    refused: list[str] = []
    closed: list[tuple] = []
    pipeline.connected.connect(lambda *args: connected.append(args))
    pipeline.connect_failed.connect(lambda port, reason: refused.append(reason))
    pipeline.disconnected.connect(lambda *args: closed.append(args))
    log = work / "through-a-port.txt"
    failure: list[BaseException] = []

    def write_all() -> None:
        try:
            for piece in writes:
                sender.write(piece)
        except OSError as error:
            # pyserial's errors are OSErrors too.
            failure.append(error)

    writer = threading.Thread(target=write_all, name="sender")
    try:
        pipeline.open(sender.port, DEFAULT_BAUD_RATE)
        wait_until(application, lambda: bool(connected or refused), timeout=WAIT_SECONDS)
        if refused:
            raise OSError(f"WireTrace could not open {sender.port}: {refused[0]}")
        pipeline.start_log(_log_request(log, sender.port))
        pipeline.take_display(sys.maxsize)
        arrived = [0]

        def count() -> bool:
            records, skipped = pipeline.take_display(sys.maxsize)
            arrived[0] += len(records) + skipped
            return arrived[0] >= line_count

        clock = QElapsedTimer()
        clock.start()
        started = time.perf_counter()
        writer.start()
        while not count():
            application.processEvents()
            if failure:
                break
            if clock.elapsed() > WAIT_SECONDS * 1000:
                raise BenchmarkFailure(f"only {arrived[0]} of {line_count} lines arrived")
            time.sleep(PTY_POLL_MS / 1000)
        pipeline.close()
        wait_until(application, lambda: bool(closed), timeout=WAIT_SECONDS)
        elapsed = time.perf_counter() - started
    finally:
        if writer.is_alive():
            writer.join(WAIT_SECONDS)
        pipeline.shutdown()
        sender.close()
    if failure:
        raise BenchmarkFailure(f"writing to the port failed: {failure[0]}")
    summary = closed[0][2]
    if summary.lines_lost:
        raise BenchmarkFailure(f"{summary.lines_lost} lines were not written")
    _check_log(log, expected)
    return line_count / elapsed, size / elapsed


def measure_pseudo_terminal(work: Path) -> tuple[float, float]:
    return measure_through(work, _PseudoTerminal(), PTY_LINES)


def measure_port_pair(work: Path, sending: str, reading: str) -> tuple[float, float]:
    return measure_through(work, _PortPair(sending, reading), PTY_LINES)


# ── Other processes: WireTrace measured from outside ─────────────────────


class Child:
    """A WireTrace process run by this file (``--child``), in a home of its
    own, told what to do on its standard input and answering on its
    standard output, one line at a time."""

    def __init__(self, mode: str, work: Path, *arguments: str) -> None:
        import psutil

        home = work / f"home-{mode}"
        home.mkdir(parents=True)
        environment = isolated_environment(home)
        prepare_preferences(environment)
        # What the process prints of its own errors is kept, to say why it
        # failed.
        self._errors_path = home / "errors.txt"
        with self._errors_path.open("w", encoding="utf-8") as errors:
            self._process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--child", mode, *arguments],
                cwd=ROOT, env=environment, text=True,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
            )
        self._answers: queue.SimpleQueue[str | None] = queue.SimpleQueue()
        self._listener = threading.Thread(target=self._listen, name=f"{mode} answers", daemon=True)
        self._listener.start()
        try:
            self.process = process_named(psutil.Process(self._process.pid), self.expect("process"))
        except BaseException:
            self.end_now()
            raise

    def _listen(self) -> None:
        assert self._process.stdout is not None
        for answer in self._process.stdout:
            self._answers.put(answer.strip())
        self._answers.put(None)

    def expect(self, word: str) -> str:
        """Wait for an answer that starts with ``word``; returns the rest."""
        deadline = time.monotonic() + WAIT_SECONDS
        while True:
            try:
                answer = self._answers.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                raise BenchmarkFailure(f"no '{word}' from the measured process{self._errors()}") from None
            if answer is None:
                raise BenchmarkFailure(f"the measured process ended before '{word}'{self._errors()}")
            if answer.split(" ", 1)[0] == word:
                return answer[len(word):].strip()
            if answer.startswith("failed"):
                raise BenchmarkFailure(answer[len("failed"):].strip())

    def _errors(self) -> str:
        """The last error the process printed, if any, to add to a message."""
        printed = self._errors_path.read_text(encoding="utf-8", errors="replace").strip()
        return f": {printed.splitlines()[-1]}" if printed else ""

    def tell(self, word: str) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(f"{word}\n")
        self._process.stdin.flush()

    def finish(self) -> None:
        """Wait for the process to end, and close what was opened to it."""
        assert self._process.stdin is not None and self._process.stdout is not None
        self._process.stdin.close()
        try:
            self._process.wait(WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
            raise BenchmarkFailure("the measured process did not end") from None
        finally:
            self._listener.join(WAIT_SECONDS)
            self._process.stdout.close()

    def end_now(self) -> None:
        """End the process at once, after a failure, and close what was
        opened to it."""
        assert self._process.stdin is not None and self._process.stdout is not None
        self._process.kill()
        self._process.wait()
        self._process.stdin.close()
        self._listener.join(WAIT_SECONDS)
        self._process.stdout.close()


def process_named(started, named: str):
    """The process a child named as its own: the process ``started`` or
    one it started, as a psutil process; anything else is refused."""
    try:
        number = int(named)
    except ValueError:
        raise BenchmarkFailure(f"the measured process named no process number: {named!r}") from None
    if number == started.pid:
        return started
    for process in started.children(recursive=True):
        if process.pid == number:
            return process
    raise BenchmarkFailure(f"process {number} is not the one started, nor one it started")


def use_over(process, seconds: float) -> tuple[float, int]:
    """The share of one processor's time ``process`` used over ``seconds``,
    and the most memory it held meanwhile."""
    before = process.cpu_times()
    started = time.perf_counter()
    peak = process.memory_info().rss
    while (remaining := seconds - (time.perf_counter() - started)) > 0:
        time.sleep(min(SAMPLE_SECONDS, remaining))
        peak = max(peak, process.memory_info().rss)
    after = process.cpu_times()
    elapsed = time.perf_counter() - started
    used = (after.user - before.user) + (after.system - before.system)
    return 100 * used / elapsed, peak


def measure_idle_and_load(work: Path, rate: int, results: Results, *, idle: bool) -> None:
    child = Child("load", work, str(rate))
    try:
        child.expect("connected")
        if idle:
            time.sleep(SETTLE_SECONDS)
            share, memory = use_over(child.process, MEASURE_SECONDS)
            results.add("idle_processor", f"Idle, {LOAD_TABS} tabs: processor", "% of one processor", share)
            results.add("idle_memory", f"Idle, {LOAD_TABS} tabs: memory", "bytes", memory)
        child.tell("feed")
        child.expect("feeding")
        time.sleep(SETTLE_SECONDS)
        share, memory = use_over(child.process, MEASURE_SECONDS)
        child.tell("stop")
        report = json.loads(child.expect("done"))
    except BaseException:
        child.end_now()
        raise
    child.finish()
    label = f"{rate:,} lines a second per tab, {LOAD_TABS} tabs"
    results.add(f"load_{rate}_processor", f"{label}: processor", "% of one processor", share)
    results.add(f"load_{rate}_memory", f"{label}: memory", "bytes", memory)
    results.add(
        f"load_{rate}_sent", f"{label}: lines sent a second, all tabs, every one logged", "lines/s",
        report["lines_per_second"],
    )


def measure_console_full(work: Path, results: Results) -> None:
    child = Child("console", work)
    try:
        child.expect("full")
        time.sleep(SETTLE_SECONDS)
        _, memory = use_over(child.process, MEASURE_SECONDS)
        child.tell("stop")
        child.expect("done")
    except BaseException:
        child.end_now()
        raise
    child.finish()
    results.add("console_full_memory", "One tab, console full: memory", "bytes", memory)


def measure_start(work: Path, number: int) -> float:
    """Seconds from starting WireTrace to its window built, as its own
    diagnostic log gives the moment."""
    home = work / f"home-start-{number}"
    home.mkdir(parents=True)
    environment = isolated_environment(home)
    prepare_preferences(environment)
    logs = diagnostic_log_folder(environment)
    started = time.time()
    process = subprocess.Popen(
        [sys.executable, str(ROOT / "main.py")], cwd=ROOT, env=environment,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + WAIT_SECONDS
        while time.monotonic() < deadline:
            moment = _window_built(logs)
            if moment is not None:
                return moment - started
            if process.poll() is not None:
                raise BenchmarkFailure(f"WireTrace ended (status {process.returncode}) before its window was built")
            time.sleep(SAMPLE_SECONDS / 10)
        raise BenchmarkFailure("WireTrace's window was not built in time")
    finally:
        process.terminate()
        try:
            process.wait(CLOSE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _window_built(logs: Path) -> float | None:
    for log in sorted(logs.glob("*.log")) if logs.is_dir() else ():
        for entry in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if WINDOW_BUILT in entry:
                return datetime.datetime.fromisoformat(entry.split(" ", 1)[0]).timestamp()
    return None


# ── The children: WireTrace as a user runs it ────────────────────────────


def _answer(text: str) -> None:
    print(text, flush=True)


def _child_window():
    """A WireTrace window over in-process ports, set up by WireTrace's own
    start-up steps, with every feature on, as a user's after the tour."""
    from app.application import WireTraceApp
    from app.modules import MODULES, ModuleSettings
    from core.highlight_rules import HighlightRule, HighlightStore
    from tests_gui.support import LoopbackTransport
    from ui.main_window import MainWindow

    # The application object and the preferences and theme, made exactly
    # as at a start; the window is then given in-process ports.
    wiretrace = WireTraceApp([sys.argv[0]])
    wiretrace._create_app()
    wiretrace._load_preferences_and_theme()
    application, config, themes = wiretrace._app, wiretrace._config, wiretrace._theme_manager
    features = ModuleSettings(config)
    for spec in MODULES:
        features.set_enabled(spec, True)
    HighlightStore(config).set_rules((HighlightRule("status=ok", 3), HighlightRule("rpm", 5)))
    config.alert_silence = True
    transports: dict[str, LoopbackTransport] = {}

    def factory(port, baud, line=None, driver=None):
        transport = LoopbackTransport()
        transport.open(port, baud, line)
        transports[port] = transport
        return transport, ""

    window = MainWindow(config, themes, transport_factory=factory)
    window.show()

    return application, window, config, transports


class _Orders(QObject):
    """What the measuring process says on standard input, one word a line,
    read on a thread of its own and handled on the main thread."""

    given = Signal(str)

    def listen(self, handle: Callable[[str], None]) -> None:
        """Hand each order to ``handle``, from now on."""
        self.given.connect(handle, Qt.ConnectionType.QueuedConnection)
        threading.Thread(target=self._read, name="orders", daemon=True).start()

    def _read(self) -> None:
        for order in sys.stdin:
            self.given.emit(order.strip())


def _connect_and_log(application, config, tab, port: str) -> Path:
    """Connect ``tab`` to ``port`` and start a log, as from its toolbar,
    choosing Start in the window that asks for the log's name. Each
    tab's logs go to a folder of their own, which is returned."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from app.constants import DEFAULT_BAUD_RATE
    from tests_gui.support import wait_until
    from ui.widgets.tab_toolbar import TabToolbar

    toolbar = tab.findChild(TabToolbar)
    toolbar.connect_requested.emit(port, DEFAULT_BAUD_RATE)
    wait_until(application, lambda: tab.is_connected, timeout=WAIT_SECONDS)

    def accept() -> None:
        dialog = QApplication.activeModalWidget()
        if dialog is None:
            QTimer.singleShot(0, accept)
        else:
            dialog.accept()

    folder = Path.home() / "Logs" / port
    folder.mkdir(parents=True)
    config.default_log_directory = str(folder)
    QTimer.singleShot(0, accept)
    tab.start_log()
    if not tab.is_logging:
        raise BenchmarkFailure(f"the log did not start on {port}")
    return folder


def _logged_lines(folder: Path) -> int:
    """The lines in the logs of a folder, a log carried on in new files
    included."""
    from tests_gui.support import log_body

    return sum(len(log_body(str(path))) for path in folder.glob("*.txt"))


def child_load(rate: int) -> None:
    from tests_gui.support import wait_until

    application, window, config, transports = _child_window()
    tabs = [window.current_tab()]
    while len(tabs) < LOAD_TABS:
        tabs.append(window.new_tab())
    ports = [f"BENCH{number}" for number in range(LOAD_TABS)]
    folders = []
    for tab, port in zip(tabs, ports, strict=True):
        window.show_tab(tab)
        folders.append(_connect_and_log(application, config, tab, port))
        tab.toggle_plot()
    per_feed = rate * LOAD_FEED_INTERVAL_MS // 1000
    # One second of lines, made ahead of time and fed in turn, from a
    # thread of its own and on a fixed timetable, as a port delivers
    # whatever the window is doing.
    second = pieces(lines(rate), per_feed)
    fed = [0]
    # When the first and the latest piece were fed: the rate is the lines
    # after the first piece over the time between them.
    fed_at = {"first": 0.0, "latest": 0.0}
    stopping = threading.Event()

    def feed() -> None:
        interval = LOAD_FEED_INTERVAL_MS / 1000
        due = time.perf_counter()
        while not stopping.is_set():
            piece = second[(fed[0] // per_feed) % len(second)]
            for port in ports:
                transports[port].feed(piece)
            fed[0] += per_feed
            fed_at["latest"] = time.perf_counter()
            if fed[0] == per_feed:
                fed_at["first"] = fed_at["latest"]
            due += interval
            stopping.wait(max(0.0, due - time.perf_counter()))

    feeder = threading.Thread(target=feed, name="feeder", daemon=True)

    def on_order(order: str) -> None:
        if order == "feed":
            feeder.start()
            _answer("feeding")
        elif order == "stop":
            stopping.set()
            feeder.join()
            # Disconnecting writes every line already received to the log
            # before the log is closed.
            for tab in tabs:
                tab.disconnect_device()
            wait_until(application, lambda: not any(tab.is_connected for tab in tabs), timeout=WAIT_SECONDS)
            for folder, port in zip(folders, ports, strict=True):
                logged = _logged_lines(folder)
                if logged < fed[0]:
                    _answer(f"failed {port}: {fed[0] - logged} of {fed[0]} lines were not logged")
                    application.exit(1)
                    return
            if fed[0] < 2 * per_feed:
                _answer("failed the load was stopped before a second piece was fed")
                application.exit(1)
                return
            sent = LOAD_TABS * (fed[0] - per_feed) / (fed_at["latest"] - fed_at["first"])
            _answer("done " + json.dumps({"lines_per_second": sent}))
            application.exit(0)

    orders = _Orders()
    orders.listen(on_order)
    _answer("connected")
    sys.exit(application.exec())


def fill_console(application, tab, feed: Callable[[bytes], None], limit: int) -> int:
    """Send numbered lines until the console of ``tab`` holds ``limit``
    lines, its line limit; returns how many were sent.

    A piece is sent once the console has taken the one before, shown or
    skipped. A console that cannot draw lines as fast as they come skips
    some to stay close to the device (and says so), so the console is
    filled until it is full rather than with a set number of lines.
    """
    from tests_gui.support import wait_until

    def taken(sent: int) -> bool:
        figures = tab.session_figures()
        return figures is not None and figures.lines >= sent and not tab.lines_waiting_for_console()

    sent = 0 if (figures := tab.session_figures()) is None else figures.lines
    first = sent
    while tab.console.total_count < limit:
        feed(pieces(lines(CAPACITY_LINES_PER_FEED, sent), CAPACITY_LINES_PER_FEED)[0])
        sent += CAPACITY_LINES_PER_FEED
        wait_until(application, lambda sent=sent: taken(sent), timeout=WAIT_SECONDS)
    return sent - first


def child_console() -> None:
    application, window, config, transports = _child_window()
    tab = window.current_tab()
    _connect_and_log(application, config, tab, "BENCH0")
    fill_console(application, tab, transports["BENCH0"].feed, config.max_console_lines)

    def on_order(order: str) -> None:
        if order == "stop":
            _answer("done")
            application.exit(0)

    orders = _Orders()
    orders.listen(on_order)
    _answer("full")
    sys.exit(application.exec())


# ── Running and reporting ────────────────────────────────────────────────


def _capacity(step: Path, results: Results) -> None:
    per_line, per_byte = measure_capacity(step)
    results.add("capacity_lines", "Capacity: lines a second", "lines/s", per_line)
    results.add("capacity_bytes", "Capacity: bytes a second", "bytes/s", per_byte)


PTY_KEYS = ("pty_lines", "pty_bytes")


def _pseudo_terminal(step: Path, results: Results) -> None:
    if not hasattr(os, "openpty"):
        results.cannot_measure("pseudo_terminal", "this system has no pseudo-terminals", PTY_KEYS)
        return
    try:
        per_line, per_byte = measure_pseudo_terminal(step)
    except OSError as error:
        # The system would not give or open a pseudo-terminal: said, not
        # counted as a failure of WireTrace.
        results.cannot_measure("pseudo_terminal", str(error), PTY_KEYS)
        return
    results.add("pty_lines", "Through a pseudo-terminal: lines a second", "lines/s", per_line)
    results.add("pty_bytes", "Through a pseudo-terminal: bytes a second", "bytes/s", per_byte)


def _port_pair(step: Path, results: Results, pair: tuple[str, str]) -> None:
    sending, reading = pair
    try:
        per_line, per_byte = measure_port_pair(step, sending, reading)
    except OSError as error:
        raise BenchmarkFailure(f"the ports {sending} and {reading} could not be used: {error}") from error
    label = f"Through the ports {sending} to {reading}"
    results.add("pair_lines", f"{label}: lines a second", "lines/s", per_line)
    results.add("pair_bytes", f"{label}: bytes a second", "bytes/s", per_byte)


def _one_run(
    number: int, step: Path, results: Results, failures: list[str], pair: tuple[str, str] | None,
) -> None:
    """Every measurement but the start, once."""
    measurements: list[tuple[str, Callable[[], None]]] = [
        ("capacity", lambda: _capacity(step, results)),
        ("pseudo-terminal", lambda: _pseudo_terminal(step, results)),
    ]
    if pair is not None:
        measurements.append(("port pair", lambda: _port_pair(step, results, pair)))
    for index, rate in enumerate(LOAD_RATES):
        measurements.append((
            f"load, {rate:,} lines a second",
            lambda rate=rate, index=index: measure_idle_and_load(
                step / f"load-{rate}", rate, results, idle=index == 0,
            ),
        ))
    measurements.append(("console full", lambda: measure_console_full(step / "console", results)))
    for name, measure in measurements:
        try:
            measure()
        except BenchmarkFailure as failure:
            failures.append(f"run {number}, {name}: {failure}")
        print(f"  {name}", flush=True)


def run(runs: int, machine: str, pair: tuple[str, str] | None = None) -> tuple[dict[str, object], bool]:
    results = Results()
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="wiretrace-benchmark-") as folder:
        work = Path(folder)
        environment = isolated_environment(work / "home")
        with environment_of(environment):
            for number in range(1, runs + 1):
                print(f"Run {number} of {runs}", flush=True)
                step = work / f"run-{number}"
                step.mkdir()
                _one_run(number, step, results, failures, pair)
            # The first start compiles what Python caches; it is not counted.
            starts = work / "starts"
            measure_start(starts, 0)
            for number in range(1, runs + 1):
                try:
                    results.add("start", "Start, to the window built", "s", measure_start(starts, number))
                except BenchmarkFailure as failure:
                    failures.append(f"start {number}: {failure}")
            print("  start", flush=True)
            machine_facts = describe_machine(machine)

    from version import APP_VERSION

    report: dict[str, object] = {
        "format": RESULTS_FORMAT,
        "wiretrace": APP_VERSION,
        "commit": commit(),
        "measured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "runs": runs,
        "line": {"example": line(0), "format": LINE_FORMAT},
        "machine": machine_facts,
        "method": method(),
        "measurements": results.summaries(),
        "not_measured": results.not_measured,
        "failures": failures,
    }
    return report, not failures


def method() -> dict[str, object]:
    """What every run does, as the figures need it said: the lines each
    measurement sends, the tabs and rates of the load, how long use is
    measured, and the display drawn on."""
    width, height = display_size()
    return {
        "capacity_lines": CAPACITY_LINES,
        "lines_through": PTY_LINES,
        "load_tabs": LOAD_TABS,
        "load_rates": list(LOAD_RATES),
        "measure_seconds": MEASURE_SECONDS,
        "display": {"width": width, "height": height},
    }


def merge(paths: list[Path]) -> dict[str, object]:
    """One file of the results of several machines, for one version and
    one commit."""
    reports = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    first = reports[0]
    for report in reports:
        for key in ("format", "wiretrace", "commit", "method"):
            if report[key] != first[key]:
                raise SystemExit(f"{key} differs between the files: {report[key]} and {first[key]}")
    return {
        "format": first["format"],
        "wiretrace": first["wiretrace"],
        "commit": first["commit"],
        "machines": reports,
    }


def _shown(value: float, unit: str) -> str:
    if unit == "bytes":
        return f"{value / 1_000_000:,.1f} MB"
    if unit == "bytes/s":
        return f"{value / 1_000_000:,.2f} MB/s"
    if unit == "s":
        return f"{value:.2f} s"
    if unit.startswith("%"):
        return f"{value:.1f} %"
    return f"{value:,.0f} {unit}"


def print_report(report: dict[str, object]) -> None:
    machine = report["machine"]
    print()
    print(f"WireTrace {report['wiretrace']} on {machine['name']}: {machine['processor']},"
          f" {machine['processors_usable'] or machine['processors_seen']} processors,"
          f" {machine['memory_bytes'] / 1_000_000_000:.1f} GB")
    print(f"{report['runs']} runs; median (lowest to highest)")
    for entry in report["measurements"].values():
        unit = entry["unit"]
        print(f"  {entry['title']}: {_shown(entry['median'], unit)}"
              f" ({_shown(entry['lowest'], unit)} to {_shown(entry['highest'], unit)})")
    capacity = report["measurements"].get("capacity_bytes")
    if capacity:
        for baud in SERIAL_SPEEDS:
            wire = baud / BITS_PER_BYTE_8N1
            print(f"  {baud:,} baud at 8N1 carries {wire:,.0f} bytes a second:"
                  f" the median capacity is {capacity['median'] / wire:,.0f} times that")
    for name, reason in report["not_measured"].items():
        print(f"  Not measured, {name}: {reason}")
    for failure in report["failures"]:
        print(f"  FAILED {failure}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure WireTrace on this computer.")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="times each measurement is made")
    parser.add_argument("--machine", default=platform.node(), help="the computer's name in the results")
    parser.add_argument("--output", type=Path, help="where the results are written, as JSON")
    parser.add_argument(
        "--pair", nargs=2, metavar=("SENDING", "READING"),
        help="two serial ports joined to each other: lines are sent on the first, WireTrace reads the second",
    )
    parser.add_argument("--merge", type=Path, nargs="+", help="results of several machines to put in one file")
    parser.add_argument("--child", nargs="+", help=argparse.SUPPRESS)
    arguments = parser.parse_args()

    if arguments.child:
        mode, *rest = arguments.child
        _answer(f"process {os.getpid()}")
        try:
            if mode == "load":
                child_load(int(rest[0]))
            elif mode == "console":
                child_console()
        except BenchmarkFailure as failure:
            _answer(f"failed {failure}")
            return 1
        return 0
    if arguments.merge:
        if arguments.output is None:
            parser.error("--merge needs --output")
        arguments.output.write_text(json.dumps(merge(arguments.merge), indent=2) + "\n", encoding="utf-8")
        return 0
    if arguments.runs < 1:
        parser.error("--runs must be at least 1")
    pair = tuple(arguments.pair) if arguments.pair else None
    report, passed = run(arguments.runs, arguments.machine, pair)
    print_report(report)
    if arguments.output is not None:
        arguments.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
