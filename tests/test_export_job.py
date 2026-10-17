# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.export_job, run with real threads and real files."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import core.export_job as export_job
from app.constants import ExportFormat
from core.export_job import ExportJob, ExportRequest, ExportResult
from core.records import LineRecord, command_record, format_timestamp, notice_record
from core.session_spool import SessionSpool
from tests.file_backends import FaultyBackend

_START = datetime(2026, 1, 2, 3, 4, 5)


def _records(count: int, text: str = "Temp: {}, Hum: 55") -> list[LineRecord]:
    return [
        LineRecord(_START + timedelta(milliseconds=index), text.format(index), "DATA")
        for index in range(count)
    ]


@pytest.fixture
def spool(tmp_path):
    instance = SessionSpool(str(tmp_path / "spool"))
    instance.create()
    yield instance
    instance.delete()


def _export(spool, directory: Path, export_format: ExportFormat, **options) -> ExportResult:
    text_path, csv_path = ExportRequest.paths_for(export_format, str(directory / "export"))
    request = ExportRequest(
        text_path=text_path, csv_path=csv_path, name="bench",
        port_name="PORT0", baud_rate=115200,
        split_bytes=options.pop("split_bytes", 0),
        line_ending=options.pop("line_ending", "\n"),
    )
    results: list[ExportResult] = []
    done = threading.Event()

    def finished(result: ExportResult) -> None:
        results.append(result)
        done.set()

    job = ExportJob(spool.snapshot(), request, finished, **options)
    job.start()
    assert done.wait(10.0)
    job.join()
    return results[0]


def _data_lines(path: str) -> list[str]:
    return [
        line for line in Path(path).read_text(encoding="utf-8").split("\n")
        if line.startswith("[")
    ]


class TestPathsFor:
    def test_each_format_selects_its_files(self):
        assert ExportRequest.paths_for(ExportFormat.TXT, "x") == ("x.txt", None)
        assert ExportRequest.paths_for(ExportFormat.CSV, "x") == (None, "x.csv")
        assert ExportRequest.paths_for(ExportFormat.BOTH, "x") == ("x.txt", "x.csv")


