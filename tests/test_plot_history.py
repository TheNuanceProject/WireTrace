# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.plot_history: older plot values read back from the recording."""

from __future__ import annotations

import queue
import time
from datetime import datetime, timedelta

import numpy as np
import pytest

import core.plot_history as plot_history
from app.constants import PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS
from core.plot_history import (
    DetailRequest,
    DetailResult,
    HistoryBatch,
    HistoryReader,
    HistorySource,
)
from core.plot_parsers import RegexParser
from core.records import LineRecord, RecordOrigin, notice_record
from core.session_spool import SessionSpool

_ORIGIN = datetime(2026, 3, 4, 5, 6, 7)
_WAIT_SECONDS = 10.0


def _line(index: int, seconds: float, value: float) -> LineRecord:
    return LineRecord(_ORIGIN + timedelta(seconds=seconds), f"temp={value} n={index}", "DATA")


def _source(generation: int = 1, pattern: str = r"temp=(?P<temp>-?[\d.]+)") -> HistorySource:
    parser = RegexParser(pattern)
    return HistorySource(generation, _ORIGIN, tuple(parser.columns), parser)


@pytest.fixture
def spool(tmp_path):
    instance = SessionSpool(str(tmp_path), segment_bytes=4_000, max_bytes=10**9)
    instance.create()
    yield instance
    instance.delete()


@pytest.fixture
def reader(spool):
    delivered: queue.Queue = queue.Queue()
    instance = HistoryReader(lambda position, most: spool.snapshot_from(position, most), delivered.put)
    instance.delivered = delivered
    yield instance
    instance.stop()


def _next(reader: HistoryReader, kind: type) -> object:
    deadline = time.monotonic() + _WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            item = reader.delivered.get(timeout=0.05)
        except queue.Empty:
            continue
        if isinstance(item, kind):
            return item
    raise AssertionError(f"no {kind.__name__} in time")


def _follow_until(reader: HistoryReader, count: int) -> tuple[np.ndarray, np.ndarray]:
    xs: list[np.ndarray] = []
    ys: list[np.ndarray] = []
    while sum(x.size for x in xs) < count:
        batch = _next(reader, HistoryBatch)
        if "temp" in batch.samples:
            xs.append(batch.samples["temp"][0])
            ys.append(batch.samples["temp"][1])
    return np.concatenate(xs), np.concatenate(ys)


def test_following_reads_every_device_line_once_as_the_recording_grows(spool, reader):
    spool.append([_line(i, i * 0.01, i) for i in range(500)])
    reader.follow(_source())
    reader.set_reading(True)
    first_x, first_y = _follow_until(reader, 500)

    spool.append([notice_record("Break sent for 250 ms", _ORIGIN + timedelta(seconds=6))])
    spool.append([_line(i, i * 0.01, i) for i in range(500, 800)])
    later_x, later_y = _follow_until(reader, 300)

    assert list(np.concatenate([first_y, later_y])) == list(range(800))
    assert np.allclose(np.concatenate([first_x, later_x]), np.arange(800) * 0.01)


def test_nothing_is_read_while_reading_is_off(spool, reader):
    spool.append([_line(i, i, i) for i in range(10)])
    reader.follow(_source())

    time.sleep(0.3)
    assert reader.delivered.empty()

    reader.set_reading(True)
    assert _follow_until(reader, 10)[1].tolist() == list(range(10))


def test_detail_gives_every_sample_up_to_those_in_memory(spool, reader):
    spool.append([_line(i, i * 0.1, i) for i in range(100)])
    reader.follow(_source())
    reader.set_reading(True)
    _follow_until(reader, 100)

    reader.request_detail(DetailRequest(1, low=2.0, high=8.0, pixels=400, held={"temp": (6.0, 1)}))
    result = _next(reader, DetailResult)

    y = result.samples["temp"][1]
    assert y.tolist() == list(range(20, 60))
    assert result.every_sample and result.complete
    figures = result.figures["temp"]
    assert (figures.count, figures.minimum, figures.maximum) == (40, 20, 59)
    assert figures.total == sum(range(20, 60))
    assert (figures.last_x, figures.last_y) == pytest.approx((5.9, 59))


def test_samples_sharing_the_oldest_time_in_memory_are_split_exactly(spool, reader):
    same = _ORIGIN + timedelta(seconds=5)
    spool.append([_line(i, i, i) for i in range(5)])
    spool.append([LineRecord(same, f"temp={value}", "DATA") for value in (50, 51, 52, 53)])
    reader.follow(_source())
    reader.set_reading(True)
    _follow_until(reader, 9)

    # Two of the four lines at 5 s are still in memory.
    reader.request_detail(DetailRequest(1, low=0.0, high=10.0, pixels=400, held={"temp": (5.0, 2)}))
    result = _next(reader, DetailResult)

    assert result.samples["temp"][1].tolist() == [0, 1, 2, 3, 4, 50, 51]


