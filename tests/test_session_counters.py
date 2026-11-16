# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.session_counters: the exact figures of a session."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.constants import SESSION_RESTARTS_KEPT
from core.records import LineRecord, RecordOrigin, command_record, notice_record
from core.session_counters import SessionCounters

_START = datetime(2026, 9, 1, 8, 0, 0)


def _line(tag: str) -> LineRecord:
    return LineRecord(_START, f"{tag} line", tag)


def test_nothing_before_the_first_session() -> None:
    assert SessionCounters().figures() is None


def test_device_lines_are_counted_by_level_and_nothing_else_is() -> None:
    counters = SessionCounters()
    counters.start("COM7", 115200, _START)

    received = counters.received([
        _line("ERROR"), _line("WARNING"), _line("WARNING"), _line("DATA"),
        notice_record("DTR set off", _START), command_record("status", _START),
    ])

    figures = counters.figures()
    assert received == 4
    assert figures.lines == 4
    assert figures.by_level == {"ERROR": 1, "WARNING": 2, "DATA": 1}


def test_device_lines_not_recorded_are_counted() -> None:
    counters = SessionCounters()
    counters.start("COM7", 115200, _START)

    counters.not_recorded([_line("DATA"), _line("INFO"), notice_record("x", _START)])

    assert counters.figures().not_recorded == 2


def test_restarts_are_all_counted_and_the_latest_kept() -> None:
    counters = SessionCounters()
    counters.start("COM7", 115200, _START)

    for index in range(SESSION_RESTARTS_KEPT + 5):
        assert counters.restarted(_START + timedelta(seconds=index), "POWERON") == index + 1

    figures = counters.figures()
    assert figures.restart_count == SESSION_RESTARTS_KEPT + 5
    assert len(figures.restarts) == SESSION_RESTARTS_KEPT
    assert figures.restarts[-1].at == _START + timedelta(seconds=SESSION_RESTARTS_KEPT + 4)


def test_the_end_is_kept_once_and_a_new_session_starts_afresh() -> None:
    counters = SessionCounters()
    counters.start("COM7", 115200, _START)
    counters.received([_line("DATA")])
    counters.end(_START + timedelta(hours=1), unexpected=True)
    counters.end(_START + timedelta(hours=2), unexpected=False)

    ended = counters.figures()
    assert (ended.ended, ended.ended_unexpectedly) == (_START + timedelta(hours=1), True)

    counters.start("COM9", 9600, _START + timedelta(hours=3))
    fresh = counters.figures()
    assert (fresh.port, fresh.baud, fresh.lines, fresh.ended) == ("COM9", 9600, 0, None)


def test_a_device_line_from_the_device_is_the_only_kind_counted() -> None:
    counters = SessionCounters()
    counters.start("COM7", 115200, _START)

    counters.received([LineRecord(_START, "x", "DATA", origin=RecordOrigin.NOTICE)])

    assert counters.figures().lines == 0