class TestExport:
    def test_text_export_holds_every_line(self, spool, tmp_path):
        records = _records(500)
        spool.append(records)
        result = _export(spool, tmp_path, ExportFormat.TXT)

        assert result.error == ""
        assert result.lines == 500
        assert result.paths == (str(tmp_path / "export.txt"),)
        assert _data_lines(result.paths[0]) == [
            f"[{format_timestamp(r.timestamp)}] {r.text}" for r in records
        ]
        header = Path(result.paths[0]).read_text(encoding="utf-8")
        assert "Exported Data" in header
        assert "Export Name   : bench" in header

    def test_export_ends_its_lines_with_lf_unless_told_otherwise(self, spool, tmp_path):
        spool.append(_records(10))
        result = _export(spool, tmp_path, ExportFormat.BOTH)

        for path in result.paths:
            assert b"\r" not in Path(path).read_bytes(), path

    def test_every_line_of_both_files_ends_with_the_chosen_ending(self, spool, tmp_path):
        spool.append(_records(120))
        result = _export(spool, tmp_path, ExportFormat.BOTH, line_ending="\r\n")

        assert len(result.paths) == 2
        for path in result.paths:
            data = Path(path).read_bytes()
            assert data.endswith(b"\r\n"), path
            assert data.count(b"\n") == data.count(b"\r\n"), path

    def test_both_formats_are_written(self, spool, tmp_path):
        spool.append(_records(120))
        result = _export(spool, tmp_path, ExportFormat.BOTH)
        assert [Path(p).name for p in result.paths] == ["export.txt", "export.csv"]
        rows = [
            line for line in Path(result.paths[1]).read_text().split("\n")
            if line and not line.startswith("#")
        ]
        assert rows[0] == "Timestamp,Temp,Hum"
        assert len(rows) == 121

    def test_notices_are_kept_in_text_but_not_counted_or_in_csv(self, spool, tmp_path):
        spool.append(_records(60))
        spool.append([
            notice_record("3 lines skipped", _START),
            command_record("status", _START),
        ])
        result = _export(spool, tmp_path, ExportFormat.BOTH)
        assert result.lines == 61
        assert len(_data_lines(result.paths[0])) == 62
        rows = [
            line for line in Path(result.paths[1]).read_text().split("\n")
            if line and not line.startswith("#")
        ]
        assert len(rows) == 61

    def test_large_export_is_split_into_parts(self, spool, tmp_path):
        records = _records(2_000)
        spool.append(records)
        result = _export(spool, tmp_path, ExportFormat.TXT, split_bytes=20_000)
        assert len(result.paths) > 2
        assert sum(len(_data_lines(path)) for path in result.paths) == 2_000

    def test_csv_export_starts_a_new_part_at_the_row_limit(
        self, spool, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(export_job, "CSV_MAX_ROWS_PER_PART", 100)
        spool.append(_records(250))
        result = _export(spool, tmp_path, ExportFormat.CSV, split_bytes=10**9)

        assert len(result.paths) == 3
        row_counts = []
        for path in result.paths:
            lines = [
                line for line in Path(path).read_text(encoding="utf-8").split("\n")
                if line and not line.startswith("#")
            ]
            assert lines[0] == "Timestamp,Temp,Hum"
            row_counts.append(len(lines) - 1)
        assert row_counts == [100, 100, 50]

    def test_empty_session_gives_header_only_files(self, spool, tmp_path):
        result = _export(spool, tmp_path, ExportFormat.BOTH)
        assert result.lines == 0
        assert _data_lines(result.paths[0]) == []

    def test_existing_file_is_not_replaced(self, spool, tmp_path):
        (tmp_path / "export.txt").write_text("precious")
        spool.append(_records(3))
        result = _export(spool, tmp_path, ExportFormat.TXT)
        assert (tmp_path / "export.txt").read_text() == "precious"
        assert Path(result.paths[0]).name == "export_2.txt"

    def test_truncated_session_is_noted_in_the_header(self, tmp_path):
        spool = SessionSpool(str(tmp_path / "spool"), segment_bytes=2_000, max_bytes=4_000)
        spool.create()
        for start in range(0, 2_000, 20):
            spool.append(_records(2_000)[start:start + 20])
        result = _export(spool, tmp_path, ExportFormat.TXT)
        assert "no longer available" in Path(result.paths[0]).read_text(encoding="utf-8")
        spool.delete()


class TestFailure:
    def test_write_error_fails_the_export_and_removes_partial_files(self, spool, tmp_path):
        spool.append(_records(50))
        backend = FaultyBackend()
        backend.accept_before_failing = 400
        result = _export(spool, tmp_path, ExportFormat.BOTH, backend=backend)
        assert result.error != ""
        assert result.paths == ()
        assert [name for name in os.listdir(tmp_path) if name.startswith("export")] == []

    def test_unwritable_directory_fails_the_export(self, spool, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        spool.append(_records(5))
        result = _export(spool, blocker / "sub", ExportFormat.TXT)
        assert result.error != ""

    def test_snapshot_is_released_after_failure(self, spool, tmp_path):
        spool.append(_records(5))
        backend = FaultyBackend()
        backend.fail_creates = True
        _export(spool, tmp_path, ExportFormat.TXT, backend=backend)
        assert spool._holds == 0


class TestCancel:
    def test_cancelled_export_removes_its_files(self, spool, tmp_path):
        spool.append(_records(5_000))
        text_path, csv_path = ExportRequest.paths_for(
            ExportFormat.BOTH, str(tmp_path / "export"),
        )
        request = ExportRequest(text_path, csv_path, "bench", "PORT0", 115200, 0)
        results: list[ExportResult] = []
        done = threading.Event()

        def finished(result: ExportResult) -> None:
            results.append(result)
            done.set()

        job = ExportJob(spool.snapshot(), request, finished)
        job.cancel()
        job.start()
        assert done.wait(10.0)
        job.join()
        assert results[0].cancelled
        assert [name for name in os.listdir(tmp_path) if name.startswith("export")] == []
