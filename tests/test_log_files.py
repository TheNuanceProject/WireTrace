# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.log_files: exact writes, failure handling, splitting."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.constants import LOG_PENDING_MAX_BYTES
from core.log_files import SplitLogFile
from tests.file_backends import FaultyBackend


def _header(part_number: int) -> bytes:
    return f"# part {part_number}\n".encode()


def _loss_marker(count: int) -> bytes:
    return f"# lost {count}\n".encode()


def _open(path: Path, **options) -> SplitLogFile:
    log = SplitLogFile(str(path), _header, _loss_marker, **options)
    log.open()
    return log


class TestWriting:
    def test_header_and_lines_are_written_in_order(self, tmp_path):
        path = tmp_path / "log.txt"
        log = _open(path)
        log.append(b"one\n")
        log.append(b"two\n")
        log.close()
        assert path.read_bytes() == b"# part 1\none\ntwo\n"
        assert log.units_written == 2
        assert log.units_lost == 0
        assert log.paths == [str(path)]

    def test_empty_log_still_has_its_header(self, tmp_path):
        path = tmp_path / "log.txt"
        _open(path).close()
        assert path.read_bytes() == b"# part 1\n"

    def test_lazy_header_is_written_with_the_first_chunk(self, tmp_path):
        path = tmp_path / "log.csv"
        log = _open(path, eager_header=False)
        assert path.read_bytes() == b""
        log.append(b"row\n")
        log.flush()
        assert path.read_bytes() == b"# part 1\nrow\n"
        log.close()

    def test_lazy_header_is_written_at_close_when_no_chunk_arrived(self, tmp_path):
        path = tmp_path / "log.csv"
        _open(path, eager_header=False).close()
        assert path.read_bytes() == b"# part 1\n"

    def test_short_writes_by_the_operating_system_are_completed(self, tmp_path):
        backend = FaultyBackend()
        backend.max_bytes_per_write = 3
        path = tmp_path / "log.txt"
        log = _open(path, backend=backend)
        log.append(b"a long line of text\n")
        log.close()
        assert path.read_bytes() == b"# part 1\na long line of text\n"

    def test_many_lines_are_handed_over_in_one_write(self, tmp_path):
        backend = FaultyBackend()
        path = tmp_path / "log.txt"
        log = _open(path, backend=backend)
        calls_after_header = backend.write_calls
        lines = [f"line {number}\n".encode() for number in range(1_000)]
        for line in lines:
            log.append(line)
        log.flush()
        assert backend.write_calls == calls_after_header + 1
        assert log.units_written == 1_000
        log.close()
        assert path.read_bytes() == b"# part 1\n" + b"".join(lines)

    def test_close_syncs_the_file_to_the_storage_device(self, tmp_path):
        backend = FaultyBackend()
        log = _open(tmp_path / "log.txt", backend=backend)
        log.append(b"line\n")
        syncs_before_close = backend.sync_calls
        log.close()
        assert backend.sync_calls == syncs_before_close + 1

    def test_each_finished_part_is_synced(self, tmp_path):
        backend = FaultyBackend()
        log = _open(tmp_path / "log.txt", part_limit_bytes=14, backend=backend)
        for number in range(3):
            log.append(f"line{number}\n".encode())
        log.flush()
        assert len(log.paths) == 3
        assert backend.sync_calls == 2
        log.close()

    def test_close_is_idempotent(self, tmp_path):
        log = _open(tmp_path / "log.txt")
        log.close()
        log.close()


class TestNeverOverwrites:
    def test_existing_file_is_kept_and_a_numbered_name_is_used(self, tmp_path):
        path = tmp_path / "log.txt"
        path.write_bytes(b"precious")
        log = _open(path)
        log.append(b"new\n")
        log.close()
        assert path.read_bytes() == b"precious"
        assert log.paths == [str(tmp_path / "log_2.txt")]
        assert (tmp_path / "log_2.txt").read_bytes() == b"# part 1\nnew\n"

    def test_later_parts_follow_the_renamed_first_part(self, tmp_path):
        (tmp_path / "log.txt").write_bytes(b"precious")
        log = _open(tmp_path / "log.txt", part_limit_bytes=14)
        for index in range(3):
            log.append(f"line{index}\n".encode())
        log.close()
        assert [Path(p).name for p in log.paths] == [
            "log_2.txt", "log_2_002.txt", "log_2_003.txt",
        ]


