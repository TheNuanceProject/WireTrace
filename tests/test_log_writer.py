# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.log_writer, run with real threads and real files."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import core.log_files as log_files
import core.log_writer as log_writer
from core.log_writer import LogRequest, LogStartError, SessionWriter
from core.records import LineRecord, command_record, format_timestamp
from tests.file_backends import FaultyBackend, GatedBackend

_START = datetime(2026, 1, 2, 3, 4, 5)
_WAIT_SECONDS = 10.0


class RecordingListener:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.recoveries = 0
        self.dropped: list[int] = []
        self.spool_errors: list[str] = []
        self.errors: list[str] = []
        self.failed = threading.Event()
        self.recovered = threading.Event()

    def log_write_failed(self, message: str) -> None:
        self.failures.append(message)
        self.failed.set()

    def log_write_recovered(self) -> None:
        self.recoveries += 1
        self.recovered.set()

    def lines_dropped(self, count: int) -> None:
        self.dropped.append(count)

    def spool_unavailable(self, message: str) -> None:
        self.spool_errors.append(message)

    def writer_error(self, message: str) -> None:
        self.errors.append(message)


def _records(count: int, first: int = 0, text: str = "line {}") -> list[LineRecord]:
    return [
        LineRecord(_START + timedelta(milliseconds=index), text.format(index), "DATA")
        for index in range(first, first + count)
    ]


def _text_line(record: LineRecord) -> str:
    return f"[{format_timestamp(record.timestamp)}] {record.text}"


def _request(
    directory: Path, *, csv: bool = False, split_bytes: int = 0, line_ending: str = "\n",
) -> LogRequest:
    return LogRequest(
        line_ending=line_ending,
        text_path=str(directory / "session.txt"),
        csv_path=str(directory / "session.csv") if csv else None,
        session_name="bench",
        port_name="PORT0",
        baud_rate=115200,
        description="",
        split_bytes=split_bytes,
    )


def _body(path: str) -> list[str]:
    """The lines of a .txt log after its header block."""
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    rules = [index for index, line in enumerate(lines) if line.startswith("=" * 80)]
    return [line for line in lines[rules[2] + 2:] if line != ""]


def _csv_rows(path: str) -> list[str]:
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    return [line for line in lines if line and not line.startswith("#")]


def _wait_until_handled(writer: SessionWriter) -> None:
    """Return once the writer has dealt with everything submitted so far."""
    snapshot = writer.snapshot()
    if snapshot is not None:
        snapshot.release()


@pytest.fixture
def listener() -> RecordingListener:
    return RecordingListener()


@pytest.fixture
def make_writer(tmp_path, listener):
    writers: list[SessionWriter] = []

    def factory(**options) -> SessionWriter:
        settings = {
            "max_pending_lines": 100_000,
            "flush_interval_ms": 50,
            "spool_root": str(tmp_path / "spool"),
        }
        settings.update(options)
        writer = SessionWriter(listener, **settings)
        writers.append(writer)
        return writer

    yield factory
    for writer in writers:
        writer.shutdown()


