# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.records: timestamp formatting and record builders."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.constants import TAG_COMMAND, TAG_NOTICE
from core.records import (
    RecordOrigin,
    command_record,
    format_timestamp,
    notice_record,
)

_MOMENT = datetime(2026, 3, 9, 7, 5, 3, 42_999)


class TestFormatTimestamp:
    def test_format_is_date_time_and_milliseconds(self):
        assert format_timestamp(_MOMENT) == "2026-03-09 07:05:03.042"

    def test_milliseconds_are_truncated_not_rounded(self):
        assert format_timestamp(_MOMENT.replace(microsecond=999_999)).endswith(".999")

    def test_moments_in_different_seconds_format_independently(self):
        moments = [
            _MOMENT,
            _MOMENT + timedelta(seconds=1),
            _MOMENT,
            _MOMENT + timedelta(days=1),
            _MOMENT - timedelta(hours=8),
        ]
        assert [format_timestamp(moment) for moment in moments] == [
            "2026-03-09 07:05:03.042",
            "2026-03-09 07:05:04.042",
            "2026-03-09 07:05:03.042",
            "2026-03-10 07:05:03.042",
            "2026-03-08 23:05:03.042",
        ]


class TestRecordBuilders:
    def test_command_record_is_marked_as_a_command(self):
        record = command_record("AT", _MOMENT)
        assert (record.text, record.tag, record.origin) == (
            ">>> AT", TAG_COMMAND, RecordOrigin.COMMAND,
        )

    def test_notice_record_carries_the_application_prefix(self):
        record = notice_record("Logging paused", _MOMENT)
        assert (record.text, record.tag, record.origin) == (
            "[WireTrace] Logging paused", TAG_NOTICE, RecordOrigin.NOTICE,
        )