class TestSplitting:
    def test_parts_are_cut_between_lines_at_the_size_limit(self, tmp_path):
        log = _open(tmp_path / "log.txt", part_limit_bytes=21)
        for index in range(6):
            log.append(f"line{index}\n".encode())
        log.close()
        names = [Path(p).name for p in log.paths]
        assert names == ["log.txt", "log_002.txt", "log_003.txt"]
        contents = [Path(p).read_bytes() for p in log.paths]
        assert contents[0] == b"# part 1\nline0\nline1\n"
        assert contents[1] == b"# part 2\nline2\nline3\n"
        assert contents[2] == b"# part 3\nline4\nline5\n"

    def test_parts_are_cut_at_the_unit_limit(self, tmp_path):
        log = _open(tmp_path / "log.csv", part_limit_units=2, eager_header=False)
        for index in range(5):
            log.append(f"r{index}\n".encode())
        log.close()
        contents = [Path(p).read_bytes() for p in log.paths]
        assert contents == [
            b"# part 1\nr0\nr1\n", b"# part 2\nr2\nr3\n", b"# part 3\nr4\n",
        ]

    def test_chunk_larger_than_the_limit_gets_a_part_of_its_own(self, tmp_path):
        log = _open(tmp_path / "log.txt", part_limit_bytes=10)
        log.append(b"x" * 50 + b"\n")
        log.append(b"y\n")
        log.close()
        contents = [Path(p).read_bytes() for p in log.paths]
        assert contents == [b"# part 1\n" + b"x" * 50 + b"\n", b"# part 2\ny\n"]

    def test_no_limit_means_one_part(self, tmp_path):
        log = _open(tmp_path / "log.txt")
        for _ in range(1000):
            log.append(b"x" * 100 + b"\n")
        log.close()
        assert len(log.paths) == 1

    def test_every_line_is_in_exactly_one_part(self, tmp_path):
        log = _open(tmp_path / "log.txt", part_limit_bytes=200)
        expected = [f"line number {index}\n".encode() for index in range(500)]
        for line in expected:
            log.append(line)
            log.flush()
        log.close()
        found: list[bytes] = []
        for number, part in enumerate(log.paths, start=1):
            lines = Path(part).read_bytes().splitlines(keepends=True)
            assert lines[0] == _header(number)
            found.extend(lines[1:])
        assert found == expected