class TestLogging:
    def test_every_submitted_line_is_in_the_log_in_order(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        records = _records(5_000)
        for start in range(0, 5_000, 137):
            writer.submit(records[start:start + 137])
        summary = writer.stop_log()

        assert summary.error == ""
        assert summary.lines_written == 5_000
        assert summary.lines_lost == 0
        assert _body(summary.text_paths[0]) == [_text_line(r) for r in records]

    def test_lines_submitted_just_before_stop_are_written(self, make_writer, tmp_path):
        writer = make_writer(flush_interval_ms=5_000)
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        records = _records(20_000)
        writer.submit(records)
        summary = writer.stop_log()
        assert summary.lines_written == 20_000
        assert len(_body(summary.text_paths[0])) == 20_000

    def test_log_begins_at_the_start_request(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.submit(_records(10))
        writer.start_log(_request(tmp_path))
        after = _records(5, first=10)
        writer.submit(after)
        summary = writer.stop_log()
        assert _body(summary.text_paths[0]) == [_text_line(r) for r in after]

    def test_header_reports_the_session(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        started = writer.start_log(_request(tmp_path))
        writer.stop_log()
        header = Path(started.text_path).read_text(encoding="utf-8")
        assert "Log Session" in header
        assert "Session Name  : bench" in header
        assert "Port          : PORT0" in header
        assert "Baud Rate     : 115200" in header

    def test_stop_without_a_log_returns_none(self, make_writer):
        writer = make_writer()
        writer.begin_session()
        assert writer.stop_log() is None

    def test_second_start_is_refused(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        with pytest.raises(LogStartError):
            writer.start_log(_request(tmp_path))

    def test_existing_file_is_never_replaced(self, make_writer, tmp_path):
        (tmp_path / "session.txt").write_text("precious")
        writer = make_writer()
        writer.begin_session()
        started = writer.start_log(_request(tmp_path))
        writer.stop_log()
        assert (tmp_path / "session.txt").read_text() == "precious"
        assert Path(started.text_path).name == "session_2.txt"

    def test_unwritable_directory_fails_and_leaves_nothing_behind(self, make_writer, tmp_path):
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x")
        writer = make_writer()
        writer.begin_session()
        with pytest.raises(LogStartError):
            writer.start_log(_request(blocker / "logs"))
        assert writer.stop_log() is None

    def test_command_echo_is_logged_and_counted(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        writer.submit([command_record("status", _START)])
        summary = writer.stop_log()
        assert summary.lines_written == 1
        assert _body(summary.text_paths[0])[0].endswith("] >>> status")


class TestCsv:
    def test_structured_session_is_written_as_columns(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True))
        records = _records(120, text="Temp: {}, Hum: 55")
        writer.submit(records)
        summary = writer.stop_log()
        rows = _csv_rows(summary.csv_paths[0])
        assert rows[0] == "Timestamp,Temp,Hum"
        assert len(rows) == 121
        assert rows[1] == f"{format_timestamp(records[0].timestamp)},0,55"

    def test_short_session_is_settled_when_the_log_stops(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True))
        writer.submit(_records(3, text="a={}, b=2"))
        summary = writer.stop_log()
        assert _csv_rows(summary.csv_paths[0])[0] == "Timestamp,a,b"
        assert len(_csv_rows(summary.csv_paths[0])) == 4

    def test_empty_log_has_a_raw_header_only(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True))
        summary = writer.stop_log()
        assert _csv_rows(summary.csv_paths[0]) == ["Timestamp,Data"]

    def test_command_echo_is_not_a_csv_row(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True))
        writer.submit(_records(60, text="rpm={}"))
        writer.submit([command_record("set rpm=1200", _START)])
        summary = writer.stop_log()
        assert len(_csv_rows(summary.csv_paths[0])) == 61


class TestSplitting:
    def test_text_log_is_split_and_no_line_is_lost(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, split_bytes=20_000))
        records = _records(3_000)
        writer.submit(records)
        summary = writer.stop_log()

        assert len(summary.text_paths) > 3
        assert [Path(p).name for p in summary.text_paths[:2]] == [
            "session.txt", "session_002.txt",
        ]
        body: list[str] = []
        for number, path in enumerate(summary.text_paths, start=1):
            assert os.path.getsize(path) <= 20_000
            if number > 1:
                assert f"File          : {number}" in Path(path).read_text(encoding="utf-8")
            body.extend(_body(path))
        assert body == [_text_line(r) for r in records]

    def test_csv_parts_repeat_the_header_and_respect_the_row_limit(
        self, make_writer, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(log_writer, "CSV_MAX_ROWS_PER_PART", 100)
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True, split_bytes=10**9))
        writer.submit(_records(450, text="Temp: {}, Hum: 55"))
        summary = writer.stop_log()

        assert len(summary.csv_paths) == 5
        total = 0
        for path in summary.csv_paths:
            rows = _csv_rows(path)
            assert rows[0] == "Timestamp,Temp,Hum"
            assert len(rows) - 1 <= 100
            total += len(rows) - 1
        assert total == 450

    def test_splitting_off_means_one_file(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True, split_bytes=0))
        writer.submit(_records(5_000, text="Temp: {}, Hum: 55"))
        summary = writer.stop_log()
        assert len(summary.text_paths) == 1
        assert len(summary.csv_paths) == 1


