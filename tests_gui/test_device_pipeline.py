# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the device pipeline: real threads, a loopback port, real files."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from app.constants import DisplayMode, ExportFormat, LineEnding
from core.device_pipeline import DevicePipeline, DisplayBuffer, arrival_times
from core.export_job import ExportRequest
from core.log_writer import LogRequest, LogStartError
from core.records import LineRecord, RecordOrigin
from tests_gui.support import LoopbackPort, line_text, log_body, wait_until

_PORT = "PORT0"
_BAUD = 115200


class TestArrivalTimes:
    _NOW = datetime(2026, 1, 1, 12, 0, 0)

    def test_first_read_carries_the_read_time(self):
        assert arrival_times(3, None, self._NOW) == [self._NOW] * 3

    def test_lines_are_spread_evenly_since_the_previous_read(self):
        previous = self._NOW - timedelta(milliseconds=20)
        times = arrival_times(4, previous, self._NOW)
        assert times == [
            previous + timedelta(milliseconds=5),
            previous + timedelta(milliseconds=10),
            previous + timedelta(milliseconds=15),
            self._NOW,
        ]

    def test_single_line_carries_the_read_time(self):
        previous = self._NOW - timedelta(milliseconds=20)
        assert arrival_times(1, previous, self._NOW) == [self._NOW]

    def test_burst_after_a_silence_is_not_spread_over_the_silence(self):
        previous = self._NOW - timedelta(seconds=30)
        assert arrival_times(3, previous, self._NOW) == [self._NOW] * 3

    def test_clock_moving_backwards_never_produces_earlier_stamps(self):
        previous = self._NOW + timedelta(seconds=1)
        assert arrival_times(2, previous, self._NOW) == [self._NOW] * 2

    def test_tiny_interval_with_many_lines_stays_ordered_and_bounded(self):
        previous = self._NOW - timedelta(microseconds=13)
        times = arrival_times(25, previous, self._NOW)
        assert times == sorted(times)
        assert times[-1] == self._NOW
        assert all(previous <= moment <= self._NOW for moment in times)

    def test_no_lines_no_times(self):
        assert arrival_times(0, None, self._NOW) == []


class Events:
    """Collects what a pipeline reports."""

    def __init__(self, pipeline: DevicePipeline) -> None:
        self.connected: list[tuple[str, int]] = []
        self.connect_failed: list[tuple[str, str]] = []
        self.disconnected: list[tuple[str, bool, object]] = []
        self.command_failed: list[str] = []
        self.stats: list[tuple[int, int]] = []
        self.exports: list[object] = []
        pipeline.connected.connect(lambda *a: self.connected.append(a))
        pipeline.connect_failed.connect(lambda *a: self.connect_failed.append(a))
        pipeline.disconnected.connect(lambda *a: self.disconnected.append(a))
        pipeline.command_failed.connect(self.command_failed.append)
        pipeline.stats_updated.connect(lambda *a: self.stats.append(a))
        pipeline.export_finished.connect(self.exports.append)


@pytest.fixture
def port() -> LoopbackPort:
    return LoopbackPort()


@pytest.fixture
def make_pipeline(qapp: QApplication, tmp_path: Path, port: LoopbackPort):
    pipelines: list[DevicePipeline] = []

    def factory(**options) -> DevicePipeline:
        settings = {
            "spool_root": str(tmp_path / "spool"),
            "display_capacity": 100_000,
            "max_pending_lines": 1_000_000,
            "flush_interval_ms": 50,
            "transport_factory": port.factory,
        }
        settings.update(options)
        pipeline = DevicePipeline(**settings)
        pipelines.append(pipeline)
        return pipeline

    yield factory
    for pipeline in pipelines:
        pipeline.shutdown()


def _connect(qapp, pipeline: DevicePipeline) -> Events:
    events = Events(pipeline)
    pipeline.open(_PORT, _BAUD)
    wait_until(qapp, lambda: bool(events.connected))
    return events


def _take_all(qapp, pipeline: DevicePipeline, count: int) -> list[LineRecord]:
    records: list[LineRecord] = []

    def collected() -> bool:
        records.extend(pipeline.take_display(10_000)[0])
        return len(records) >= count

    wait_until(qapp, collected)
    return records