class TestWriteFailure:
    def test_failed_write_keeps_the_remainder_and_resumes_without_gap(self, tmp_path):
        backend = FaultyBackend()
        path = tmp_path / "log.txt"
        log = _open(path, backend=backend)
        log.append(b"first line\n")
        log.append(b"second line\n")
        backend.accept_before_failing = 5
        with pytest.raises(OSError, match="No space"):
            log.flush()
        assert log.is_failing
        assert path.read_bytes() == b"# part 1\nfirst"

        backend.accept_before_failing = None
        log.append(b"third line\n")
        log.flush()
        assert not log.is_failing
        log.close()
        assert path.read_bytes() == b"# part 1\nfirst line\nsecond line\nthird line\n"
        assert log.units_written == 3
        assert log.units_lost == 0

    def test_failure_inside_a_batch_counts_exactly_the_whole_lines_written(
        self, tmp_path,
    ):
        backend = FaultyBackend()
        path = tmp_path / "log.txt"
        log = _open(path, backend=backend)
        for text in (b"aaaa\n", b"bbbb\n", b"cccc\n", b"dddd\n"):
            log.append(text)
        backend.accept_before_failing = 12  # two lines and part of the third
        with pytest.raises(OSError, match="No space"):
            log.flush()
        assert log.units_written == 2
        assert path.read_bytes() == b"# part 1\naaaa\nbbbb\ncc"

        backend.accept_before_failing = None
        log.close()
        assert path.read_bytes() == b"# part 1\naaaa\nbbbb\ncccc\ndddd\n"
        assert log.units_written == 4
        assert log.units_lost == 0

    def test_lines_beyond_the_pending_budget_are_counted_and_marked(self, tmp_path):
        backend = FaultyBackend()
        path = tmp_path / "log.txt"
        log = _open(path, backend=backend)
        backend.fail_writes = True
        big = b"z" * (LOG_PENDING_MAX_BYTES // 2) + b"\n"
        assert log.append(big)
        with pytest.raises(OSError):
            log.flush()
        assert log.append(b"z" * (LOG_PENDING_MAX_BYTES // 2 - 3) + b"\n")
        assert not log.append(b"dropped one\n")
        assert not log.append(b"dropped two\n")
        assert log.units_lost == 2

        backend.fail_writes = False
        log.flush()
        assert log.append(b"after recovery\n")
        log.close()
        tail = path.read_bytes()[-40:]
        assert tail.endswith(b"# lost 2\nafter recovery\n")
        assert log.units_written == 3
        assert log.units_lost == 2

    def test_loss_with_no_line_after_it_is_marked_when_the_file_closes(self, tmp_path):
        backend = FaultyBackend()
        path = tmp_path / "log.txt"
        log = _open(path, backend=backend)
        backend.fail_writes = True
        assert log.append(b"z" * LOG_PENDING_MAX_BYTES + b"\n")
        with pytest.raises(OSError):
            log.flush()
        assert not log.append(b"dropped one\n")
        assert not log.append(b"dropped two\n")

        backend.fail_writes = False
        log.close()
        assert path.read_bytes().endswith(b"z\n# lost 2\n")
        assert log.units_written == 1
        assert log.units_lost == 2

    def test_part_that_reports_an_error_on_close_does_not_stop_the_log(self, tmp_path):
        backend = FaultyBackend()
        log = _open(tmp_path / "log.txt", part_limit_bytes=14, backend=backend)
        log.append(b"line0\n")
        log.flush()
        backend.fail_closes = True
        log.append(b"line1\n")
        log.append(b"line2\n")
        log.flush()
        backend.fail_closes = False
        log.close()
        contents = [Path(p).read_bytes() for p in log.paths]
        assert contents == [
            b"# part 1\nline0\n", b"# part 2\nline1\n", b"# part 3\nline2\n",
        ]
        assert log.units_written == 3

    def test_failure_to_start_a_new_part_is_retried(self, tmp_path):
        backend = FaultyBackend()
        log = _open(tmp_path / "log.txt", part_limit_bytes=14, backend=backend)
        log.append(b"line0\n")
        log.flush()
        backend.fail_creates = True
        log.append(b"line1\n")
        with pytest.raises(OSError):
            log.flush()
        backend.fail_creates = False
        log.close()
        contents = [Path(p).read_bytes() for p in log.paths]
        assert contents == [b"# part 1\nline0\n", b"# part 2\nline1\n"]

    def test_close_while_failing_counts_what_was_not_written(self, tmp_path):
        backend = FaultyBackend()
        log = _open(tmp_path / "log.txt", backend=backend)
        log.append(b"written\n")
        log.flush()
        backend.fail_writes = True
        log.append(b"never written 1\n")
        log.append(b"never written 2\n")
        with pytest.raises(OSError):
            log.close()
        assert log.units_written == 1
        assert log.units_lost == 2
        log.close()

    def test_open_failure_is_raised(self, tmp_path):
        backend = FaultyBackend()
        backend.fail_creates = True
        log = SplitLogFile(str(tmp_path / "log.txt"), _header, _loss_marker, backend=backend)
        with pytest.raises(OSError):
            log.open()

    def test_missing_directory_is_reported(self, tmp_path):
        log = SplitLogFile(str(tmp_path / "nowhere" / "log.txt"), _header, _loss_marker)
        with pytest.raises(OSError):
            log.open()