class TestLineEnding:
    def test_log_ends_its_lines_with_lf_unless_told_otherwise(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        started = writer.start_log(_request(tmp_path))
        writer.submit(_records(3))
        writer.stop_log()

        assert b"\r" not in Path(started.text_path).read_bytes()

    def test_every_line_of_both_files_ends_with_the_chosen_ending(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True, line_ending="\r\n"))
        writer.submit(_records(40, text="Temp: {}, Hum: 5"))
        writer.set_paused(True, _START)
        writer.set_paused(False, _START)
        summary = writer.stop_log()

        for path in (*summary.text_paths, *summary.csv_paths):
            data = Path(path).read_bytes()
            assert data.endswith(b"\r\n"), path
            assert data.count(b"\n") == data.count(b"\r\n"), path
        lines = Path(summary.text_paths[0]).read_bytes().split(b"\r\n")
        assert _text_line(_records(1, text="Temp: {}, Hum: 5")[0]).encode() in lines

    def test_every_part_of_a_split_log_uses_the_chosen_ending(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, split_bytes=20_000, line_ending="\r\n"))
        writer.submit(_records(1_500))
        summary = writer.stop_log()

        assert len(summary.text_paths) > 1
        for path in summary.text_paths:
            data = Path(path).read_bytes()
            assert data.count(b"\n") == data.count(b"\r\n"), path
            assert os.path.getsize(path) <= 20_000


