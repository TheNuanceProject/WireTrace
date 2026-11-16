# SPDX-License-Identifier: GPL-3.0-or-later
"""Saves the values of a plot to a CSV file.

The values are read from the session recording, not from what is drawn,
so a file holds every sample of the time asked for, exactly as found in
the lines, even where the plot draws only the least and greatest. The
lines are read with a parser of the job's own that finds the values the
way the plot does now.

One row for each device line that holds a value: the time it was
received (as the log writes it), its time on the plot in seconds, and
each value, empty where the line did not hold it. Every cell goes
through the same guard as the CSV log (``core.csv_engine.csv_cell``),
so no cell can be read as a formula by a spreadsheet.

The file is created as an export is, written beside its final name and
moved into place when complete, so a failed or cancelled export leaves
nothing behind. The work runs on a thread of its own. It stops when it
is cancelled, and when the pattern takes too long on the lines, as the
plot does.

This module contains no Qt code and does not touch the GUI or serial I/O.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.constants import LOG_FLUSH_THRESHOLD_BYTES, PLOT_MAX_LINE_CHARACTERS
from core.csv_engine import TIMESTAMP_COLUMN, csv_cell
from core.log_files import OsFileBackend
from core.log_format import FileHeaderInfo, csv_header, encode_csv_line
from core.plot_history import HistorySource, pattern_stalled
from core.records import RecordOrigin, format_timestamp
from core.session_spool import SpoolSnapshot

logger = logging.getLogger(__name__)

_TITLE = "Plot Values"
_KIND = "Plot"
_TIME_LABEL = "Exported"
_SECONDS_COLUMN = "Seconds"
_SECONDS_FORMAT = "{:.6f}"
_PARTIAL_SUFFIX = ".partial"
_TRUNCATED_NOTE = (
    "The start of this session is no longer available; "
    "the values begin with the oldest data still held."
)
_THREAD_NAME = "wiretrace-plot-export"
_STALLED = (
    "The pattern takes too long on the recorded lines, so the values were not saved. "
    "Change the pattern in Configure plot."
)


class _Cancelled(Exception):
    """The job was cancelled."""


class _Stalled(Exception):
    """The pattern ran out of time on too many lines in a row."""


@dataclass(frozen=True)
class PlotCsvRequest:
    """Which values to save, from when to when, and where.

    ``low`` and ``high`` are times on the plot, in seconds; every value
    of the session is ``0`` to infinity.
    """

    path: str
    name: str
    port_name: str
    baud_rate: int
    low: float
    high: float
    line_ending: str
    source: HistorySource


@dataclass(frozen=True)
class PlotCsvResult:
    """The outcome of saving plot values.

    ``error`` is empty on success and when the job was cancelled.
    """

    path: str
    rows: int
    error: str
    cancelled: bool = False


class PlotCsvJob:
    """Writes the plot values of one snapshot to a CSV file on a thread.

    Args:
        snapshot: The session recording. Released when the job ends.
        request: What to save and where.
        on_finished: Called on the job's thread with the result.
    """

    def __init__(
        self,
        snapshot: SpoolSnapshot,
        request: PlotCsvRequest,
        on_finished: Callable[[PlotCsvResult], None],
    ) -> None:
        self._snapshot = snapshot
        self._request = request
        self._on_finished = on_finished
        self._cancelled = threading.Event()
        self._thread = threading.Thread(target=self._run, name=_THREAD_NAME, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        """Ask the job to stop. The file it was writing is removed."""
        self._cancelled.set()

    def join(self) -> None:
        """Wait for the job's thread to end."""
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        request = self._request
        partial = request.path + _PARTIAL_SUFFIX
        try:
            rows = self._write(partial)
            os.replace(partial, request.path)
            result = PlotCsvResult(request.path, rows, "")
        except _Cancelled:
            with contextlib.suppress(OSError):
                os.remove(partial)
            result = PlotCsvResult(request.path, 0, "", cancelled=True)
        except _Stalled:
            logger.warning("Saving plot values stopped: the pattern takes too long")
            with contextlib.suppress(OSError):
                os.remove(partial)
            result = PlotCsvResult(request.path, 0, _STALLED)
        except Exception as exc:
            if isinstance(exc, OSError):
                logger.error("Saving plot values failed: %s", exc)
            else:
                logger.exception("Saving plot values failed unexpectedly")
            with contextlib.suppress(OSError):
                os.remove(partial)
            result = PlotCsvResult(request.path, 0, str(exc) or type(exc).__name__)
        finally:
            self._snapshot.release()
        self._on_finished(result)

    def _write(self, path: str) -> int:
        request = self._request
        source = request.source
        info = FileHeaderInfo(
            title=_TITLE,
            kind=_KIND,
            name=request.name,
            time_label=_TIME_LABEL,
            time=datetime.now(),
            port_name=request.port_name,
            baud_rate=request.baud_rate,
            note=_TRUNCATED_NOTE if self._snapshot.truncated and request.low <= 0 else "",
        )
        columns = source.columns
        header = ",".join([TIMESTAMP_COLUMN, _SECONDS_COLUMN, *(csv_cell(name) for name in columns)])
        extract = source.parser.extract
        rows = 0
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # Left over from an export that was cut short, such as by a power cut.
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)
        with os.fdopen(OsFileBackend().create(path), "wb") as output:
            output.write(csv_header(info, 1, header, request.line_ending))
            pending: list[bytes] = []
            pending_bytes = 0
            for record in self._snapshot.records():
                if self._cancelled.is_set():
                    raise _Cancelled
                if record.origin is not RecordOrigin.DEVICE:
                    continue
                at = (record.timestamp - source.origin).total_seconds()
                if not request.low <= at <= request.high:
                    continue
                values = extract(record.text[:PLOT_MAX_LINE_CHARACTERS])
                if pattern_stalled(source.parser):
                    raise _Stalled
                if not values or not any(name in values for name in columns):
                    continue
                cells = [
                    format_timestamp(record.timestamp),
                    _SECONDS_FORMAT.format(at),
                    *(csv_cell(repr(values[name])) if name in values else "" for name in columns),
                ]
                line = encode_csv_line(",".join(cells), request.line_ending)
                pending.append(line)
                pending_bytes += len(line)
                rows += 1
                if pending_bytes >= LOG_FLUSH_THRESHOLD_BYTES:
                    output.write(b"".join(pending))
                    pending, pending_bytes = [], 0
            output.write(b"".join(pending))
        return rows