def _log_request(directory: Path, csv: bool = False) -> LogRequest:
    return LogRequest(
        text_path=str(directory / "session.txt"),
        csv_path=str(directory / "session.csv") if csv else None,
        session_name="bench", port_name=_PORT, baud_rate=_BAUD,
        description="", split_bytes=0,
    )


class TestConnection:
    def test_open_reports_connected(self, qapp, make_pipeline):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        assert events.connected == [(_PORT, _BAUD)]

    def test_refused_port_reports_the_reason(self, qapp, make_pipeline, port):
        port.refuse_with = "Access is denied"
        pipeline = make_pipeline()
        events = Events(pipeline)
        pipeline.open(_PORT, _BAUD)
        wait_until(qapp, lambda: bool(events.connect_failed))
        assert events.connect_failed == [(_PORT, "Access is denied")]

    def test_close_reports_an_expected_disconnect(self, qapp, make_pipeline):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        pipeline.close()
        wait_until(qapp, lambda: bool(events.disconnected))
        assert events.disconnected == [(_PORT, False, None)]

    def test_lost_link_reports_an_unexpected_disconnect(self, qapp, make_pipeline, port):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        port.transport.drop()
        wait_until(qapp, lambda: bool(events.disconnected))
        assert events.disconnected[0][:2] == (_PORT, True)

    def test_reconnect_starts_a_clean_session(self, qapp, make_pipeline, port):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        port.feed(b"first session\n")
        _take_all(qapp, pipeline, 1)
        pipeline.close()
        wait_until(qapp, lambda: bool(events.disconnected))

        pipeline.open(_PORT, _BAUD)
        wait_until(qapp, lambda: len(events.connected) == 2)
        port.feed(b"second session\n")
        records = _take_all(qapp, pipeline, 1)
        assert [r.text for r in records] == ["second session"]


class TestLines:
    def test_lines_arrive_in_order_with_tags(self, qapp, make_pipeline, port):
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        port.feed(b"Temp: 22.4\r\nE (10) wifi: timeout\nready\r")
        records = _take_all(qapp, pipeline, 3)
        assert [(r.text, r.tag) for r in records] == [
            ("Temp: 22.4", "DATA"),
            ("E (10) wifi: timeout", "ERROR"),
            ("ready", "INFO"),
        ]
        assert all(r.origin is RecordOrigin.DEVICE for r in records)

    def test_line_split_across_reads_is_one_line(self, qapp, make_pipeline, port):
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        for piece in (b"Te", b"mp: 2", b"2.4\r", b"\nnext\n"):
            port.feed(piece)
        records = _take_all(qapp, pipeline, 2)
        assert [r.text for r in records] == ["Temp: 22.4", "next"]

    def test_blank_lines_are_kept_and_escape_only_lines_are_not(
        self, qapp, make_pipeline, port,
    ):
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        port.feed(b"one\n\n\x1b[2J\ntwo\n")
        records = _take_all(qapp, pipeline, 3)
        assert [r.text for r in records] == ["one", "", "two"]

    def test_unterminated_tail_is_delivered_at_disconnect(self, qapp, make_pipeline, port):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        port.feed(b"complete\nno newline yet")
        pipeline.close()
        wait_until(qapp, lambda: bool(events.disconnected))
        records = _take_all(qapp, pipeline, 2)
        assert [r.text for r in records] == ["complete", "no newline yet"]

    def test_timestamps_never_decrease(self, qapp, make_pipeline, port, tmp_path):
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        pipeline.start_log(_log_request(tmp_path))
        records: list[LineRecord] = []
        # Reads a few milliseconds apart, so their lines are spread
        # over the interval, with commands and pauses between them.
        for chunk in range(20):
            port.feed(b"".join(f"c{chunk} l{i}\n".encode() for i in range(25)))
            records.extend(_take_all(qapp, pipeline, 25))
            pipeline.send_command(f"cmd{chunk}")
            records.extend(_take_all(qapp, pipeline, 1))
            pipeline.set_log_paused(chunk % 2 == 0)
        stamps = [r.timestamp for r in records]
        assert len(stamps) == 520
        assert stamps == sorted(stamps)

        summary = pipeline.stop_log()
        logged = [line.split("] ", 1)[0] for line in log_body(summary.text_paths[0])]
        assert any("Logging paused" in line for line in log_body(summary.text_paths[0]))
        assert logged == sorted(logged)

    def test_stats_report_the_total(self, qapp, make_pipeline, port):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        port.feed(b"a\nb\nc\n")
        wait_until(qapp, lambda: any(total == 3 for _, total in events.stats))