class TestProgress:
    def test_no_progress_without_a_log(self, make_writer):
        writer = make_writer()
        writer.begin_session()

        assert writer.log_progress() is None

    def test_progress_reports_the_size_of_the_file(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        started = writer.start_log(_request(tmp_path))
        writer.submit(_records(500))
        _wait_until_handled(writer)

        progress = writer.log_progress()
        writer.stop_log()

        assert progress.part_number == 1
        assert progress.split_bytes == 0
        assert progress.part_bytes == os.path.getsize(started.text_path)

    def test_progress_follows_the_part_being_written(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, split_bytes=20_000))
        writer.submit(_records(3_000))
        _wait_until_handled(writer)

        progress = writer.log_progress()
        summary = writer.stop_log()

        assert progress.split_bytes == 20_000
        assert progress.part_number == len(summary.text_paths)
        assert progress.part_bytes == os.path.getsize(summary.text_paths[-1])

    def test_progress_ends_with_the_log(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        writer.stop_log()

        assert writer.log_progress() is None


class TestPause:
    def test_paused_lines_are_skipped_and_the_gap_is_marked(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        writer.submit(_records(2))
        paused_at = _START + timedelta(milliseconds=1, microseconds=500)
        resumed_at = _START + timedelta(milliseconds=4, microseconds=500)
        writer.set_paused(True, paused_at)
        writer.submit(_records(3, first=2))
        writer.set_paused(False, resumed_at)
        writer.submit(_records(1, first=5))
        summary = writer.stop_log()

        body = _body(summary.text_paths[0])
        assert body == [
            _text_line(_records(1)[0]),
            _text_line(_records(1, first=1)[0]),
            f"[{format_timestamp(paused_at)}] [WireTrace] Logging paused",
            f"[{format_timestamp(resumed_at)}] [WireTrace] Logging resumed",
            _text_line(_records(1, first=5)[0]),
        ]
        assert summary.lines_written == 3

    def test_spool_keeps_recording_while_the_log_is_paused(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        writer.set_paused(True, _START)
        writer.submit(_records(4))
        snapshot = writer.snapshot()
        assert len(list(snapshot.records())) == 4
        snapshot.release()

    def test_lines_dropped_while_paused_are_not_lost_from_the_log(
        self, make_writer, tmp_path, listener,
    ):
        backend = GatedBackend()
        writer = make_writer(backend=backend, max_pending_lines=10, flush_interval_ms=10)
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        writer.set_paused(True, _START)

        backend.gate.clear()
        writer.submit(_records(5))
        assert backend.blocked.wait(_WAIT_SECONDS)
        writer.submit(_records(10, first=5))
        writer.submit(_records(7, first=15))
        backend.gate.set()
        summary = writer.stop_log()

        assert listener.dropped == [7]
        assert summary.lines_lost == 0


class TestSpool:
    def test_session_is_recorded_without_a_log(self, make_writer):
        writer = make_writer()
        writer.begin_session()
        records = _records(300)
        writer.submit(records)
        snapshot = writer.snapshot()
        assert list(snapshot.records()) == records
        snapshot.release()

    def test_export_mark_hides_earlier_lines(self, make_writer):
        writer = make_writer()
        writer.begin_session()
        writer.submit(_records(10))
        writer.mark_export_start()
        after = _records(4, first=10)
        writer.submit(after)
        snapshot = writer.snapshot()
        assert list(snapshot.records()) == after
        snapshot.release()

    def test_spool_stays_readable_after_the_session_ends(self, make_writer):
        writer = make_writer()
        writer.begin_session()
        records = _records(5)
        writer.submit(records)
        writer.end_session()
        snapshot = writer.snapshot()
        assert list(snapshot.records()) == records
        snapshot.release()

    def test_export_in_progress_survives_the_start_of_a_new_session(
        self, make_writer, tmp_path,
    ):
        writer = make_writer()
        writer.begin_session()
        records = _records(5)
        writer.submit(records)
        snapshot = writer.snapshot()

        writer.begin_session()
        assert list(snapshot.records()) == records
        assert len(os.listdir(tmp_path / "spool")) == 2
        snapshot.release()
        assert len(os.listdir(tmp_path / "spool")) == 1

    def test_new_session_starts_with_an_empty_spool(self, make_writer):
        writer = make_writer()
        writer.begin_session()
        writer.submit(_records(5))
        writer.begin_session()
        snapshot = writer.snapshot()
        assert list(snapshot.records()) == []
        snapshot.release()

    def test_shutdown_removes_the_spool(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.submit(_records(5))
        writer.shutdown()
        assert os.listdir(tmp_path / "spool") == []

    def test_unusable_spool_location_is_reported_and_logging_still_works(
        self, make_writer, tmp_path, listener,
    ):
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        writer = make_writer(spool_root=str(blocker / "spool"))
        writer.begin_session()
        assert len(listener.spool_errors) == 1
        assert writer.snapshot() is None

        writer.start_log(_request(tmp_path))
        writer.submit(_records(10))
        assert writer.stop_log().lines_written == 10


    def test_spool_write_failure_is_reported_and_the_log_is_unaffected(
        self, make_writer, tmp_path, listener,
    ):
        backend = FaultyBackend()
        backend.fail_writes_to_suffix = ".spool"
        writer = make_writer(backend=backend)
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        records = _records(20)
        writer.submit(records[:10])
        writer.submit(records[10:])
        summary = writer.stop_log()

        assert len(listener.spool_errors) == 1
        assert _body(summary.text_paths[0]) == [_text_line(r) for r in records]
        assert writer.snapshot() is None
        assert os.listdir(tmp_path / "spool") == []


class TestDiskFailure:
    def test_lines_given_up_on_are_marked_in_both_files_where_they_were(
        self, make_writer, tmp_path, listener, monkeypatch,
    ):
        monkeypatch.setattr(log_files, "LOG_PENDING_MAX_BYTES", 200)
        backend = FaultyBackend()
        writer = make_writer(backend=backend, flush_interval_ms=10)
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True))
        writer.submit(_records(60, text="a: {}, b: 2"))

        backend.fail_writes = True
        writer.submit(_records(10, first=60, text="a: {}, b: 2"))
        assert listener.failed.wait(_WAIT_SECONDS)
        writer.submit(_records(7, first=70, text="a: {}, b: 2"))
        _wait_until_handled(writer)
        backend.fail_writes = False
        assert listener.recovered.wait(_WAIT_SECONDS)
        after = _records(3, first=77, text="a: {}, b: 2")
        writer.submit(after)
        summary = writer.stop_log()

        assert summary.lines_lost == 7
        assert summary.csv_rows_lost == 7
        assert summary.lines_written == 73
        body = _body(summary.text_paths[0])
        assert body[-4:] == [
            f"[{format_timestamp(after[0].timestamp)}] "
            "[WireTrace] 7 lines were not written: the disk was unavailable",
            *[_text_line(r) for r in after],
        ]
        csv_lines = Path(summary.csv_paths[0]).read_text(encoding="utf-8").split("\n")
        assert csv_lines[-5] == (
            "# [WireTrace] 7 rows were not written: the disk was unavailable"
        )

    def test_failure_is_reported_once_and_nothing_is_lost_on_recovery(
        self, make_writer, tmp_path, listener,
    ):
        backend = FaultyBackend()
        writer = make_writer(backend=backend)
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        records = _records(300)
        writer.submit(records[:100])

        backend.fail_writes = True
        writer.submit(records[100:200])
        assert listener.failed.wait(_WAIT_SECONDS)
        time.sleep(0.2)
        assert len(listener.failures) == 1

        backend.fail_writes = False
        writer.submit(records[200:])
        assert listener.recovered.wait(_WAIT_SECONDS)
        summary = writer.stop_log()

        assert summary.error == ""
        assert summary.lines_lost == 0
        assert _body(summary.text_paths[0]) == [_text_line(r) for r in records]

    def test_log_that_cannot_be_closed_reports_what_was_lost(
        self, make_writer, tmp_path, listener,
    ):
        backend = FaultyBackend()
        writer = make_writer(backend=backend)
        writer.begin_session()
        started = writer.start_log(_request(tmp_path))
        header_size = os.path.getsize(started.text_path)
        writer.submit(_records(10))
        deadline = time.monotonic() + _WAIT_SECONDS
        while (
            os.path.getsize(started.text_path) == header_size
            and time.monotonic() < deadline
        ):
            time.sleep(0.005)
        assert os.path.getsize(started.text_path) > header_size

        backend.fail_writes = True
        writer.submit(_records(7, first=10))
        summary = writer.stop_log()
        assert summary.error != ""
        assert summary.lines_written == 10
        assert summary.lines_lost == 7


class TestOverload:
    def test_lines_beyond_the_queue_limit_are_counted_reported_and_marked(
        self, make_writer, tmp_path, listener,
    ):
        backend = GatedBackend()
        writer = make_writer(backend=backend, max_pending_lines=1_000, flush_interval_ms=10)
        writer.begin_session()
        writer.start_log(_request(tmp_path))

        backend.gate.clear()
        writer.submit(_records(10))
        assert backend.blocked.wait(_WAIT_SECONDS)

        writer.submit(_records(1_000, first=10))
        writer.submit(_records(400, first=1_010))
        writer.submit(_records(300, first=1_410))

        backend.gate.set()
        summary = writer.stop_log()

        assert listener.dropped == [700]
        assert summary.lines_written == 1_010
        assert summary.lines_lost == 700
        body = _body(summary.text_paths[0])
        first_dropped = _records(1, first=1_010)[0]
        assert body[-1] == (
            f"[{format_timestamp(first_dropped.timestamp)}] "
            "[WireTrace] 700 lines were not recorded: the disk could not keep up"
        )
        assert len(body) == 1_011

    def test_idle_writer_accepts_a_batch_larger_than_the_queue_limit(
        self, make_writer, tmp_path, listener,
    ):
        writer = make_writer(max_pending_lines=1_000)
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        writer.submit(_records(1_001))
        summary = writer.stop_log()

        assert listener.dropped == []
        assert summary.lines_written == 1_001
        assert summary.lines_lost == 0


    def test_a_backlog_is_bounded_by_its_characters_as_well(
        self, make_writer, tmp_path, listener, monkeypatch,
    ):
        monkeypatch.setattr(log_writer, "LOG_PENDING_MAX_CHARACTERS", 1_000)
        backend = GatedBackend()
        writer = make_writer(backend=backend, max_pending_lines=10**6, flush_interval_ms=10)
        writer.begin_session()
        writer.start_log(_request(tmp_path))

        backend.gate.clear()
        writer.submit(_records(1))
        assert backend.blocked.wait(_WAIT_SECONDS)

        writer.submit(_records(4, first=1, text="x" * 200))
        writer.submit(_records(3, first=5, text="y" * 200))

        backend.gate.set()
        summary = writer.stop_log()

        assert listener.dropped == [3]
        assert summary.lines_written == 5
        assert summary.lines_lost == 3


class TestVolume:
    def test_two_hundred_thousand_lines_are_written_exactly(self, make_writer, tmp_path):
        writer = make_writer(max_pending_lines=10**9)
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True, split_bytes=4 * 1024 * 1024))
        total = 200_000
        batch = 500
        started = time.perf_counter()
        for start in range(0, total, batch):
            writer.submit(_records(batch, first=start, text="Temp: {}, Hum: 55"))
        summary = writer.stop_log()
        elapsed = time.perf_counter() - started

        assert summary.lines_written == total
        assert summary.lines_lost == 0
        assert sum(len(_body(path)) for path in summary.text_paths) == total
        assert sum(len(_csv_rows(path)) - 1 for path in summary.csv_paths) == total
        assert total / elapsed > 10_000


