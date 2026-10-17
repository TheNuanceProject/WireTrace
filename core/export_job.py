# SPDX-License-Identifier: GPL-3.0-or-later
"""Saves a recorded session to ``.txt`` and/or ``.csv`` files.

An export reads a snapshot of the session spool and writes it out on a
thread of its own, so saving a long session never blocks the window.
The files use the same format and the same splitting as a live log
(see ``core.log_format`` and ``core.log_files``).

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

from app.constants import (
    CSV_MAX_ROWS_PER_PART,
    LOG_FLUSH_THRESHOLD_BYTES,
    ExportFormat,
)
from core.csv_engine import CSVEngine
from core.log_files import FileBackend, SplitLogFile
from core.log_format import (
    DEFAULT_FILE_LINE_ENDING,
    FileHeaderInfo,
    csv_header,
    encode_csv_line,
    encode_text_line,
    text_header,
)
from core.records import RecordOrigin, format_timestamp
from core.session_spool import SpoolSnapshot

logger = logging.getLogger(__name__)

_EXPORT_TITLE = "Exported Data"
_EXPORT_KIND = "Export"
_EXPORT_TIME_LABEL = "Exported"
_TRUNCATED_NOTE = (
    "The start of this session is no longer available; "
    "the export begins with the oldest data still held."
)
_THREAD_NAME = "wiretrace-export"


@dataclass(frozen=True, slots=True)
class ExportRequest:
    """What to export and where.

    Attributes:
        text_path: Path of the ``.txt`` file, or None to skip it.
        csv_path: Path of the ``.csv`` file, or None to skip it.
        name: Name the user gave the export.
        port_name: Serial port of the session, empty if none.
        baud_rate: Baud rate of the session, 0 if none.
        split_bytes: Size at which each file starts a new part (0 = never).
        line_ending: The characters that end each line of the files.
    """

    text_path: str | None
    csv_path: str | None
    name: str
    port_name: str
    baud_rate: int
    split_bytes: int
    line_ending: str = DEFAULT_FILE_LINE_ENDING

    @staticmethod
    def paths_for(export_format: ExportFormat, base_path: str) -> tuple[str | None, str | None]:
        """Return the ``(text_path, csv_path)`` a format calls for."""
        text = f"{base_path}.txt" if export_format is not ExportFormat.CSV else None
        csv = f"{base_path}.csv" if export_format is not ExportFormat.TXT else None
        return text, csv


@dataclass(frozen=True, slots=True)
class ExportResult:
    """The outcome of an export.

    Attributes:
        text_paths: Every part of the text file written, in order.
        csv_paths: Every part of the CSV file written, in order.
        lines: Session lines exported.
        error: Why the export failed, or empty if it succeeded.
        cancelled: True if the export was stopped before it finished.
    """

    text_paths: tuple[str, ...]
    csv_paths: tuple[str, ...]
    lines: int
    error: str
    cancelled: bool

    @property
    def paths(self) -> tuple[str, ...]:
        """Every file written: the text file's parts, then the CSV file's."""
        return self.text_paths + self.csv_paths


class ExportJob:
    """Writes one snapshot to disk on a background thread.

    Args:
        snapshot: The session data to export. Released when the job ends.
        request: Destination and labels.
        on_finished: Called on the export thread with the result.
        backend: File-system operations; replaceable for tests.
    """

    def __init__(
        self,
        snapshot: SpoolSnapshot,
        request: ExportRequest,
        on_finished: Callable[[ExportResult], None],
        backend: FileBackend | None = None,
    ) -> None:
        self._snapshot = snapshot
        self._request = request
        self._on_finished = on_finished
        self._backend = backend
        self._cancelled = threading.Event()
        self._thread = threading.Thread(target=self._run, name=_THREAD_NAME, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def cancel(self) -> None:
        """Ask the export to stop. Files written so far are removed."""
        self._cancelled.set()

    def join(self) -> None:
        """Wait for the export thread to end."""
        if self._thread.is_alive():
            self._thread.join()

    # ── Thread ───────────────────────────────────────────────────────────

    def _run(self) -> None:
        files: list[SplitLogFile] = []
        try:
            result = self._export(files)
        except Exception as exc:
            # Whatever went wrong, the caller is told and no partial
            # file is left behind looking like a finished export.
            if isinstance(exc, OSError):
                logger.error("Export failed: %s", exc)
            else:
                logger.exception("Export failed unexpectedly")
            self._remove(files)
            result = ExportResult(
                text_paths=(), csv_paths=(), lines=0,
                error=str(exc) or type(exc).__name__, cancelled=False,
            )
        finally:
            self._snapshot.release()
        self._on_finished(result)

    def _export(self, files: list[SplitLogFile]) -> ExportResult:
        request = self._request
        info = FileHeaderInfo(
            title=_EXPORT_TITLE,
            kind=_EXPORT_KIND,
            name=request.name,
            time_label=_EXPORT_TIME_LABEL,
            time=datetime.now(),
            port_name=request.port_name,
            baud_rate=request.baud_rate,
            note=_TRUNCATED_NOTE if self._snapshot.truncated else "",
        )

        line_ending = request.line_ending
        text_file: SplitLogFile | None = None
        csv_file: SplitLogFile | None = None
        engine = CSVEngine()
        if request.text_path is not None:
            text_file = SplitLogFile(
                request.text_path,
                header=lambda part: text_header(info, part, line_ending),
                loss_marker=_no_loss_marker,
                part_limit_bytes=request.split_bytes,
                backend=self._backend,
            )
            files.append(text_file)
        if request.csv_path is not None:
            csv_file = SplitLogFile(
                request.csv_path,
                header=lambda part: csv_header(info, part, engine.header, line_ending),
                loss_marker=_no_loss_marker,
                part_limit_bytes=request.split_bytes,
                part_limit_units=CSV_MAX_ROWS_PER_PART if request.split_bytes else 0,
                eager_header=False,
                backend=self._backend,
            )
            files.append(csv_file)

        for log_file, path in ((text_file, request.text_path), (csv_file, request.csv_path)):
            if log_file is not None:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                log_file.open()

        lines = 0
        for record in self._snapshot.records():
            if self._cancelled.is_set():
                self._remove(files)
                return ExportResult(text_paths=(), csv_paths=(), lines=0, error="", cancelled=True)
            if record.origin is not RecordOrigin.NOTICE:
                lines += 1
            if text_file is not None:
                text_file.append(encode_text_line(record, line_ending))
                self._flush_if_due(text_file)
            if csv_file is not None and record.origin is RecordOrigin.DEVICE:
                for row in engine.feed(format_timestamp(record.timestamp), record.text):
                    csv_file.append(encode_csv_line(row, line_ending))
                self._flush_if_due(csv_file)

        if csv_file is not None:
            for row in engine.finalize():
                csv_file.append(encode_csv_line(row, line_ending))
        for log_file in files:
            log_file.close()

        return ExportResult(
            text_paths=tuple(text_file.paths) if text_file is not None else (),
            csv_paths=tuple(csv_file.paths) if csv_file is not None else (),
            lines=lines, error="", cancelled=False,
        )

    @staticmethod
    def _flush_if_due(log_file: SplitLogFile) -> None:
        if log_file.queued_bytes >= LOG_FLUSH_THRESHOLD_BYTES:
            log_file.flush()

    @staticmethod
    def _remove(files: list[SplitLogFile]) -> None:
        """Close and delete the files of an export that did not complete."""
        for log_file in files:
            with contextlib.suppress(OSError):
                log_file.close()
            for path in log_file.paths:
                with contextlib.suppress(OSError):
                    os.remove(path)


def _no_loss_marker(count: int) -> bytes:
    """An export stops at the first write error, so nothing is ever skipped."""
    raise AssertionError(f"export reported {count} lost lines")
