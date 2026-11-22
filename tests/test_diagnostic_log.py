# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the diagnostic log: where and how it writes, its limits, that
nothing waits on it, and that failures on the data path leave the data
out (``app/diagnostic_log.py``)."""

from __future__ import annotations

import errno
import logging
import os
import queue
import re
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

from app import diagnostic_log as module
from app.constants import (
    DIAGNOSTIC_LOG_FILE_BYTES,
    DIAGNOSTIC_LOG_FOLDER_LOCK,
    DIAGNOSTIC_LOG_QUEUE_RECORDS,
    DIAGNOSTIC_LOG_TOTAL_BYTES,
    DIAGNOSTIC_SUMMARY_EVENTS,
    get_diagnostic_log_dir,
)
from app.diagnostic_log import DiagnosticLog, described, diagnostic_summary, log_data_failure

STARTED = datetime(2026, 11, 19, 18, 27, 5)
_LINE = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}[+-]\d\d:\d\d (INFO|WARNING|ERROR) \[[^\]]+\] [\w.]+: .*"
)


@pytest.fixture
def log_at(tmp_path: Path):
    """Start a diagnostic log in a folder; it is stopped at the end."""
    started: list[DiagnosticLog] = []
    root = logging.getLogger()
    level = root.level
    root.setLevel(logging.INFO)

    def start(**limits: int) -> DiagnosticLog:
        diagnostics = DiagnosticLog(tmp_path / "logs", started=STARTED, **limits)
        diagnostics.start(["WireTrace 9.9.9, installed as source"])
        started.append(diagnostics)
        return diagnostics

    yield start
    for diagnostics in started:
        diagnostics.stop()
    root.setLevel(level)


def _files(folder: Path) -> list[str]:
    """The logs in ``folder``, beside the lock the runs share."""
    return sorted(path.name for path in folder.iterdir() if path.name != DIAGNOSTIC_LOG_FOLDER_LOCK)


class TestWhereAndHow:
    def test_the_limits_are_those_agreed(self):
        assert DIAGNOSTIC_LOG_FILE_BYTES == 1024 * 1024
        assert DIAGNOSTIC_LOG_TOTAL_BYTES == 5 * 1024 * 1024
        assert DIAGNOSTIC_LOG_QUEUE_RECORDS > 0

    @pytest.mark.parametrize(
        ("platform", "environment", "expected"),
        [
            ("win32", {"LOCALAPPDATA": "/local"}, ("/local", "WireTrace", "Logs")),
            ("linux", {"XDG_STATE_HOME": "/state"}, ("/state", "WireTrace", "logs")),
            ("linux", {}, ("~", ".local", "state", "WireTrace", "logs")),
            ("darwin", {}, ("~", "Library", "Logs", "WireTrace")),
        ],
    )
    def test_the_folder_is_the_systems_place_for_application_logs(
        self, monkeypatch, platform, environment, expected,
    ):
        monkeypatch.setattr(sys, "platform", platform)
        for name in ("LOCALAPPDATA", "XDG_STATE_HOME"):
            monkeypatch.delenv(name, raising=False)
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("HOME", "/home/someone")

        parts = [os.path.expanduser(part) if part == "~" else part for part in expected]
        assert get_diagnostic_log_dir() == os.path.join(*parts)

    def test_a_run_writes_its_own_file_one_event_a_line(self, log_at, tmp_path):
        diagnostics = log_at()
        logging.getLogger("core.transport").info("Opened COM7 @ 115200 baud, 8N1, via QSerialPort")
        logging.getLogger("ui.device_tab").warning("Waiting for the board on COM7 to return")
        diagnostics.stop()

        name = f"wiretrace-20261119-182705-{os.getpid()}.log"
        assert _files(tmp_path / "logs") == [name]
        lines = (tmp_path / "logs" / name).read_text(encoding="utf-8").splitlines()
        assert [line.split(": ", 1)[1] for line in lines] == [
            "WireTrace 9.9.9, installed as source",
            "Opened COM7 @ 115200 baud, 8N1, via QSerialPort",
            "Waiting for the board on COM7 to return",
        ]
        assert all(_LINE.fullmatch(line) for line in lines)
        assert " WARNING [MainThread] ui.device_tab: " in lines[2]

    @pytest.mark.skipif(sys.platform == "win32", reason="permissions are POSIX modes here")
    def test_the_folder_and_its_files_are_private_to_their_owner(self, log_at, tmp_path):
        diagnostics = log_at()
        diagnostics.stop()

        assert stat.S_IMODE((tmp_path / "logs").stat().st_mode) == 0o700
        assert stat.S_IMODE(diagnostics.current_file.stat().st_mode) == 0o600

    def test_an_error_is_written_with_its_traceback(self, log_at):
        diagnostics = log_at()
        try:
            raise OSError("the disk is full")
        except OSError:
            logging.getLogger("core.log_writer").exception("Log write failed")
        diagnostics.stop()

        text = diagnostics.current_file.read_text(encoding="utf-8")
        assert "Log write failed\nTraceback (most recent call last):" in text
        assert "OSError: the disk is full" in text

    def test_stopping_writes_what_is_waiting_and_lets_go(self, log_at):
        diagnostics = log_at()
        for number in range(500):
            logging.getLogger("test").info("event %d", number)
        diagnostics.stop()

        text = diagnostics.current_file.read_text(encoding="utf-8")
        assert "event 499" in text
        logging.getLogger("test").info("after stopping")
        assert "after stopping" not in diagnostics.current_file.read_text(encoding="utf-8")

    def test_a_folder_that_cannot_be_made_stops_nothing(self, tmp_path):
        blocker = tmp_path / "logs"
        blocker.write_text("a file where the folder would be")
        diagnostics = DiagnosticLog(blocker / "inner", started=STARTED)

        diagnostics.start(["first"])
        logging.getLogger("test").warning("still running")
        diagnostics.stop()

        assert diagnostics.current_file is None


class TestLimits:
    def test_a_full_file_is_continued_in_the_next(self, log_at, tmp_path):
        diagnostics = log_at(file_bytes=600, total_bytes=100_000)
        for number in range(20):
            logging.getLogger("test").info("event %02d %s", number, "x" * 60)
        diagnostics.stop()

        stem = f"wiretrace-20261119-182705-{os.getpid()}"
        names = _files(tmp_path / "logs")
        assert f"{stem}.log" in names and f"{stem}-2.log" in names
        for name in names:
            assert (tmp_path / "logs" / name).stat().st_size <= 600
        # In the order the run wrote them: its first file, then each part.
        text = "".join(
            (tmp_path / "logs" / name).read_text(encoding="utf-8")
            for name in sorted(names, key=lambda name: int(module._RUN_FILE.fullmatch(name)["part"] or 1))
        )
        assert [int(n) for n in re.findall(r"event (\d\d)", text)] == list(range(20))

    def test_files_written_at_the_same_moment_go_in_the_order_they_were_written(self, tmp_path):
        # The system's clock may move only every few milliseconds, so a
        # run's files can carry the same time.
        first = tmp_path / "wiretrace-20261101-080000-1.log"
        second = tmp_path / "wiretrace-20261101-080000-1-2.log"
        moment = time.time() - 3600
        for path in (first, second):
            path.write_bytes(b"x" * 400)
            os.utime(path, (moment, moment))

        run = module._RunFiles(tmp_path, f"wiretrace-20261121-000000-{os.getpid()}", lambda: 0, 400, 1000)
        run.handle(logging.makeLogRecord({"msg": "event", "levelno": 20}))
        run.close()

        assert not first.exists()
        assert second.exists()

    def test_the_oldest_files_go_while_the_folder_holds_more_than_its_total(self, log_at, tmp_path):
        folder = tmp_path / "logs"
        folder.mkdir()
        old = folder / "wiretrace-20261101-080000-1.log"
        old.write_bytes(b"x" * 900)
        os.utime(old, (time.time() - 3600, time.time() - 3600))
        unrelated = folder / "notes.txt"
        unrelated.write_bytes(b"y" * 5000)

        diagnostics = log_at(file_bytes=400, total_bytes=1000)
        for number in range(12):
            logging.getLogger("test").info("event %02d %s", number, "x" * 40)
        diagnostics.stop()

        assert not old.exists()
        assert unrelated.exists()
        logs = [path for path in folder.glob("wiretrace-*.log")]
        assert diagnostics.current_file in logs
        assert sum(path.stat().st_size for path in logs) <= 1000

    def test_a_file_the_system_will_not_remove_is_passed_over(self, log_at, tmp_path, monkeypatch):
        folder = tmp_path / "logs"
        folder.mkdir()
        held = folder / "wiretrace-20261101-080000-1.log"
        held.write_bytes(b"x" * 500)
        os.utime(held, (time.time() - 3600, time.time() - 3600))
        unlink = Path.unlink

        def refuse(path: Path, *args, **kwargs) -> None:
            if path == held:
                raise PermissionError("in use by another process")
            unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", refuse)
        diagnostics = log_at(file_bytes=400, total_bytes=1000)
        logging.getLogger("test").info("written all the same")
        diagnostics.stop()

        assert held.exists()
        assert "written all the same" in diagnostics.current_file.read_text(encoding="utf-8")

    def test_two_runs_at_once_keep_the_folder_to_its_total(self, tmp_path):
        # Two windows write at the same time, as their writing threads do;
        # each is a process still running (this one).
        runs = [
            module._RunFiles(tmp_path, f"wiretrace-2026112{number}-000000-{os.getpid()}", lambda: 0, 1000, 5000)
            for number in range(2)
        ]
        peak = 0
        for number in range(2000):
            runs[number % 2].handle(logging.makeLogRecord({"msg": "m" * 40 + str(number), "levelno": 20}))
            peak = max(peak, sum(path.stat().st_size for path in tmp_path.glob('wiretrace-*.log')))
        for run in runs:
            run.close()

        assert 0 < peak <= 5000

    def test_the_newest_file_of_a_run_that_has_ended_counts_as_what_it_holds(self, tmp_path):
        ended = tmp_path / "wiretrace-20261101-080000-1.log"
        ended.write_bytes(b"x" * 100)

        run = module._RunFiles(tmp_path, f"wiretrace-20261121-000000-{os.getpid()}", lambda: 0, 400, 1000)
        run.handle(logging.makeLogRecord({"msg": "event", "levelno": 20}))
        run.close()

        assert ended.exists()
        assert run._room == 400

    def test_a_folder_with_no_room_writes_nothing_and_says_so_when_it_has(self, tmp_path):
        # Two other runs writing, each holding a file that may grow to a
        # full one: no room is left for a third.
        others = [
            module._RunFiles(tmp_path, f"wiretrace-2026110{number}-080000-{number}", lambda: 0, 400, 800)
            for number in (1, 2)
        ]
        for other in others:
            other.handle(logging.makeLogRecord({"msg": "other", "levelno": 20}))

        run = module._RunFiles(tmp_path, f"wiretrace-20261121-000000-{os.getpid()}", lambda: 0, 400, 800)
        run.handle(logging.makeLogRecord({"msg": "while full", "name": "test", "levelno": 20}))
        assert run.current is None

        others[0].close()
        run.handle(logging.makeLogRecord({"msg": "with room", "name": "test", "levelno": 20}))
        run.close()
        others[1].close()

        lines = run.current.read_text(encoding="utf-8").splitlines()
        assert lines[0].endswith("WARNING [diagnostic log] 1 messages were not written: the folder had no room for them, or the disk refused them")
        assert lines[1].endswith("test: with room")
        assert sum(path.stat().st_size for path in tmp_path.glob('wiretrace-*.log')) <= 800

    def test_a_full_disk_leaves_one_file_and_says_what_it_refused(self, tmp_path, monkeypatch):
        run = module._RunFiles(tmp_path, f"wiretrace-20261121-000000-{os.getpid()}", lambda: 0, 4000, 20_000)
        write = os.write
        refusing = [True]

        def full_disk(stream: int, data: bytes) -> int:
            if stream == run._stream and refusing[0]:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            return write(stream, data)

        monkeypatch.setattr(module.os, "write", full_disk)
        for number in range(200):
            run.handle(logging.makeLogRecord({"msg": f"event {number}", "levelno": 20}))
        assert _files(tmp_path) == [run.current.name]

        refusing[0] = False
        run.handle(logging.makeLogRecord({"msg": "the disk has room again", "name": "test", "levelno": 20}))
        run.close()

        lines = run.current.read_text(encoding="utf-8").splitlines()
        assert lines[0].endswith(
            "200 messages were not written: the folder had no room for them, or the disk refused them"
        )
        assert lines[1].endswith("test: the disk has room again")

    def test_messages_the_queue_dropped_are_said_once_there_is_room(self, tmp_path):
        others = [
            module._RunFiles(tmp_path, f"wiretrace-2026110{number}-080000-{number}", lambda: 0, 400, 800)
            for number in (1, 2)
        ]
        for other in others:
            other.handle(logging.makeLogRecord({"msg": "other", "levelno": 20}))
        dropped = iter([500])
        run = module._RunFiles(
            tmp_path, f"wiretrace-20261121-000000-{os.getpid()}", lambda: next(dropped, 0), 400, 800,
        )
        run.handle(logging.makeLogRecord({"msg": "while full", "levelno": 20}))

        others[0].close()
        run.handle(logging.makeLogRecord({"msg": "with room", "name": "test", "levelno": 20}))
        run.close()
        others[1].close()

        written = "".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("wiretrace-20261121-*.log"))
        assert "500 messages were not written: they came faster than the disk took them" in written

    def test_runs_beginning_files_at_once_never_count_the_same_room(self, tmp_path):
        runs = [
            module._RunFiles(tmp_path, f"wiretrace-2026112{number}-000000-{number}", lambda: 0, 1000, 3000)
            for number in range(6)
        ]
        start = threading.Barrier(len(runs))

        def write(run: module._RunFiles) -> None:
            start.wait()
            for number in range(40):
                run.handle(logging.makeLogRecord({"msg": "m" * 60 + str(number), "levelno": 20}))

        threads = [threading.Thread(target=write, args=(run,)) for run in runs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        held = sum(path.stat().st_size for path in tmp_path.glob("wiretrace-*.log"))
        reserved = sum(1000 - run._size for run in runs if run._stream is not None)
        for run in runs:
            run.close()

        # What the folder holds, with what the runs still writing may add.
        assert held + reserved <= 3000

    def test_stopping_with_the_queue_full_writes_it_and_lets_go(self, tmp_path, monkeypatch):
        gate = threading.Event()
        writing = threading.Event()
        emit = module._RunFiles.emit

        def stalled(self, record):
            writing.set()
            gate.wait()
            emit(self, record)

        monkeypatch.setattr(module._RunFiles, "emit", stalled)
        diagnostics = DiagnosticLog(tmp_path / "logs", started=STARTED, queue_records=10)
        diagnostics.start()
        logging.getLogger("test").warning("first")
        # The writing thread holds the first and waits on the disk; the
        # queue then fills behind it.
        assert writing.wait(5)
        for number in range(30):
            logging.getLogger("test").warning("event %d", number)
        assert diagnostics._queueing.queue.full()

        threading.Timer(0.3, gate.set).start()
        diagnostics.stop()

        assert "event" in diagnostics.current_file.read_text(encoding="utf-8")

    def test_a_disk_that_does_not_answer_does_not_hold_up_the_exit(self, tmp_path, monkeypatch):
        gate = threading.Event()
        emit = module._RunFiles.emit

        def stalled(self, record):
            gate.wait()
            emit(self, record)

        monkeypatch.setattr(module._RunFiles, "emit", stalled)
        monkeypatch.setattr(module, "DIAGNOSTIC_LOG_STOP_SECONDS", 0.2)
        diagnostics = DiagnosticLog(tmp_path / "logs", started=STARTED, queue_records=10)
        diagnostics.start()
        logging.getLogger("test").warning("event")

        began = time.monotonic()
        diagnostics.stop()
        gate.set()

        assert time.monotonic() - began < 1.0

    def test_a_message_that_finds_the_queue_full_is_counted_and_said(self, tmp_path):
        handler = module._Queueing(queue.Queue(maxsize=1))
        for number in range(4):
            handler.enqueue(logging.makeLogRecord({"msg": f"event {number}"}))

        files = module._RunFiles(tmp_path, "wiretrace-run", handler.take_dropped, 10_000, 100_000)
        files.handle(logging.makeLogRecord({"msg": "the next one", "name": "test", "levelno": 20}))
        files.close()

        lines = files.current.read_text(encoding="utf-8").splitlines()
        assert lines[0].endswith(
            "WARNING [diagnostic log] 3 messages were not written: they came faster than the disk took them"
        )
        assert lines[1].endswith("test: the next one")
        assert handler.take_dropped() == 0

    def test_the_latest_events_and_problems_are_kept_for_the_summary(self, log_at):
        diagnostics = log_at()
        for number in range(DIAGNOSTIC_SUMMARY_EVENTS + 5):
            logging.getLogger("test").info("event %d", number)
        logging.getLogger("test").error("a problem")
        diagnostics.stop()

        events, problems = diagnostics.recent()
        assert len(events) == DIAGNOSTIC_SUMMARY_EVENTS
        assert events[-1].endswith("test: a problem")
        assert [line.split(": ", 1)[1] for line in problems] == ["a problem"]


class TestDataStaysOut:
    def test_a_failure_on_the_data_path_is_logged_by_kind_and_place_only(self, caplog):
        log = logging.getLogger("core.plot_engine")
        received = "temp=x12 secret-token-42"

        with caplog.at_level(logging.ERROR, logger="core.plot_engine"):
            try:
                float(received)
            except ValueError:
                log_data_failure(log, "The plot could not read a line of %d bytes", len(received))

        text = caplog.text
        assert "secret-token-42" not in text
        assert f"The plot could not read a line of {len(received)} bytes: ValueError, at" in text
        assert "float(received)" in text  # the source line, which is code
        assert caplog.records[0].exc_info is None

    def test_an_error_is_named_by_its_kind_never_by_a_file_or_what_it_quotes(self):
        import configparser

        from core.log_writer import LogStartError

        missing = PermissionError(13, "Permission denied", "/home/someone/My secret project.txt")
        try:
            raise LogStartError(str(missing)) from missing
        except LogStartError as raised:
            started = raised
        parsing = configparser.ParsingError("preferences.ini")
        parsing.append(5, "saved = my private command")

        assert described(missing) == "PermissionError: Permission denied"
        assert described(started) == "PermissionError: Permission denied"
        assert described(parsing) == "ParsingError"
        assert described(ValueError("Expecting value: my text")) == "ValueError"
        # WireTrace's own words, with no system error behind them.
        assert described(OSError("this installation cannot update itself")) == (
            "OSError: this installation cannot update itself"
        )
        missing_file = FileNotFoundError(2, "No such file or directory", "/home/someone/Acme pump 7.csv")
        assert described(missing_file) == "FileNotFoundError: No such file or directory"

    def test_source_code_holding_a_percent_sign_is_logged_as_it_is(self, caplog):
        with caplog.at_level(logging.ERROR, logger="test"):
            try:
                raise KeyError("%s %d")
            except KeyError:
                log_data_failure(logging.getLogger("test"), "Failed %s", "here")

        assert "Failed here: KeyError, at" in caplog.text


class TestSummary:
    def test_it_says_what_the_run_is_its_ports_and_its_latest_events(self):
        text = diagnostic_summary(
            ["WireTrace 2.2.0, installed as exe", "System: Windows-11"],
            ["COM15, 115200 baud, connected"],
            Path("/logs"),
            ["e1", "e2"],
            ["p1"],
        )

        assert text == (
            "WireTrace 2.2.0, installed as exe\n"
            "System: Windows-11\n"
            f"Diagnostic log: {Path('/logs')}\n"
            "\n"
            "Ports open:\n"
            "  COM15, 115200 baud, connected\n"
            "\n"
            "Latest warnings and errors in this run:\n"
            "  p1\n"
            "\n"
            "Latest events in this run:\n"
            "  e1\n"
            "  e2\n"
        )

    def test_nothing_to_list_is_said_as_none(self):
        text = diagnostic_summary(["WireTrace"], [], Path("/logs"), [], [])

        assert text.count("  none") == 3


def test_a_disk_that_does_not_answer_does_not_keep_wiretrace_from_closing(tmp_path):
    # The writing thread is held by the disk at exit; the process still
    # closes, since the logging module never waits on the run's files.
    script = tmp_path / "stuck.py"
    script.write_text(
        "import logging, os, sys, threading\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(Path(module.__file__).resolve().parents[1])!r})\n"
        "import app.diagnostic_log as dl\n"
        "dl.DIAGNOSTIC_LOG_STOP_SECONDS = 0.5\n"
        f"log = dl.DiagnosticLog(Path({str(tmp_path / 'logs')!r}))\n"
        "write, stuck = os.write, threading.Event()\n"
        "def held(stream, data):\n"
        "    if stream == log._files._stream and b'stops' in data:\n"
        "        stuck.set()\n"
        "        threading.Event().wait()\n"
        "    return write(stream, data)\n"
        "os.write = held\n"
        "log.start()\n"
        "logging.getLogger('test').warning('the disk stops answering')\n"
        "stuck.wait(5)\n"
        "log.stop()\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    finished = subprocess.run([sys.executable, str(script)], timeout=30, check=False)

    assert finished.returncode == 0


# Modules that meet the user's own files and data: what they log of an
# error goes through ``described``.
_USER_FILE_MODULES = (
    "app/config.py", "app/saved_commands.py", "core/highlight_rules.py", "core/log_writer.py",
    "core/log_files.py", "core/plot_export.py", "core/export_job.py", "ui/device_tab.py",
    "ui/features/report_feature.py", "ui/features/plot_feature.py", "ui/main_window.py",
)
_LOG_CALLS = {"debug", "info", "warning", "error", "critical", "exception"}


@pytest.mark.parametrize("name", _USER_FILE_MODULES)
def test_where_the_users_files_are_met_no_error_is_logged_in_its_own_words(name):
    import ast

    tree = ast.parse((Path(module.__file__).resolve().parents[1] / name).read_text(encoding="utf-8"))
    raw = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler) or handler.name is None:
            continue
        for call in ast.walk(handler):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr in _LOG_CALLS
                and any(isinstance(arg, ast.Name) and arg.id == handler.name for arg in call.args[1:])
            ):
                raw.append(call.lineno)

    assert raw == [], f"{name}: an error logged in its own words at lines {raw}"