def test_a_long_range_is_drawn_by_its_extremes_with_exact_figures(spool, reader):
    values = np.sin(np.arange(5_000) / 50.0)
    values[3_333] = 9.0
    spool.append([_line(i, i * 0.001, float(v)) for i, v in enumerate(values)])
    reader.follow(_source())
    reader.set_reading(True)
    _follow_until(reader, 5_000)

    reader.request_detail(DetailRequest(1, low=0.0, high=5.0, pixels=100))
    result = _next(reader, DetailResult)

    y = result.samples["temp"][1]
    assert not result.every_sample
    assert y.size <= 2 * 100
    assert 9.0 in y.tolist()
    figures = result.figures["temp"]
    assert figures.count == 5_000
    assert figures.maximum == 9.0
    assert figures.total == pytest.approx(float(values.sum()))


def test_a_range_no_longer_recorded_is_not_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(plot_history, "PLOT_HISTORY_POLL_MS", 10)
    spool = SessionSpool(str(tmp_path), segment_bytes=2_000, max_bytes=4_000)
    spool.create()
    delivered: queue.Queue = queue.Queue()
    reader = HistoryReader(lambda position, most: spool.snapshot_from(position, most), delivered.put)
    reader.delivered = delivered
    try:
        reader.follow(_source())
        reader.set_reading(True)
        for start in range(0, 600, 20):
            spool.append([_line(i, i * 0.1, i) for i in range(start, start + 20)])
            _follow_until(reader, 20)

        reader.request_detail(DetailRequest(1, low=0.0, high=60.0, pixels=400))
        result = _next(reader, DetailResult)

        assert not result.complete
        assert result.samples["temp"][1].tolist()[-1] == 599
    finally:
        reader.stop()
        spool.delete()


def test_the_index_never_outgrows_the_recording(tmp_path, monkeypatch):
    monkeypatch.setattr(plot_history, "PLOT_HISTORY_POLL_MS", 10)
    spool = SessionSpool(str(tmp_path), segment_bytes=2_000, max_bytes=4_000)
    spool.create()
    delivered: queue.Queue = queue.Queue()
    reader = HistoryReader(lambda position, most: spool.snapshot_from(position, most), delivered.put)
    reader.delivered = delivered
    try:
        reader.follow(_source())
        reader.set_reading(True)
        sizes = []
        for start in range(0, 1_200, 20):
            spool.append([_line(i, i * 0.1, i) for i in range(start, start + 20)])
            _follow_until(reader, 20)
            sizes.append(len(reader._index))
        held = spool.snapshot_from(None)
        oldest = held.oldest_segment
        held.release()

        assert all(entry.where.segment >= oldest for entry in reader._index[:-1])
        assert max(sizes[len(sizes) // 2:]) <= max(sizes[: len(sizes) // 2]) + 1

        reader.request_detail(DetailRequest(1, low=115.0, high=119.0, pixels=400))
        result = _next(reader, DetailResult)
        assert result.complete
        assert result.samples["temp"][1].tolist()[-1] == 1_190
    finally:
        reader.stop()
        spool.delete()


def test_a_pattern_that_keeps_running_out_of_time_stops_reading(spool, reader, monkeypatch):
    source = _source()
    monkeypatch.setattr(type(source.parser), "consecutive_timeouts",
                        property(lambda self: PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS))
    spool.append([_line(i, i, i) for i in range(10)])

    reader.follow(source)
    reader.set_reading(True)

    assert _next(reader, HistoryBatch).stalled


def test_a_new_source_starts_again_from_the_start(spool, reader):
    spool.append([LineRecord(_ORIGIN + timedelta(seconds=i), f"temp={i} rpm={i * 10}", "DATA")
                  for i in range(20)])
    reader.follow(_source())
    reader.set_reading(True)
    _follow_until(reader, 20)

    reader.follow(_source(generation=2, pattern=r"rpm=(?P<rpm>\d+)"))
    batch = _next(reader, HistoryBatch)
    while batch.generation != 2:
        batch = _next(reader, HistoryBatch)

    assert batch.samples["rpm"][1].tolist()[:3] == [0, 10, 20]


def test_lines_before_the_start_of_time_and_other_records_are_left_out(spool, reader):
    spool.append([
        _line(0, -1.0, 99),
        LineRecord(_ORIGIN, "temp=5", "DATA", origin=RecordOrigin.COMMAND),
        _line(1, 0.5, 7),
    ])
    reader.follow(_source())
    reader.set_reading(True)

    assert _follow_until(reader, 1)[1].tolist() == [7]