class TestDisplayBuffer:
    def test_overflow_skips_the_oldest_in_view_but_not_in_the_log(
        self, qapp, make_pipeline, port, tmp_path,
    ):
        pipeline = make_pipeline(display_capacity=100)
        events = _connect(qapp, pipeline)
        pipeline.start_log(_log_request(tmp_path))
        port.feed(b"".join(f"line {i}\n".encode() for i in range(1_000)))
        wait_until(qapp, lambda: any(total == 1_000 for _, total in events.stats))

        records, skipped = pipeline.take_display(10_000)
        assert skipped == 900
        assert [r.text for r in records] == [f"line {i}" for i in range(900, 1_000)]

        summary = pipeline.stop_log()
        assert summary.lines_written == 1_000
        assert pipeline.take_display(10)[1] == 0


    def test_the_characters_waiting_are_bounded_as_well(self):
        buffer = DisplayBuffer(capacity=10_000, max_characters=1_000)
        start = datetime(2026, 1, 2, 3, 4, 5)

        buffer.put([LineRecord(start, f"{n:03d}" + "x" * 97, "DATA") for n in range(25)])

        records, skipped = buffer.take(10_000)
        assert skipped == 15
        assert [record.text[:3] for record in records] == [f"{n:03d}" for n in range(15, 25)]

        buffer.put([LineRecord(start, "y" * 600, "DATA"), LineRecord(start, "z" * 600, "DATA")])
        records, skipped = buffer.take(10_000)
        assert skipped == 1
        assert [record.text[0] for record in records] == ["z"]

    def test_backlog_can_be_cut_to_the_newest_lines(self, qapp, make_pipeline, port):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        port.feed(b"".join(f"line {i}\n".encode() for i in range(1_000)))
        wait_until(qapp, lambda: any(total == 1_000 for _, total in events.stats))

        pipeline.limit_display_backlog(10)
        records, skipped = pipeline.take_display(10_000)
        assert skipped == 990
        assert [r.text for r in records] == [f"line {i}" for i in range(990, 1_000)]

        pipeline.limit_display_backlog(10)
        assert pipeline.take_display(10) == ([], 0)

    def test_discarded_backlog_is_not_reported_as_skipped(
        self, qapp, make_pipeline, port,
    ):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        port.feed(b"one\ntwo\n")
        wait_until(qapp, lambda: any(total == 2 for _, total in events.stats))

        pipeline.discard_display()
        assert pipeline.take_display(10) == ([], 0)


