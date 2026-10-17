# SPDX-License-Identifier: GPL-3.0-or-later
"""The line record that travels through the data pipeline.

A record is created once, in the reader, when a line has been received.
The console, the log files, the CSV export, the session spool, and the
live plot all consume the same record, so they always agree on a line's
text and on the moment it arrived.

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime

from app.constants import LOG_TIMESTAMP_FORMAT, TAG_COMMAND, TAG_NOTICE
from version import APP_NAME

#: Prefix of every line WireTrace itself adds to a stream or a log file,
#: so such lines can never be mistaken for device output.
NOTICE_PREFIX = f"[{APP_NAME}]"

#: Prefix of the echo of a command sent to the device.
COMMAND_PREFIX = ">>>"


class RecordOrigin(enum.Enum):
    """Where a record came from."""

    DEVICE = "device"    # a line received from the serial port
    COMMAND = "command"  # the echo of a command the user sent
    NOTICE = "notice"    # a line WireTrace added (skipped lines, pauses)


@dataclass(frozen=True, slots=True)
class LineRecord:
    """One line of a session, stamped at arrival.

    ``hex_row`` is True for a row of the HEX display (offset, bytes and
    characters) rather than a line of text.
    """

    timestamp: datetime
    text: str
    tag: str
    origin: RecordOrigin = RecordOrigin.DEVICE
    hex_row: bool = False


def command_record(command: str, timestamp: datetime) -> LineRecord:
    """Build the echo record for a command sent to the device."""
    return LineRecord(
        timestamp=timestamp,
        text=f"{COMMAND_PREFIX} {command}",
        tag=TAG_COMMAND,
        origin=RecordOrigin.COMMAND,
    )


def notice_record(message: str, timestamp: datetime) -> LineRecord:
    """Build a record for a line WireTrace adds to the stream."""
    return LineRecord(
        timestamp=timestamp,
        text=f"{NOTICE_PREFIX} {message}",
        tag=TAG_NOTICE,
        origin=RecordOrigin.NOTICE,
    )


# The most recent second formatted, as ``(second, text)``. Consecutive
# lines nearly always fall in the same second, and formatting the date
# costs several times more than everything else done per line. Kept as
# one tuple so threads that share it always read a matching pair.
_last_formatted_second: tuple[datetime | None, str] = (None, "")


def format_timestamp(moment: datetime) -> str:
    """Format a timestamp as written to logs: ``YYYY-MM-DD HH:MM:SS.mmm``."""
    global _last_formatted_second
    second = moment.replace(microsecond=0)
    cached_second, text = _last_formatted_second
    if second != cached_second:
        text = second.strftime(LOG_TIMESTAMP_FORMAT)
        _last_formatted_second = (second, text)
    return f"{text}.{moment.microsecond // 1000:03d}"