class TestNoSession:
    def test_log_cannot_start_before_a_session(self, make_writer, tmp_path):
        writer = make_writer()
        with pytest.raises(LogStartError, match="not connected"):
            writer.start_log(_request(tmp_path))
        assert list(tmp_path.glob("session*")) == []

    def test_log_cannot_start_after_the_session_ended(self, make_writer, tmp_path):
        writer = make_writer()
        writer.begin_session()
        writer.end_session()
        with pytest.raises(LogStartError, match="not connected"):
            writer.start_log(_request(tmp_path))
        assert list(tmp_path.glob("session*")) == []


class TestDurability:
    def test_log_is_synced_on_a_schedule(self, make_writer, tmp_path, monkeypatch):
        monkeypatch.setattr(log_writer, "LOG_FSYNC_INTERVAL_MS", 50)
        backend = FaultyBackend()
        writer = make_writer(backend=backend, flush_interval_ms=10)
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        writer.submit(_records(3))

        deadline = time.monotonic() + _WAIT_SECONDS
        while backend.sync_calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backend.sync_calls >= 2
        writer.stop_log()

    def test_stopping_the_log_syncs_it(self, make_writer, tmp_path):
        backend = FaultyBackend()
        writer = make_writer(backend=backend)
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        writer.submit(_records(3))
        writer.stop_log()
        assert backend.sync_calls == 1

    def test_a_large_backlog_is_written_before_the_flush_interval(
        self, make_writer, tmp_path,
    ):
        writer = make_writer(flush_interval_ms=60_000)
        writer.begin_session()
        started = writer.start_log(_request(tmp_path))
        header_size = os.path.getsize(started.text_path)
        line_count = log_writer.LOG_FLUSH_THRESHOLD_BYTES // 20
        writer.submit(_records(line_count, text="x" * 40 + " {}"))

        deadline = time.monotonic() + _WAIT_SECONDS
        while (
            os.path.getsize(started.text_path) == header_size
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert os.path.getsize(started.text_path) > header_size
        writer.stop_log()


class TestNothingBlocksTheCaller:
    def test_pause_and_export_mark_return_while_the_disk_is_stalled(
        self, make_writer, tmp_path,
    ):
        backend = GatedBackend()
        writer = make_writer(backend=backend, flush_interval_ms=10)
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        backend.gate.clear()
        writer.submit(_records(5))
        assert backend.blocked.wait(_WAIT_SECONDS)

        returned = threading.Event()

        def use_the_controls() -> None:
            writer.set_paused(True, _START)
            writer.mark_export_start()
            returned.set()

        threading.Thread(target=use_the_controls, daemon=True).start()
        assert returned.wait(_WAIT_SECONDS)
        backend.gate.set()
        writer.stop_log()


class TestUnexpectedErrors:
    def test_csv_error_costs_the_text_log_nothing(
        self, make_writer, tmp_path, listener, monkeypatch,
    ):
        real_feed = log_writer.CSVEngine.feed

        def failing_feed(self, timestamp, line):
            if line == "line 3":
                raise RuntimeError("engine fault")
            return real_feed(self, timestamp, line)

        monkeypatch.setattr(log_writer.CSVEngine, "feed", failing_feed)
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True))
        records = _records(10)
        writer.submit(records)
        summary = writer.stop_log()

        assert _body(summary.text_paths[0]) == [_text_line(r) for r in records]
        assert summary.lines_written == 10
        assert summary.lines_lost == 0
        assert len(listener.errors) == 1
        assert "building the CSV log" in listener.errors[0]
        assert "engine fault" in listener.errors[0]

    def test_error_while_recording_is_reported_and_the_writer_carries_on(
        self, make_writer, tmp_path, listener, monkeypatch,
    ):
        real_encode = log_writer.encode_text_line

        def failing_encode(record, line_ending):
            if record.text == "poison":
                raise RuntimeError("encoder fault")
            return real_encode(record, line_ending)

        monkeypatch.setattr(log_writer, "encode_text_line", failing_encode)
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        before, after = _records(2), _records(2, first=10)
        poison = _records(2, first=5, text="poison")
        writer.submit([*before, *poison, *after])
        summary = writer.stop_log()

        assert len(listener.errors) == 1
        assert "encoder fault" in listener.errors[0]
        assert _body(summary.text_paths[0]) == [_text_line(r) for r in [*before, *after]]
        assert summary.lines_written == 4
        assert summary.lines_lost == 2

    def test_error_in_a_request_nobody_waits_for_is_reported(
        self, make_writer, tmp_path, listener, monkeypatch,
    ):
        def failing_mark(self):
            raise RuntimeError("mark fault")

        monkeypatch.setattr(log_writer.SessionSpool, "mark", failing_mark)
        writer = make_writer()
        writer.begin_session()
        writer.mark_export_start()
        _wait_until_handled(writer)

        assert len(listener.errors) == 1
        assert "mark fault" in listener.errors[0]

    def test_error_in_the_scheduled_flush_is_reported_once_per_attempt(
        self, make_writer, tmp_path, listener, monkeypatch,
    ):
        attempts = []

        def failing_flush(self):
            attempts.append(time.monotonic())
            raise RuntimeError("flush fault")

        writer = make_writer(flush_interval_ms=50)
        writer.begin_session()
        writer.start_log(_request(tmp_path))
        monkeypatch.setattr(SessionWriter, "_flush_log", failing_flush)
        time.sleep(0.5)
        monkeypatch.undo()
        writer.stop_log()

        # On schedule, not in a tight loop: about ten attempts in half
        # a second, where a loop without a pause makes thousands.
        assert 1 <= len(attempts) <= 20
        assert len(listener.errors) == len(attempts)

    def test_deeply_nested_line_does_not_stop_a_csv_log(
        self, make_writer, tmp_path, listener,
    ):
        writer = make_writer()
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True))
        nested = LineRecord(_START, '{"a":' + "[" * 60_000, "DATA")
        records = [*_records(3), nested, *_records(3, first=3)]
        writer.submit(records)
        summary = writer.stop_log()

        assert listener.errors == []
        assert summary.lines_written == 7
        assert len(_csv_rows(summary.csv_paths[0])) == 8


class TestCsvLoss:
    def test_csv_rows_that_could_not_be_written_are_in_the_summary(
        self, make_writer, tmp_path,
    ):
        backend = FaultyBackend()
        writer = make_writer(backend=backend, flush_interval_ms=10)
        writer.begin_session()
        writer.start_log(_request(tmp_path, csv=True))
        backend.fail_writes = True
        writer.submit(_records(5, text="plain text {}"))
        summary = writer.stop_log()

        assert summary.error != ""
        assert summary.lines_lost == 5
        assert summary.csv_rows_lost == 5