class TestLogging:
    def test_every_line_before_disconnect_is_in_the_log(
        self, qapp, make_pipeline, port, tmp_path,
    ):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        pipeline.start_log(_log_request(tmp_path))

        total = 20_000
        expected = [f"burst line {i}" for i in range(total)]
        payload = "".join(f"{text}\n" for text in expected).encode()
        for start in range(0, len(payload), 4_096):
            port.feed(payload[start:start + 4_096])
        port.feed(b"last line without newline")
        pipeline.close()
        wait_until(qapp, lambda: bool(events.disconnected))

        summary = events.disconnected[0][2]
        assert summary.error == ""
        assert summary.lines_lost == 0
        assert summary.lines_written == total + 1
        body = [line_text(line) for line in log_body(summary.text_paths[0])]
        assert body == [*expected, "last line without newline"]

    def test_unplugged_device_still_gets_a_complete_log(
        self, qapp, make_pipeline, port, tmp_path,
    ):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        pipeline.start_log(_log_request(tmp_path))
        port.feed(b"".join(f"line {i}\n".encode() for i in range(500)))
        port.transport.drop()
        wait_until(qapp, lambda: bool(events.disconnected))

        _, unexpected, summary = events.disconnected[0]
        assert unexpected
        assert summary.lines_written == 500

    def test_console_and_log_share_one_timestamp_per_line(
        self, qapp, make_pipeline, port, tmp_path,
    ):
        from core.records import format_timestamp

        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        pipeline.start_log(_log_request(tmp_path))
        port.feed(b"a\nb\nc\n")
        records = _take_all(qapp, pipeline, 3)
        summary = pipeline.stop_log()
        assert log_body(summary.text_paths[0]) == [
            f"[{format_timestamp(r.timestamp)}] {r.text}" for r in records
        ]

    def test_log_cannot_start_without_a_connection(self, qapp, make_pipeline, tmp_path):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        pipeline.close()
        wait_until(qapp, lambda: bool(events.disconnected))
        with pytest.raises(LogStartError, match="not connected"):
            pipeline.start_log(_log_request(tmp_path))
        assert list(tmp_path.glob("session*")) == []

    def test_start_failure_is_raised(self, qapp, make_pipeline, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        with pytest.raises(LogStartError):
            pipeline.start_log(_log_request(blocker / "sub"))

    def test_csv_is_written_alongside(self, qapp, make_pipeline, port, tmp_path):
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        pipeline.start_log(_log_request(tmp_path, csv=True))
        port.feed(b"".join(f"Temp: {i}, Hum: 55\n".encode() for i in range(80)))
        _take_all(qapp, pipeline, 80)
        summary = pipeline.stop_log()
        rows = [
            line for line in Path(summary.csv_paths[0]).read_text().split("\n")
            if line and not line.startswith("#")
        ]
        assert rows[0] == "Timestamp,Temp,Hum"
        assert len(rows) == 81


class TestCommands:
    def test_command_is_sent_and_echoed_into_the_session(
        self, qapp, make_pipeline, port, tmp_path,
    ):
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        pipeline.start_log(_log_request(tmp_path))
        pipeline.send_command("status")
        records = _take_all(qapp, pipeline, 1)
        assert port.transport.written == [b"status\n"]
        assert records[0].text == ">>> status"
        assert records[0].tag == "COMMAND"
        assert records[0].origin is RecordOrigin.COMMAND
        summary = pipeline.stop_log()
        assert line_text(log_body(summary.text_paths[0])[0]) == ">>> status"

    @pytest.mark.parametrize(
        ("ending", "sent"),
        [
            (LineEnding.NONE, b"status"),
            (LineEnding.LF, b"status\n"),
            (LineEnding.CR, b"status\r"),
            (LineEnding.CRLF, b"status\r\n"),
        ],
    )
    def test_command_is_followed_by_the_chosen_line_ending(
        self, qapp, make_pipeline, port, ending, sent,
    ):
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        pipeline.send_command("status", ending)
        records = _take_all(qapp, pipeline, 1)

        assert port.transport.written == [sent]
        assert records[0].text == ">>> status"

    def test_failed_write_is_reported_and_not_echoed(self, qapp, make_pipeline, port):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        port.transport.write_error = "Device not ready"
        pipeline.send_command("status")
        wait_until(qapp, lambda: bool(events.command_failed))
        assert events.command_failed == ["Device not ready"]
        assert pipeline.take_display(10)[0] == []

    def test_command_while_disconnected_is_reported(self, qapp, make_pipeline):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        pipeline.close()
        wait_until(qapp, lambda: bool(events.disconnected))
        pipeline.send_command("status")
        wait_until(qapp, lambda: bool(events.command_failed))


class TestHexMode:
    def test_bytes_are_shown_as_hex_rows(self, qapp, make_pipeline, port):
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        pipeline.set_display_mode(DisplayMode.HEX)
        port.feed(bytes(range(0x41, 0x51)))
        records = _take_all(qapp, pipeline, 1)
        assert records[0].text == (
            "00000000  41 42 43 44 45 46 47 48  49 4A 4B 4C 4D 4E 4F 50  "
            "|ABCDEFGHIJKLMNOP|"
        )
        assert records[0].hex_row is True

    def test_incomplete_row_is_shown_once_the_stream_goes_quiet(
        self, qapp, make_pipeline, port,
    ):
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        pipeline.set_display_mode(DisplayMode.HEX)
        port.feed(b"AB")
        records = _take_all(qapp, pipeline, 1)
        assert records[0].text.startswith("00000000  41 42 ")
        assert records[0].text.endswith("|AB|")

    def test_switching_mode_shows_what_the_old_mode_still_held(
        self, qapp, make_pipeline, port,
    ):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        port.feed(b"whole\npartial")
        wait_until(qapp, lambda: any(total == 1 for _, total in events.stats))
        pipeline.set_display_mode(DisplayMode.HEX)
        records = _take_all(qapp, pipeline, 2)
        assert [r.text for r in records] == ["whole", "partial"]


class TestExport:
    def test_whole_session_is_exported_without_logging(
        self, qapp, make_pipeline, port, tmp_path,
    ):
        pipeline = make_pipeline(display_capacity=10)
        events = _connect(qapp, pipeline)
        port.feed(b"".join(f"line {i}\n".encode() for i in range(300)))
        wait_until(qapp, lambda: any(total == 300 for _, total in events.stats))

        text_path, csv_path = ExportRequest.paths_for(
            ExportFormat.TXT, str(tmp_path / "export"),
        )
        assert pipeline.export(ExportRequest(text_path, csv_path, "x", _PORT, _BAUD, 0))
        wait_until(qapp, lambda: bool(events.exports))

        result = events.exports[0]
        assert result.error == ""
        assert result.lines == 300
        assert not pipeline.is_exporting
        assert [line_text(line) for line in log_body(result.paths[0])] == [
            f"line {i}" for i in range(300)
        ]

    def test_export_starts_at_the_mark(self, qapp, make_pipeline, port, tmp_path):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        port.feed(b"before\n")
        wait_until(qapp, lambda: any(total == 1 for _, total in events.stats))
        pipeline.mark_export_start()
        port.feed(b"after\n")
        wait_until(qapp, lambda: any(total == 2 for _, total in events.stats))

        text_path, _ = ExportRequest.paths_for(ExportFormat.TXT, str(tmp_path / "export"))
        pipeline.export(ExportRequest(text_path, None, "x", _PORT, _BAUD, 0))
        wait_until(qapp, lambda: bool(events.exports))
        assert [line_text(line) for line in log_body(events.exports[0].paths[0])] == ["after"]

    def test_export_without_a_session_is_refused(self, make_pipeline, tmp_path):
        pipeline = make_pipeline()
        assert not pipeline.export(
            ExportRequest(str(tmp_path / "e.txt"), None, "x", "", 0, 0),
        )

    def test_export_with_nothing_recorded_is_refused_and_writes_no_file(
        self, qapp, make_pipeline, port, tmp_path,
    ):
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        assert not pipeline.export(
            ExportRequest(str(tmp_path / "e.txt"), None, "x", _PORT, _BAUD, 0),
        )
        assert not pipeline.is_exporting
        assert list(tmp_path.glob("e*")) == []

    def test_session_can_still_be_exported_after_a_failed_reconnect(
        self, qapp, make_pipeline, port, tmp_path,
    ):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        port.feed(b"one\ntwo\n")
        wait_until(qapp, lambda: any(total == 2 for _, total in events.stats))
        pipeline.close()
        wait_until(qapp, lambda: bool(events.disconnected))

        port.refuse_with = "Access denied"
        pipeline.open(_PORT, _BAUD)
        wait_until(qapp, lambda: bool(events.connect_failed))

        text_path = str(tmp_path / "after.txt")
        assert pipeline.export(ExportRequest(text_path, None, "x", _PORT, _BAUD, 0))
        wait_until(qapp, lambda: bool(events.exports))
        assert [line_text(line) for line in log_body(events.exports[0].paths[0])] == [
            "one", "two",
        ]


class TestShutdown:
    def test_shutdown_writes_the_log_and_returns_its_summary(
        self, qapp, make_pipeline, port, tmp_path,
    ):
        pipeline = make_pipeline()
        events = _connect(qapp, pipeline)
        pipeline.start_log(_log_request(tmp_path))
        port.feed(b"".join(f"line {i}\n".encode() for i in range(2_000)))
        summary = pipeline.shutdown()

        assert summary.lines_written == 2_000
        qapp.processEvents()
        assert events.disconnected == []
        assert list((tmp_path / "spool").iterdir()) == []

    def test_shutdown_of_an_unused_pipeline(self, make_pipeline):
        assert make_pipeline().shutdown() is None

    def test_shutdown_twice_is_harmless(self, qapp, make_pipeline):
        pipeline = make_pipeline()
        _connect(qapp, pipeline)
        pipeline.shutdown()
        assert pipeline.shutdown() is None
