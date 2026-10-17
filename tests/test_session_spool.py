# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.session_spool: round trip, segments, cap, marks, stale cleanup."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.constants import SPOOL_STALE_MIN_AGE_S
from core.records import LineRecord, RecordOrigin, command_record, notice_record
from core.session_spool import SessionSpool

_START = datetime(2026, 1, 2, 3, 4, 5, 123456)


def _records(count: int, first: int = 0) -> list[LineRecord]:
    return [
        LineRecord(_START + timedelta(milliseconds=index), f"line {index}", "DATA")
        for index in range(first, first + count)
    ]


@pytest.fixture
def spool(tmp_path):
    instance = SessionSpool(str(tmp_path))
    instance.create()
    yield instance
    instance.delete()


def _read(instance: SessionSpool) -> list[LineRecord]:
    snapshot = instance.snapshot()
    try:
        return list(snapshot.records())
    finally:
        snapshot.release()


class TestRoundTrip:
    def test_records_come_back_identical_and_in_order(self, spool):
        written = _records(250)
        spool.append(written[:100])
        spool.append(written[100:])
        assert _read(spool) == written

    def test_every_origin_and_awkward_text_survive(self, spool):
        written = [
            LineRecord(_START, "tabs\tinside\tthe text", "INFO"),
            LineRecord(_START, "", "DATA"),
            LineRecord(_START, "unicode °C µs", "DATA"),
            LineRecord(_START.replace(microsecond=0), "whole second", "DATA"),
            LineRecord(_START, "00000000  41 7C  |A||", "DATA", hex_row=True),
            command_record("set rpm=1200", _START),
            notice_record("3 lines skipped", _START),
        ]
        spool.append(written)
        read = _read(spool)
        assert read == written
        assert [record.origin for record in read[-2:]] == [
            RecordOrigin.COMMAND, RecordOrigin.NOTICE,
        ]

    def test_empty_spool_has_no_records(self, spool):
        assert _read(spool) == []

    def test_snapshot_is_fixed_at_the_moment_it_is_taken(self, spool):
        spool.append(_records(10))
        snapshot = spool.snapshot()
        spool.append(_records(10, first=10))
        assert len(list(snapshot.records())) == 10
        snapshot.release()


class TestSegments:
    def test_data_is_spread_over_segments_and_read_back_whole(self, tmp_path):
        spool = SessionSpool(str(tmp_path), segment_bytes=2_000, max_bytes=10**9)
        spool.create()
        written = _records(600)
        for start in range(0, 600, 20):
            spool.append(written[start:start + 20])
        segments = [n for n in os.listdir(spool.directory) if n.endswith(".spool")]
        assert len(segments) > 3
        assert _read(spool) == written
        spool.delete()

    def test_oldest_segments_are_discarded_beyond_the_cap(self, tmp_path):
        spool = SessionSpool(str(tmp_path), segment_bytes=2_000, max_bytes=6_000)
        spool.create()
        written = _records(2_000)
        for start in range(0, 2_000, 20):
            spool.append(written[start:start + 20])

        snapshot = spool.snapshot()
        kept = list(snapshot.records())
        snapshot.release()
        assert snapshot.truncated
        assert 0 < len(kept) < len(written)
        assert kept == written[-len(kept):]

        size = sum(
            os.path.getsize(os.path.join(spool.directory, name))
            for name in os.listdir(spool.directory)
        )
        assert size <= 6_000 + 2 * 2_000
        spool.delete()

    def test_segments_are_kept_while_a_snapshot_is_held(self, tmp_path):
        spool = SessionSpool(str(tmp_path), segment_bytes=2_000, max_bytes=4_000)
        spool.create()
        first = _records(40)
        spool.append(first)
        snapshot = spool.snapshot()
        for start in range(40, 2_000, 20):
            spool.append(_records(20, first=start))
        assert list(snapshot.records()) == first
        snapshot.release()
        snapshot.release()
        spool.delete()


class TestMark:
    def test_export_starts_at_the_mark(self, spool):
        spool.append(_records(30))
        spool.mark()
        after = _records(12, first=30)
        spool.append(after)
        snapshot = spool.snapshot()
        assert list(snapshot.records()) == after
        assert not snapshot.truncated
        snapshot.release()

    def test_snapshot_knows_whether_it_holds_anything(self, spool):
        empty = spool.snapshot()
        assert empty.is_empty
        empty.release()
        spool.append(_records(1))
        holding = spool.snapshot()
        assert not holding.is_empty
        holding.release()

    def test_mark_at_the_end_gives_an_empty_snapshot(self, spool):
        spool.append(_records(5))
        spool.mark()
        assert _read(spool) == []


