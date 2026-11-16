# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.plot_export: plot values saved to a CSV file."""

from __future__ import annotations

import math
import os
import threading
from datetime import datetime, timedelta

import pytest

from app.constants import PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS
from core.plot_export import PlotCsvJob, PlotCsvRequest, PlotCsvResult
from core.plot_history import HistorySource
from core.plot_parsers import RegexParser
from core.records import LineRecord, RecordOrigin, notice_record
from core.session_spool import SessionSpool

_ORIGIN = datetime(2026, 7, 8, 9, 10, 11)


@pytest.fixture
def spool(tmp_path):
    instance = SessionSpool(str(tmp_path / "spool"))
    instance.create()
    instance.append([
        LineRecord(_ORIGIN, "temp=21.5 rpm=-40", "DATA"),
        LineRecord(_ORIGIN + timedelta(seconds=1), "temp=22", "DATA"),
        notice_record("DTR set off", _ORIGIN + timedelta(seconds=1.5)),
        LineRecord(_ORIGIN + timedelta(seconds=2), "boot done", "INFO"),
        LineRecord(_ORIGIN + timedelta(seconds=2.5), "temp=99", "DATA", origin=RecordOrigin.COMMAND),
        LineRecord(_ORIGIN + timedelta(seconds=3.25), "rpm=1e-05", "DATA"),
    ])
    yield instance
    instance.delete()


class _Parser:
    """Finds ``temp`` and ``rpm``; ``rpm`` is named as a formula would be."""

    def __init__(self) -> None:
        self._temp = RegexParser(r"temp=(?P<temp>-?[\d.e-]+)")
        self._rpm = RegexParser(r"rpm=(?P<rpm>-?[\d.e-]+)")

    def extract(self, line: str) -> dict[str, float] | None:
        found = {**(self._temp.extract(line) or {})}
        if (rpm := self._rpm.extract(line)) is not None:
            found["=rpm"] = rpm["rpm"]
        return found or None


def _save(spool: SessionSpool, path: str, low: float = 0.0, high: float = math.inf) -> PlotCsvResult:
    done = threading.Event()
    results: list[PlotCsvResult] = []

    def finished(result: PlotCsvResult) -> None:
        results.append(result)
        done.set()

    request = PlotCsvRequest(
        path=path, name="Bench", port_name="COM7", baud_rate=115200,
        low=low, high=high, line_ending="\n",
        source=HistorySource(1, _ORIGIN, ("temp", "=rpm"), _Parser()),
    )
    job = PlotCsvJob(spool.snapshot_from(None), request, finished)
    job.start()
    assert done.wait(10)
    job.join()
    return results[0]


def _rows(path: str) -> list[str]:
    with open(path, encoding="utf-8") as file:
        return [line.rstrip("\n") for line in file if not line.startswith("#")]


def test_every_device_line_with_a_value_becomes_a_row(spool, tmp_path):
    path = str(tmp_path / "out" / "plot.csv")

    result = _save(spool, path)

    assert result.error == "" and result.rows == 3
    assert _rows(path) == [
        "Timestamp,Seconds,temp,'=rpm",
        "2026-07-08 09:10:11.000,0.000000,21.5,-40.0",
        "2026-07-08 09:10:12.000,1.000000,22.0,",
        "2026-07-08 09:10:14.250,3.250000,,1e-05",
    ]


def test_only_the_time_asked_for_is_saved(spool, tmp_path):
    path = str(tmp_path / "plot.csv")

    result = _save(spool, path, low=0.5, high=3.0)

    assert result.rows == 1
    assert _rows(path)[1:] == ["2026-07-08 09:10:12.000,1.000000,22.0,"]


def test_the_header_names_the_session(spool, tmp_path):
    path = str(tmp_path / "plot.csv")

    _save(spool, path)

    with open(path, encoding="utf-8") as file:
        comments = [line for line in file if line.startswith("#")]
    assert "Plot Values" in comments[0]
    assert "Plot: Bench | Port: COM7 | Baud: 115200" in comments[1]


def test_a_failed_save_leaves_no_file(spool, tmp_path):
    path = tmp_path / "taken"
    path.mkdir()

    result = _save(spool, str(path))

    assert result.error
    assert result.rows == 0
    assert path.is_dir()
    assert not os.path.exists(str(path) + ".partial")


def _job(spool: SessionSpool, path: str, parser: object, finished) -> PlotCsvJob:
    request = PlotCsvRequest(
        path=path, name="Bench", port_name="COM7", baud_rate=115200,
        low=0.0, high=math.inf, line_ending="\n",
        source=HistorySource(1, _ORIGIN, ("v",), parser),
    )
    return PlotCsvJob(spool.snapshot_from(None), request, finished)


def test_a_cancelled_save_stops_and_leaves_no_file(spool, tmp_path):
    path = str(tmp_path / "plot.csv")
    reached, release = threading.Event(), threading.Event()
    results: list[PlotCsvResult] = []

    class _Waiting:
        def extract(self, line: str) -> dict[str, float] | None:
            reached.set()
            release.wait(10)
            return {"v": 1.0}

    job = _job(spool, path, _Waiting(), results.append)
    job.start()
    assert reached.wait(10)
    job.cancel()
    release.set()
    job.join()

    assert results == [PlotCsvResult(path, 0, "", cancelled=True)]
    assert not os.path.exists(path)
    assert not os.path.exists(path + ".partial")


def test_a_pattern_that_takes_too_long_stops_the_save(tmp_path):
    many = SessionSpool(str(tmp_path / "spool"))
    many.create()
    many.append([
        LineRecord(_ORIGIN + timedelta(seconds=index), "a" * 40 + "b", "DATA")
        for index in range(PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS * 4)
    ])
    path = str(tmp_path / "plot.csv")
    results: list[PlotCsvResult] = []
    parser = RegexParser(r"(?P<v>(a|aa)+)c")

    job = _job(many, path, parser, results.append)
    job.start()
    job.join()
    many.delete()

    assert parser.consecutive_timeouts == PLOT_REGEX_MAX_CONSECUTIVE_TIMEOUTS
    assert results[0].error.startswith("The pattern takes too long")
    assert not os.path.exists(path)
