# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace's diagnostic log: its own events, kept on this computer.

What is written is what the application logs about itself: starting,
ports opened and closed with their driver and settings, errors,
recoveries, updates. Nothing a device sends and nothing the user types
is logged anywhere in WireTrace; an unexpected error met while handling
either is logged by its kind and place only (``log_data_failure``),
since an error's own words can quote the data. The log is never sent.

Nothing waits on the disk: a message is put in a queue and one thread
writes it. A message that finds the queue full is dropped, and the next
one written says how many were.

Several WireTrace windows can be open at once, each its own process, so
each run writes its own files (``wiretrace-20261119-182700-4242.log``,
continued as ``-2``, ``-3`` once a file is full). When a file is begun,
the oldest files are removed until the folder has room for a full file
of this run and of every other run still writing (whose newest file,
which that run holds open, may still grow to a full one), so the folder
never holds more than its total. A file the system will not let go is passed over. When the room
left is less than a full file, this run's file is kept to the room
left; with none, messages are not written, and the next one written
says how many were not.

This module does not touch the GUI.
"""

from __future__ import annotations

import collections
import contextlib
import errno
import logging
import logging.handlers
import os
import queue
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path

from app.constants import (
    DIAGNOSTIC_LOG_FILE_BYTES,
    DIAGNOSTIC_LOG_FOLDER_LOCK,
    DIAGNOSTIC_LOG_PREFIX,
    DIAGNOSTIC_LOG_QUEUE_RECORDS,
    DIAGNOSTIC_LOG_STOP_SECONDS,
    DIAGNOSTIC_LOG_SUFFIX,
    DIAGNOSTIC_LOG_TOTAL_BYTES,
    DIAGNOSTIC_SUMMARY_EVENTS,
    SHARED_FILE_LOCK_TIMEOUT_S,
)
from app.file_lock import FileLock

_PRIVATE_FILE = 0o600
_PRIVATE_DIRECTORY = 0o700
_ENCODING = "utf-8"

# A run's file: the run (the moment it started and its process number),
# then the part, from the second file on.
_RUN_FILE = re.compile(
    re.escape(DIAGNOSTIC_LOG_PREFIX) + r"(?P<run>\d{8}-\d{6}-\d+)(?:-(?P<part>\d+))?"
    + re.escape(DIAGNOSTIC_LOG_SUFFIX)
)

# Windows locks a range of a file, which other programs may then not read
# or write: the range held is one byte this far past the end of any log,
# so that nothing that reads the file is stopped.
_LOCK_OFFSET = 1 << 62


def _hold(stream: int) -> None:
    """Mark the file open on ``stream`` as being written by this run, for
    as long as it stays open: other runs then give it a full file's room.
    The system lets go of the mark when the file is closed or the process
    ends, however it ends."""
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            import msvcrt

            os.lseek(stream, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(stream, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _age(modified: float, name: str) -> tuple[float, str, int]:
    """The order in which files are removed, oldest first: by the time each
    was last written, and among files written at the same time (the
    system's clock may move only every few milliseconds), by run and then
    by part, so a run's first file goes before its second."""
    match = _RUN_FILE.fullmatch(name)
    if match is None:
        return (modified, name, 0)
    return (modified, match.group("run"), int(match.group("part") or 1))


def _being_written(path: Path) -> bool:
    """Whether another run holds ``path`` open to write it (``_hold``).
    Only the system's answer that the mark is held counts: any other
    failure leaves the file counted as what it holds."""
    try:
        stream = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    except OSError:
        return False
    try:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(stream, _LOCK_OFFSET, os.SEEK_SET)
            try:
                msvcrt.locking(stream, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                return exc.errno == errno.EACCES
            msvcrt.locking(stream, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
    except OSError:
        return False
    finally:
        os.close(stream)
    return False


def described(error: BaseException) -> str:
    """An error as the diagnostic log names it: its kind, and for a system
    error the system's own words for it; never a file's name or anything
    the error quotes, which can be a name the user gave or what a file of
    theirs holds. An error raised for another (``raise ... from``) names
    the first. A system error WireTrace raises with words of its own, and
    no other behind it, is named with those words, which hold no file's
    name."""
    cause = error.__cause__ if isinstance(error.__cause__, OSError) else error
    if isinstance(cause, OSError):
        if cause.strerror:
            return f"{type(cause).__name__}: {cause.strerror}"
        if cause is error and error.__cause__ is None and cause.filename is None and cause.args:
            return f"{type(cause).__name__}: {cause}"
    return type(cause).__name__


def log_data_failure(log: logging.Logger, message: str, *args: object) -> None:
    """Log the error being handled, met while handling what a device sent
    or what the user typed, by its kind and where it happened only.

    Call from an ``except`` block, in place of ``log.exception``. An
    error's own words can quote the data (``float("x12")`` names the
    text), so they are left out; the frames name only the code.
    """
    kind, _error, trace = sys.exc_info()
    name = kind.__name__ if kind is not None else "no error"
    frames = "".join(traceback.format_list(traceback.extract_tb(trace))).rstrip()
    # The frames are source code, which may hold a "%" of its own, so the
    # message is made whole before it is logged.
    log.error("%s: %s, at\n%s", message % args if args else message, name, frames)


def _moment(created: float) -> str:
    """A record's time, local, to the millisecond, with its offset from UTC."""
    return datetime.fromtimestamp(created).astimezone().isoformat(timespec="milliseconds")


class _Format(logging.Formatter):
    """One line per event: the moment, the level, the thread, the source."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return _moment(record.created)


class _Queueing(logging.handlers.QueueHandler):
    """Puts each record in the queue without waiting; counts those it
    cannot."""

    def __init__(self, records: queue.Queue) -> None:
        super().__init__(records)
        self._lock = threading.Lock()
        self._dropped = 0

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            with self._lock:
                self._dropped += 1

    def take_dropped(self) -> int:
        """How many records were dropped since this was last asked."""
        with self._lock:
            dropped, self._dropped = self._dropped, 0
        return dropped


class _RunFiles:
    """Writes records to this run's files, on the writing thread.

    Handed records by the queue's listener alone, so it is not a logging
    handler of its own: it holds no handler lock, and the logging module
    never waits on it at exit, even while a disk that does not answer
    holds the writing thread.
    """

    #: The listener passes every record; levels are decided before the queue.
    level = logging.NOTSET

    def __init__(
        self,
        directory: Path,
        stem: str,
        dropped: Callable[[], int],
        file_bytes: int,
        total_bytes: int,
    ) -> None:
        self._format = _Format()
        self._directory = directory
        self._stem = stem
        self._dropped = dropped
        self._file_bytes = file_bytes
        self._total_bytes = total_bytes
        self._stream: int | None = None
        self._size = 0
        self._part = 0
        #: The most the current file may hold: a full file, or the room
        #: the folder had left when it was begun.
        self._room = file_bytes
        #: Messages not written for want of room in the folder, or that the
        #: disk refused, and messages the queue dropped whose notice could
        #: not be written yet.
        self._unwritten = 0
        self._dropped_pending = 0
        self.current: Path | None = None
        #: This run's latest events, and its latest warnings and errors,
        #: read from other threads under the lock.
        self._recent_lock = threading.Lock()
        self._events: collections.deque[str] = collections.deque(maxlen=DIAGNOSTIC_SUMMARY_EVENTS)
        self._problems: collections.deque[str] = collections.deque(maxlen=DIAGNOSTIC_SUMMARY_EVENTS)

    def recent(self) -> tuple[list[str], list[str]]:
        """This run's latest events, and its latest warnings and errors."""
        with self._recent_lock:
            return list(self._events), list(self._problems)

    def handle(self, record: logging.LogRecord) -> None:
        try:
            self.emit(record)
        except Exception:
            # A record that cannot be formatted is not written; the writing
            # thread carries on.
            self._unwritten += 1

    def emit(self, record: logging.LogRecord) -> None:
        self._dropped_pending += self._dropped()
        if self._dropped_pending and self._write(
            f"{_moment(time.time())} WARNING [diagnostic log] {self._dropped_pending} messages were not "
            "written: they came faster than the disk took them\n"
        ):
            self._dropped_pending = 0
        if self._unwritten and self._write(
            f"{_moment(time.time())} WARNING [diagnostic log] {self._unwritten} messages were not "
            "written: the folder had no room for them, or the disk refused them\n"
        ):
            self._unwritten = 0
        line = self._format.format(record)
        with self._recent_lock:
            self._events.append(line)
            if record.levelno >= logging.WARNING:
                self._problems.append(line)
        self._write_or_count(line + "\n")

    def _write_or_count(self, text: str) -> None:
        if not self._write(text):
            self._unwritten += 1

    def _write(self, text: str) -> bool:
        """Write ``text`` to the current file, or to the next if it does not
        fit; whether it was written. A write the disk refuses (it is full,
        say) leaves the file as it is, to be written again later."""
        data = text.encode(_ENCODING, "replace")
        if self._stream is None or self._size + len(data) > self._room:
            self._begin_file()
            if self._stream is None or self._size + len(data) > self._room:
                return False
        try:
            os.write(self._stream, data)
        except OSError:
            return False
        self._size += len(data)
        return True

    def _begin_file(self) -> None:
        """Begin this run's next file, if the folder has room for any: the
        room left, at most a full file, is what the file may hold. Done
        under the folder's lock, so that two runs never count the same
        room, and a run's file is marked before any other run looks."""
        lock = FileLock(self._directory / DIAGNOSTIC_LOG_FOLDER_LOCK)
        try:
            lock.acquire(SHARED_FILE_LOCK_TIMEOUT_S)
        except OSError:
            return
        try:
            if self._stream is not None and self._size == 0:
                # The current file is empty and still too small: it is
                # measured again, rather than another empty file begun.
                self._room = self._room_left()
                return
            self._end_file()
            room = self._room_left()
            if room <= 0:
                return
            self._part += 1
            suffix = "" if self._part == 1 else f"-{self._part}"
            path = self._directory / f"{self._stem}{suffix}{DIAGNOSTIC_LOG_SUFFIX}"
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
                self._stream = os.open(path, flags, _PRIVATE_FILE)
            except OSError:
                return
            _hold(self._stream)
            self._size = 0
            self._room = room
            self.current = path
        finally:
            lock.release()

    def _end_file(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(OSError):
                os.close(self._stream)
            self._stream = None

    def _room_left(self) -> int:
        """Remove the oldest files until the folder has room for a full
        file of every other run still going and one of this run, or until
        none is left to remove; return the room this run's next file has,
        at most a full file.

        Another run's newest file counts as a full one while that run
        holds it open (``_hold``), since it may still grow to one; every
        other file counts as what it holds. This run's own files count as what they
        hold, and its current one, about to be ended, among them."""
        files = []
        for path in self._directory.glob(f"{DIAGNOSTIC_LOG_PREFIX}*{DIAGNOSTIC_LOG_SUFFIX}"):
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append((_age(stat.st_mtime, path.name), path.name, stat.st_size, path))
        # Each other run's newest file, by run: its part and the file.
        newest: dict[str, tuple[int, Path]] = {}
        for _, name, _, path in files:
            match = _RUN_FILE.fullmatch(name)
            if match is None or f"{DIAGNOSTIC_LOG_PREFIX}{match.group('run')}" == self._stem:
                continue
            part = int(match.group("part") or 1)
            run = match.group("run")
            if run not in newest or part > newest[run][0]:
                newest[run] = (part, path)
        going = {path for _, path in newest.values() if _being_written(path)}
        held = 0
        for _, _, size, path in files:
            held += self._file_bytes if path in going else size
        for _, _, size, path in sorted(files):
            if held + self._file_bytes <= self._total_bytes:
                break
            if path in going or (path == self.current and self._stream is not None):
                continue
            try:
                path.unlink()
            except OSError:
                continue
            held -= size
        return min(self._file_bytes, self._total_bytes - held)

    def close(self) -> None:
        self._end_file()


class _Listener(logging.handlers.QueueListener):
    """The writing thread, stopped within a time limit.

    ``QueueListener.stop`` puts its end mark in the queue without waiting,
    which fails while the queue is full, and then waits for the thread
    without limit, which a disk that has stopped answering would make
    endless."""

    def stop_within(self, seconds: float) -> bool:
        """Ask the thread to write what is waiting and end, and wait for it
        at most ``seconds``; whether it ended. Its thread is a daemon, so
        one still writing does not keep the process from exiting."""
        thread = self._thread
        if thread is None:
            return True
        deadline = time.monotonic() + seconds
        try:
            self.queue.put(self._sentinel, timeout=seconds)
        except queue.Full:
            return False
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            return False
        self._thread = None
        return True


class DiagnosticLog:
    """This run's diagnostic log.

    ``start`` sends every message logged in the process to it, from then
    on; ``stop`` writes what is waiting and closes the file.
    """

    def __init__(
        self,
        directory: Path,
        *,
        file_bytes: int = DIAGNOSTIC_LOG_FILE_BYTES,
        total_bytes: int = DIAGNOSTIC_LOG_TOTAL_BYTES,
        queue_records: int = DIAGNOSTIC_LOG_QUEUE_RECORDS,
        started: datetime | None = None,
    ) -> None:
        moment = (started or datetime.now()).strftime("%Y%m%d-%H%M%S")
        self._directory = directory
        self._queueing = _Queueing(queue.Queue(maxsize=queue_records))
        self._queueing.setFormatter(logging.Formatter("%(message)s"))
        self._files = _RunFiles(
            directory, f"{DIAGNOSTIC_LOG_PREFIX}{moment}-{os.getpid()}",
            self._queueing.take_dropped, file_bytes, total_bytes,
        )
        self._listener = _Listener(self._queueing.queue, self._files)
        self._running = False

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def current_file(self) -> Path | None:
        """The file being written, once one has been begun."""
        return self._files.current

    def recent(self) -> tuple[list[str], list[str]]:
        """This run's latest events, and its latest warnings and errors,
        each oldest first, as written."""
        return self._files.recent()

    def start(self, first_lines: Iterable[str] = ()) -> None:
        """Begin: ``first_lines`` are written first, as events of this
        module, then everything logged in the process."""
        if self._running:
            return
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            # Has no effect on Windows, where the folder is inside the
            # user's profile and private to them already.
            os.chmod(self._directory, _PRIVATE_DIRECTORY)
        except OSError:
            # Writing is tried again with each file begun; until then
            # messages are not written.
            pass
        self._listener.start()
        logging.getLogger().addHandler(self._queueing)
        self._running = True
        log = logging.getLogger(__name__)
        for line in first_lines:
            log.info("%s", line)

    def stop(self) -> None:
        """Write what is waiting, then close the file; within
        ``DIAGNOSTIC_LOG_STOP_SECONDS``, after which the writing thread is
        left to the process's exit, still holding its file."""
        if not self._running:
            return
        logging.getLogger().removeHandler(self._queueing)
        if self._listener.stop_within(DIAGNOSTIC_LOG_STOP_SECONDS):
            self._files.close()
        self._running = False


def diagnostic_summary(
    installation: Iterable[str],
    ports: Iterable[str],
    directory: Path,
    events: Iterable[str],
    problems: Iterable[str],
) -> str:
    """The text the user copies to paste into a report: what this run is
    (``installation``), the ports open now, where the log is, and this
    run's latest warnings and errors and latest events. It holds only what
    the log holds, so nothing a device sent and nothing typed."""

    def block(title: str, items: Iterable[str], empty: str) -> list[str]:
        listed = [f"  {item}" for item in items]
        return [title, *(listed or [f"  {empty}"]), ""]

    return "\n".join([
        *installation,
        f"Diagnostic log: {directory}",
        "",
        *block("Ports open:", ports, "none"),
        *block("Latest warnings and errors in this run:", problems, "none"),
        *block("Latest events in this run:", events, "none"),
    ]).rstrip() + "\n"