class TestLifecycle:
    def test_append_after_close_fails(self, spool):
        spool.close()
        with pytest.raises(OSError):
            spool.append(_records(1))

    def test_closed_spool_is_still_readable(self, spool):
        written = _records(5)
        spool.append(written)
        spool.close()
        assert _read(spool) == written

    def test_delete_waits_for_a_snapshot_that_is_still_being_read(self, tmp_path):
        instance = SessionSpool(str(tmp_path))
        instance.create()
        records = _records(5)
        instance.append(records)
        snapshot = instance.snapshot()

        instance.delete()
        assert list(snapshot.records()) == records
        assert os.path.isdir(instance.directory)

        snapshot.release()
        assert not os.path.exists(instance.directory)

    def test_delete_removes_the_directory(self, tmp_path):
        spool = SessionSpool(str(tmp_path))
        spool.create()
        spool.append(_records(3))
        directory = spool.directory
        spool.delete()
        assert not os.path.exists(directory)

    def test_create_fails_when_the_root_cannot_be_made(self, tmp_path):
        blocker = tmp_path / "file"
        blocker.write_text("x")
        with pytest.raises(OSError):
            SessionSpool(str(blocker / "sub")).create()


def _abandoned_spool(directory: Path) -> Path:
    """Create what a crashed session leaves behind: files, and no lock held."""
    directory.mkdir()
    (directory / "lock").write_bytes(b"")
    (directory / "segment-000001.spool").write_bytes(b"leftover")
    return directory


def _make_old(directory) -> None:
    long_ago = time.time() - 10 * SPOOL_STALE_MIN_AGE_S
    os.utime(directory, (long_ago, long_ago))


@pytest.mark.skipif(os.name == "nt", reason="Windows has no owner-only file mode")
class TestPrivacy:
    def test_the_folder_and_its_files_are_for_their_owner_only(self, tmp_path):
        previous = os.umask(0)
        instance = SessionSpool(str(tmp_path), segment_bytes=2_000)
        try:
            instance.create()
            for first in range(0, 300, 50):
                instance.append(_records(50, first=first))
            (folder,) = tmp_path.iterdir()
            segments = sorted(folder.glob("*.spool"))

            assert len(segments) > 1
            assert folder.stat().st_mode & 0o777 == 0o700
            assert {segment.stat().st_mode & 0o777 for segment in segments} == {0o600}
        finally:
            os.umask(previous)
            instance.delete()


class TestStaleCleanup:
    def test_live_spool_is_left_alone_and_abandoned_one_is_removed(self, tmp_path):
        live = SessionSpool(str(tmp_path))
        live.create()
        live.append(_records(3))

        abandoned = _abandoned_spool(tmp_path / "abandoned")
        # Both are old enough to be candidates; only the lock tells
        # them apart.
        _make_old(live.directory)
        _make_old(abandoned)

        SessionSpool.remove_stale(str(tmp_path))

        assert os.path.isdir(live.directory)
        assert not abandoned.exists()
        assert len(_read(live)) == 3
        live.delete()

    def test_directory_made_a_moment_ago_is_not_touched(self, tmp_path):
        # Another WireTrace starting at this moment has made its
        # directory and lock file and not yet taken the lock.
        starting = _abandoned_spool(tmp_path / "starting")
        SessionSpool.remove_stale(str(tmp_path))
        assert (starting / "segment-000001.spool").exists()

    def test_old_directory_that_is_not_a_spool_is_not_touched(self, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        (other / "keep.txt").write_text("keep")
        _make_old(other)
        SessionSpool.remove_stale(str(tmp_path))
        assert (other / "keep.txt").exists()

    def test_missing_root_is_ignored(self, tmp_path):
        SessionSpool.remove_stale(str(tmp_path / "does-not-exist"))

    def test_stray_files_in_the_root_are_ignored(self, tmp_path):
        (tmp_path / "note.txt").write_text("keep")
        SessionSpool.remove_stale(str(tmp_path))
        assert (tmp_path / "note.txt").exists()
