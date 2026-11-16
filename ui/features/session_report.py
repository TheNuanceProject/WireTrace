# SPDX-License-Identifier: GPL-3.0-or-later
"""The end-of-test report of one session, as plain text.

It only formats the exact figures the reader counted
(``core/session_counters.py``): how long the board was connected and
how the connection ended, its restarts, the lines received, recorded
and not, and the errors and warnings among them.
"""

from __future__ import annotations

from datetime import datetime

from app.constants import (
    ERROR_LEVELS,
    LEVEL_NAMES,
    LOG_TIMESTAMP_FORMAT,
    SESSION_DISCONNECTS_KEPT,
    SESSION_RESTARTS_KEPT,
    WARNING_LEVELS,
)
from core.records import format_timestamp
from core.session_counters import SessionFigures
from ui.formatting import format_duration, format_time_of_day
from version import APP_NAME

TITLE = "Session report"
_INDENT = "  "


def session_report(
    figures: SessionFigures,
    now: datetime,
    *,
    title: str,
    framing: str,
    restarts_counted: bool,
) -> str:
    """The report of ``figures`` as of ``now``, one fact to a line."""
    lines = [
        f"{APP_NAME} {TITLE.lower()}: {title}",
        "",
        f"Port: {figures.port} at {figures.baud:,} baud, {framing}",
        f"Connected: {figures.started.strftime(LOG_TIMESTAMP_FORMAT)}",
    ]
    if figures.ended is None:
        lines.append(f"Still connected at: {now.strftime(LOG_TIMESTAMP_FORMAT)}")
        lines.append(f"Running time: {format_duration((now - figures.started).total_seconds())}")
    else:
        how = "the connection was lost" if figures.ended_unexpectedly else "closed"
        lines.append(f"Disconnected: {figures.ended.strftime(LOG_TIMESTAMP_FORMAT)}, {how}")
        lines.append(
            f"Running time: {format_duration((figures.ended - figures.started).total_seconds())}"
        )

    errors = sum(figures.by_level.get(level, 0) for level in ERROR_LEVELS)
    warnings = sum(figures.by_level.get(level, 0) for level in WARNING_LEVELS)
    lines += [
        "",
        f"Lines received: {figures.lines:,}",
        f"Errors: {errors:,}",
        f"Warnings: {warnings:,}",
        f"Lines not recorded (the disk could not keep up): {figures.not_recorded:,}",
    ]
    by_level = [
        f"{name} {figures.by_level[level]:,}"
        for level, name in LEVEL_NAMES.items() if figures.by_level.get(level)
    ]
    if by_level:
        lines.append(f"By level: {', '.join(by_level)}")

    lines.append("")
    lines.append(f"Times the connection was lost: {figures.disconnect_count:,}")
    if figures.disconnect_count:
        lines.append(f"Time the board was away: {format_duration(figures.seconds_away)}")
        if figures.disconnect_count > len(figures.disconnects):
            lines.append(f"{_INDENT}The latest {SESSION_DISCONNECTS_KEPT:,} are listed.")
        for disconnect in figures.disconnects:
            lost = format_timestamp(disconnect.lost)
            if disconnect.back is None:
                lines.append(f"{_INDENT}{lost}  not back")
            else:
                away = (disconnect.back - disconnect.lost).total_seconds()
                lines.append(
                    f"{_INDENT}{lost}  back at {format_time_of_day(disconnect.back)}, "
                    f"after {away:.3f} s"
                )

    lines.append("")
    if not restarts_counted:
        lines.append("Restarts: not counted (Restart detection is switched off)")
    else:
        lines.append(f"Restarts of the board: {figures.restart_count:,}")
        if figures.restart_count > len(figures.restarts):
            lines.append(f"{_INDENT}The latest {SESSION_RESTARTS_KEPT:,} are listed.")
        lines += [
            f"{_INDENT}{format_timestamp(restart.at)}"
            f"  {restart.reason}"
            for restart in figures.restarts
        ]
    return "\n".join(lines) + "\n"

