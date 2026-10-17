# SPDX-License-Identifier: GPL-3.0-or-later
"""Usage statistics: what is noted while WireTrace is used, and when it is sent.

They are off unless the user switches them on. Switching them on makes
a random installation identifier on this computer: 128 bits from the
system's secure generator, derived from nothing about the computer, the
user or the network. While they are on, the application notes for each
day which features were used, which problems occurred, the highest line
rate, the number of connections, the longest time one window stayed
open and how many times WireTrace was started, each as yes or no or as
a range: never a name, a file, a command or anything a device sent.

Everything is kept in one small file beside the preferences. Several
WireTrace windows may be open at once: each changes the file only while
it holds the file's lock, and only after reading what the others wrote,
so nothing one window notes is lost. The file is written, whole and
atomically, only when something new is noted.

When a day has passed, its notes become one report, which carries the
identifier and the date of that day. Waiting reports are sent at the
next start and after each change of day while WireTrace stays open, one
at a time with a random wait between them; never while it closes. One
that cannot be sent is tried again later, and dropped once it is
``USAGE_REPORT_MAX_AGE_DAYS`` old.

Each running WireTrace also holds a lock of its own in a folder beside
the file. A normal close lets it go and removes it; a lock that can be
taken while its run is still listed means that run ended without
closing, and ``closed_unexpectedly`` is noted for the last day it noted.

Switching the statistics off deletes the identifier, the notes and the
waiting reports at once, and sends a request to delete every record the
server holds for that identifier, again at every start and every hour
until it is delivered. Once delivered, it is delivered a second time
``USAGE_DELETION_CONFIRM_AFTER_S`` later, so a report that was already on
its way, from this window or another, is deleted too. Switching them on
again makes a new identifier.

What a report holds is stated, field by field, in ``updater/privacy.py``;
a test keeps the two in step.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QGuiApplication

from app._atomic import atomic_write_text
from app.config import ConfigManager
from app.constants import (
    SHARED_FILE_LOCK_SUFFIX,
    SHARED_FILE_LOCK_TIMEOUT_S,
    THEME_FOLLOW_SYSTEM,
    USAGE_ANSWER_MAX_BYTES,
    USAGE_DAY_CHANGE_DELAY_S,
    USAGE_DELETION_CONFIRM_AFTER_S,
    USAGE_DELETION_RETRY_S,
    USAGE_NOTES_FILENAME,
    USAGE_REPORT_MAX_AGE_DAYS,
    USAGE_RUNS_FOLDER,
    USAGE_SEND_DELAY_S,
    USAGE_SEND_SPACING_MAX_S,
    USAGE_SEND_SPACING_MIN_S,
    USAGE_SEND_TIMEOUT_S,
    ThemeID,
)
from app.file_lock import FileLock, held_lock
from updater.background import Task
from updater.download import DownloadError, Opener, send
from updater.install import architecture, install_channel, system_name, user_agent
from version import APP_VERSION, USAGE_DELETE_URL, USAGE_REPORT_URL

logger = logging.getLogger(__name__)


class Feature(Enum):
    """A feature whose use on a day is noted as yes or no."""

    LOG = "log"
    CSV = "csv"
    EXPORT = "export"
    PLOT = "plot"
    PLOT_PATTERN = "plot_pattern"
    HEX = "hex"
    SAVED_COMMANDS = "saved_commands"
    TABS = "tabs"
    FILTER = "filter"
    FIND = "find"


class Problem(Enum):
    """A problem whose occurrence on a day is noted as yes or no."""

    LINES_LOST = "lines_lost"
    LOG_WRITE_FAILED = "log_write_failed"
    CONNECTION_LOST = "connection_lost"
    CLOSED_UNEXPECTEDLY = "closed_unexpectedly"


#: The fields of a report, in the order they are sent.
REPORT_FIELDS = (
    "installation", "day", "version", "os", "arch", "channel", "features", "problems",
    "peak_line_rate", "connections", "theme", "scale", "longest_session", "starts",
)

#: The highest line rate of the day, in lines a second, as one of these
#: ranges: each name stands for the rates below its bound.
LINE_RATE_RANGES: tuple[tuple[int, str], ...] = (
    (1, "none"),
    (100, "under_100"),
    (1_000, "under_1000"),
    (10_000, "under_10000"),
)
LINE_RATE_TOP = "10000_or_more"

#: The connections opened on a day, as one of these ranges: each name
#: stands for the counts up to its bound.
CONNECTION_RANGES: tuple[tuple[int, str], ...] = (
    (0, "none"),
    (1, "1"),
    (5, "2_to_5"),
    (20, "6_to_20"),
)
CONNECTIONS_TOP = "more_than_20"

#: The longest time one window was open on a day, in seconds, as one of
#: these ranges: each name stands for the times below its bound.
SESSION_RANGES: tuple[tuple[int, str], ...] = (
    (15 * 60, "under_15_minutes"),
    (60 * 60, "15_to_60_minutes"),
    (4 * 60 * 60, "1_to_4_hours"),
    (8 * 60 * 60, "4_to_8_hours"),
)
SESSION_TOP = "8_hours_or_more"

#: The starts of WireTrace on a day, as one of these ranges: each name
#: stands for the counts up to its bound.
START_RANGES: tuple[tuple[int, str], ...] = (
    (0, "none"),
    (1, "1"),
    (3, "2_to_3"),
    (10, "4_to_10"),
)
STARTS_TOP = "more_than_10"

#: Screen scales, in percent. The nearest one is reported.
SCALES = (100, 125, 150, 200)

#: The theme settings a report may name.
THEMES = (THEME_FOLLOW_SYSTEM, *(theme.value for theme in ThemeID))

_FILE_FORMAT = 2
_PERCENT = 100
#: A name WireTrace gives its own version, system, processor or kind of
#: installation. Anything else read from the file is not trusted.
_TOKEN = re.compile(r"[A-Za-z0-9_.+-]{1,32}")
#: An installation identifier: 128 random bits as lowercase hex.
_INSTALLATION = re.compile(r"[0-9a-f]{32}")
_INSTALLATION_BYTES = 16
#: The name of one run's lock: 64 random bits as lowercase hex.
_RUN = re.compile(r"[0-9a-f]{16}")
_RUN_BYTES = 8
_RUN_LOCK_SUFFIX = ".lock"
_HTTP_OK_FIRST, _HTTP_OK_LAST = 200, 299
_HTTP_REFUSED_FIRST, _HTTP_REFUSED_LAST = 400, 499
_HTTP_TOO_MANY = 429
_MS_PER_SECOND = 1000
#: What a report names a system or processor by when the system's own
#: name for it is not one WireTrace can send.
_OTHER = "other"


def line_rate_range(lines_per_second: int) -> str:
    """The range a line rate falls in."""
    for bound, name in LINE_RATE_RANGES:
        if lines_per_second < bound:
            return name
    return LINE_RATE_TOP


def connections_range(count: int) -> str:
    """The range a number of connections falls in."""
    for bound, name in CONNECTION_RANGES:
        if count <= bound:
            return name
    return CONNECTIONS_TOP


def session_range(seconds: float) -> str:
    """The range a time a window was open falls in."""
    for bound, name in SESSION_RANGES:
        if seconds < bound:
            return name
    return SESSION_TOP


def starts_range(count: int) -> str:
    """The range a number of starts falls in."""
    for bound, name in START_RANGES:
        if count <= bound:
            return name
    return STARTS_TOP


def nearest_scale(device_pixel_ratio: float) -> int:
    """The listed screen scale nearest to a device pixel ratio."""
    percent = device_pixel_ratio * _PERCENT
    return min(SCALES, key=lambda scale: abs(scale - percent))


def _screen_device_pixel_ratio() -> float:
    screen = QGuiApplication.primaryScreen()
    return screen.devicePixelRatio() if screen is not None else 1.0


_RATE_NAMES = (*(name for _, name in LINE_RATE_RANGES), LINE_RATE_TOP)
_CONNECTION_NAMES = (*(name for _, name in CONNECTION_RANGES), CONNECTIONS_TOP)
_SESSION_NAMES = (*(name for _, name in SESSION_RANGES), SESSION_TOP)
_START_NAMES = (*(name for _, name in START_RANGES), STARTS_TOP)
_FEATURE_NAMES = tuple(feature.value for feature in Feature)
_PROBLEM_NAMES = tuple(problem.value for problem in Problem)
#: Counts beyond the top range are all the same; they are not kept beyond it.
_CONNECTIONS_KEPT = CONNECTION_RANGES[-1][0] + 1
_STARTS_KEPT = START_RANGES[-1][0] + 1


# ── What the file holds ──────────────────────────────────────────────────


@dataclass
class _DayNotes:
    """What was noted on one day. Only what a report holds is kept."""

    day: date
    version: str
    os: str
    arch: str
    channel: str
    theme: str
    scale: int
    features: set[str] = field(default_factory=set)
    problems: set[str] = field(default_factory=set)
    peak_line_rate: str = LINE_RATE_RANGES[0][1]
    connections: int = 0
    longest_session: str = SESSION_RANGES[0][1]
    starts: int = 0

    def report(self, installation: str) -> dict[str, object]:
        """The report of this day, exactly as it is sent."""
        values: dict[str, object] = {
            "installation": installation,
            "day": self.day.isoformat(),
            "version": self.version,
            "os": self.os,
            "arch": self.arch,
            "channel": self.channel,
            "features": sorted(self.features),
            "problems": sorted(self.problems),
            "peak_line_rate": self.peak_line_rate,
            "connections": connections_range(self.connections),
            "theme": self.theme,
            "scale": self.scale,
            "longest_session": self.longest_session,
            "starts": starts_range(self.starts),
        }
        return {name: values[name] for name in REPORT_FIELDS}

    def stored(self) -> dict[str, object]:
        return {
            "day": self.day.isoformat(),
            "version": self.version,
            "os": self.os,
            "arch": self.arch,
            "channel": self.channel,
            "theme": self.theme,
            "scale": self.scale,
            "features": sorted(self.features),
            "problems": sorted(self.problems),
            "peak_line_rate": self.peak_line_rate,
            "connections": self.connections,
            "longest_session": self.longest_session,
            "starts": self.starts,
        }


@dataclass(frozen=True)
class _Waiting:
    """A finished report that has not been sent yet."""

    day: date
    report: dict[str, object]


@dataclass(frozen=True)
class _Deletion:
    """A request to delete what the server holds for an identifier.

    ``first_delivered`` is when it was first delivered, by the computer's
    clock; it is delivered once more after that, and then forgotten.
    """

    installation: str
    first_delivered: datetime | None = None

    def stored(self) -> dict[str, object]:
        return {
            "installation": self.installation,
            "first_delivered": self.first_delivered.isoformat() if self.first_delivered is not None else None,
        }


@dataclass
class _State:
    """Everything the file holds. An empty state is no file at all."""

    installation: str | None = None
    notes: _DayNotes | None = None
    waiting: list[_Waiting] = field(default_factory=list)
    deletions: list[_Deletion] = field(default_factory=list)
    #: Each run that holds a lock, and the last day it noted.
    runs: dict[str, date] = field(default_factory=dict)

    def empty(self) -> bool:
        return (
            self.installation is None and self.notes is None and not self.waiting
            and not self.deletions and not self.runs
        )

    def stored(self) -> dict[str, object]:
        return {
            "format": _FILE_FORMAT,
            "installation": self.installation,
            "notes": self.notes.stored() if self.notes is not None else None,
            "waiting": [{"day": item.day.isoformat(), "report": item.report} for item in self.waiting],
            "deletions": [item.stored() for item in self.deletions],
            "runs": {run: day.isoformat() for run, day in sorted(self.runs.items())},
        }


# ── Reading the file, trusting nothing in it ─────────────────────────────


class _Invalid(ValueError):
    """Something in the notes file is not what WireTrace writes."""


def _token(value: object) -> str:
    if not isinstance(value, str) or _TOKEN.fullmatch(value) is None:
        raise _Invalid(f"not a name: {value!r}")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or _INSTALLATION.fullmatch(value) is None:
        raise _Invalid("not an installation identifier")
    return value


def _choice(value: object, allowed: tuple[object, ...]) -> object:
    if isinstance(value, bool) or value not in allowed:
        raise _Invalid(f"not one of the allowed values: {value!r}")
    return value


def _names(value: object, allowed: tuple[str, ...]) -> set[str]:
    if not isinstance(value, list):
        raise _Invalid(f"not a list: {value!r}")
    return {str(_choice(item, allowed)) for item in value}


def _count(value: object, kept: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _Invalid(f"not a count: {value!r}")
    return min(value, kept)


def _moment(value: object) -> datetime:
    if not isinstance(value, str):
        raise _Invalid(f"not a time: {value!r}")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise _Invalid(f"not a time: {value!r}") from exc


def _parse_deletion(stored: object) -> _Deletion:
    if not isinstance(stored, dict) or set(stored) != {"installation", "first_delivered"}:
        raise _Invalid("a deletion request does not have the fields of one")
    delivered = stored["first_delivered"]
    return _Deletion(
        installation=_identifier(stored["installation"]),
        first_delivered=_moment(delivered) if delivered is not None else None,
    )


def _day(value: object) -> date:
    if not isinstance(value, str):
        raise _Invalid(f"not a day: {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise _Invalid(f"not a day: {value!r}") from exc


def _parse_notes(stored: object) -> _DayNotes:
    if not isinstance(stored, dict):
        raise _Invalid("the notes are not a table")
    return _DayNotes(
        day=_day(stored.get("day")),
        version=_token(stored.get("version")),
        os=_token(stored.get("os")),
        arch=_token(stored.get("arch")),
        channel=_token(stored.get("channel")),
        theme=str(_choice(stored.get("theme"), THEMES)),
        scale=int(_choice(stored.get("scale"), SCALES)),
        features=_names(stored.get("features"), _FEATURE_NAMES),
        problems=_names(stored.get("problems"), _PROBLEM_NAMES),
        peak_line_rate=str(_choice(stored.get("peak_line_rate"), _RATE_NAMES)),
        connections=_count(stored.get("connections"), _CONNECTIONS_KEPT),
        longest_session=str(_choice(stored.get("longest_session"), _SESSION_NAMES)),
        starts=_count(stored.get("starts"), _STARTS_KEPT),
    )


def _parse_report(stored: object) -> dict[str, object]:
    if not isinstance(stored, dict) or tuple(stored) != REPORT_FIELDS:
        raise _Invalid("the report does not have the fields of a report")
    return {
        "installation": _identifier(stored["installation"]),
        "day": _day(stored["day"]).isoformat(),
        "version": _token(stored["version"]),
        "os": _token(stored["os"]),
        "arch": _token(stored["arch"]),
        "channel": _token(stored["channel"]),
        "features": sorted(_names(stored["features"], _FEATURE_NAMES)),
        "problems": sorted(_names(stored["problems"], _PROBLEM_NAMES)),
        "peak_line_rate": _choice(stored["peak_line_rate"], _RATE_NAMES),
        "connections": _choice(stored["connections"], _CONNECTION_NAMES),
        "theme": _choice(stored["theme"], THEMES),
        "scale": _choice(stored["scale"], SCALES),
        "longest_session": _choice(stored["longest_session"], _SESSION_NAMES),
        "starts": _choice(stored["starts"], _START_NAMES),
    }


def _parse_state(stored: object) -> _State:
    if not isinstance(stored, dict) or stored.get("format") != _FILE_FORMAT:
        raise _Invalid("not a notes file of this version")
    installation = stored.get("installation")
    notes = stored.get("notes")
    waiting = stored.get("waiting")
    deletions = stored.get("deletions")
    runs = stored.get("runs")
    if not isinstance(waiting, list) or not isinstance(deletions, list) or not isinstance(runs, dict):
        raise _Invalid("the file does not have the parts of a notes file")
    parsed_waiting = []
    for item in waiting:
        if not isinstance(item, dict):
            raise _Invalid("a waiting report is not a table")
        parsed_waiting.append(_Waiting(_day(item.get("day")), _parse_report(item.get("report"))))
    parsed_runs = {}
    for run, day in runs.items():
        if _RUN.fullmatch(run) is None:
            raise _Invalid("not the name of a run")
        parsed_runs[run] = _day(day)
    return _State(
        installation=_identifier(installation) if installation is not None else None,
        notes=_parse_notes(notes) if notes is not None else None,
        waiting=parsed_waiting,
        deletions=list({item.installation: item for item in map(_parse_deletion, deletions)}.values()),
        runs=parsed_runs,
    )


# ── Noting and sending ───────────────────────────────────────────────────


class UsageStatistics(QObject):
    """Notes the use of the day and sends the reports of past days.

    Every method may be called whether the statistics are on or off;
    while they are off nothing is noted, kept or sent, except a deletion
    request still to be delivered. Nothing here ever raises into the
    caller: a file that cannot be read or written, or a server that
    cannot be reached, is logged and otherwise ignored.
    """

    def __init__(
        self,
        config: ConfigManager,
        parent: QObject | None = None,
        *,
        opener: Opener | None = None,
        now: Callable[[], datetime] = datetime.now,
        device_pixel_ratio: Callable[[], float] = _screen_device_pixel_ratio,
        spacing: Callable[[], float] = lambda: random.uniform(USAGE_SEND_SPACING_MIN_S, USAGE_SEND_SPACING_MAX_S),
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._opener = opener
        self._now = now
        self._device_pixel_ratio = device_pixel_ratio
        self._spacing = spacing
        # Beside the preferences, in the user's own settings folder.
        self._path = Path(config.config_path).with_name(USAGE_NOTES_FILENAME)
        self._lock_path = self._path.with_name(self._path.name + SHARED_FILE_LOCK_SUFFIX)
        self._runs_dir = self._path.with_name(USAGE_RUNS_FOLDER)
        #: What the file held when this window last read or wrote it.
        self._state = _State()
        #: This window's run, while statistics are on.
        self._run: str | None = None
        self._run_lock: FileLock | None = None
        self._session_started: datetime | None = None
        self._report_task: Task | None = None
        self._report_item: _Waiting | None = None
        self._deletion_task: Task | None = None

        self._send_soon = QTimer(self)
        self._send_soon.setSingleShot(True)
        self._send_soon.timeout.connect(self._send_next_report)
        self._day_change = QTimer(self)
        self._day_change.setSingleShot(True)
        self._day_change.timeout.connect(self._on_day_changed)
        self._session = QTimer(self)
        self._session.setSingleShot(True)
        self._session.timeout.connect(self._on_session_threshold)
        self._deletion_retry = QTimer(self)
        self._deletion_retry.setSingleShot(True)
        self._deletion_retry.timeout.connect(self._send_deletions)

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._config.usage_statistics

    @property
    def path(self) -> Path:
        """The file the notes are kept in."""
        return self._path

    @property
    def runs_dir(self) -> Path:
        """The folder each open WireTrace holds its lock in."""
        return self._runs_dir

    @property
    def installation(self) -> str | None:
        """This installation's identifier while statistics are on."""
        return self._state.installation if self._run is not None and self.enabled else None

    @property
    def sending(self) -> bool:
        return any(task is not None and task.running for task in (self._report_task, self._deletion_task))

    def start(self) -> None:
        """Begin, as the setting says: note and send, or end what is kept."""
        self.apply_setting()

    def stop(self) -> None:
        """WireTrace is closing normally: let this window's run go."""
        for timer in (self._send_soon, self._day_change, self._session, self._deletion_retry):
            timer.stop()
        run = self._run
        if run is not None:
            self._update(lambda state: state.runs.pop(run, None) is not None)
        self._release_run()

    def set_enabled(self, enabled: bool) -> None:
        """Switch the statistics on or off, and remember the choice."""
        self._config.usage_statistics = enabled
        self._config.save()
        self.apply_setting()

    def apply_setting(self) -> None:
        """Act on the setting as it stands in the preferences.

        Beginning, at a start or when switched on, counts this run as a
        start of the day.
        """
        if self.enabled:
            self._begin(count_start=self._run is None)
        else:
            self._end()

    def note_feature(self, feature: Feature) -> None:
        self._note(lambda notes: _added(notes.features, feature.value))

    def note_problem(self, problem: Problem) -> None:
        self._note(lambda notes: _added(notes.problems, problem.value))

    def note_line_rate(self, lines_per_second: int) -> None:
        measured = line_rate_range(max(0, lines_per_second))
        self._note(lambda notes: _raised(notes, "peak_line_rate", measured, _RATE_NAMES))

    def note_connection(self) -> None:
        def change(notes: _DayNotes) -> bool:
            if notes.connections >= _CONNECTIONS_KEPT:
                return False
            notes.connections += 1
            return True

        self._note(change, always=True)

    def waiting_days(self) -> list[date]:
        """The days whose reports wait to be sent, oldest first."""
        return [item.day for item in self._state.waiting]

    def pending_deletions(self) -> list[str]:
        """The identifiers whose deletion is still to be delivered, once
        or a second time."""
        return [item.installation for item in self._state.deletions]

    # ── Beginning and ending ─────────────────────────────────────────────

    def _begin(self, *, count_start: bool) -> None:
        if self._run is None:
            run = secrets.token_hex(_RUN_BYTES)
            lock = FileLock(self._runs_dir / f"{run}{_RUN_LOCK_SUFFIX}")
            try:
                if not lock.try_acquire():
                    raise OSError("a new run's lock is already held")
            except OSError as exc:
                logger.warning("Usage statistics could not begin: %s", exc)
                return
            self._run, self._run_lock = run, lock
            self._session_started = self._now()
        run = self._run
        today = self._today()

        def begin(state: _State) -> bool:
            changed = False
            if state.installation is None:
                state.installation = secrets.token_hex(_INSTALLATION_BYTES)
                changed = True
            changed = self._note_runs_that_ended(state, today) or changed
            if state.runs.get(run) != today:
                state.runs[run] = today
                changed = True
            if count_start:
                notes = self._today_notes(state, today)
                if notes.starts < _STARTS_KEPT:
                    notes.starts += 1
                changed = True
            return changed

        if not self._update(begin):
            self._release_run()
            return
        self._send_soon.start(USAGE_SEND_DELAY_S * _MS_PER_SECOND)
        self._schedule_day_change()
        self._schedule_session()
        self._schedule_deletions(USAGE_SEND_DELAY_S)

    def _end(self) -> None:
        """Statistics are off: forget everything but deletions to deliver."""
        for timer in (self._send_soon, self._day_change, self._session):
            timer.stop()
        if self._report_task is not None:
            self._report_task.cancel()

        def end(state: _State) -> bool:
            had = not state.empty()
            if state.installation is not None and all(
                item.installation != state.installation for item in state.deletions
            ):
                state.deletions.append(_Deletion(state.installation))
            state.installation = None
            state.notes = None
            state.waiting = []
            state.runs = {}
            return had

        self._update(end)
        self._release_run()
        self._remove_ended_run_locks()
        self._schedule_deletions(0)

    def _release_run(self) -> None:
        lock, self._run_lock, self._run = self._run_lock, None, None
        self._session_started = None
        if lock is None:
            return
        try:
            lock.remove()
        except OSError as exc:
            logger.warning("This run's lock could not be removed: %s", exc)

    def _remove_ended_run_locks(self) -> None:
        """Remove the locks of runs that have ended, now that none is listed."""
        try:
            paths = list(self._runs_dir.glob(f"*{_RUN_LOCK_SUFFIX}"))
        except OSError:
            return
        for path in paths:
            lock = FileLock(path)
            try:
                if lock.try_acquire():
                    lock.remove()
            except OSError as exc:
                logger.warning("An ended run's lock could not be removed: %s", exc)

    def _note_runs_that_ended(self, state: _State, today: date) -> bool:
        """Note ``closed_unexpectedly`` for each listed run whose lock is
        no longer held: it ended without closing."""
        changed = False
        for run, last_day in list(state.runs.items()):
            if run == self._run:
                continue
            lock = FileLock(self._runs_dir / f"{run}{_RUN_LOCK_SUFFIX}")
            try:
                ended = lock.try_acquire()
            except OSError as exc:
                logger.warning("A run's lock could not be checked: %s", exc)
                continue
            if not ended:
                continue
            try:
                lock.remove()
            except OSError as exc:
                logger.warning("An ended run's lock could not be removed: %s", exc)
            del state.runs[run]
            self._note_problem_on(state, last_day, today, Problem.CLOSED_UNEXPECTEDLY)
            changed = True
        return changed

    def _note_problem_on(self, state: _State, day: date, today: date, problem: Problem) -> None:
        """Note ``problem`` on ``day``, in its notes or its waiting report.
        A day whose report has already gone is let be."""
        if state.notes is not None and state.notes.day == day:
            state.notes.problems.add(problem.value)
            return
        for index, item in enumerate(state.waiting):
            if item.day == day:
                problems = sorted({*_listed(item.report["problems"]), problem.value})
                state.waiting[index] = _Waiting(day, {**item.report, "problems": problems})
                return
        if day == today:
            self._today_notes(state, today).problems.add(problem.value)

    # ── Keeping the notes ────────────────────────────────────────────────

    def _note(self, change: Callable[[_DayNotes], bool], *, always: bool = False) -> None:
        """Note something of today. The file is read and written only if
        it would change what this window knows, or ``always``."""
        if self._run is None or not self.enabled:
            return
        today = self._today()
        known = self._state.notes
        if not always and known is not None and known.day == today:
            trial = copy.deepcopy(known)
            if not change(trial) and not self._describe_display(trial):
                return
        run = self._run

        def apply(state: _State) -> bool:
            if state.installation is None:
                # Switched off in another window.
                return False
            notes = self._today_notes(state, today)
            state.runs[run] = today
            changed = change(notes)
            return self._describe_display(notes) or changed

        self._update(apply)
        if self._state.installation is None:
            self._stop_noting()

    def _stop_noting(self) -> None:
        """Statistics were switched off in another window: stop here too."""
        for timer in (self._send_soon, self._day_change, self._session):
            timer.stop()
        self._release_run()

    def _today(self) -> date:
        return self._now().date()

    def _today_notes(self, state: _State, today: date) -> _DayNotes:
        """The notes of today in ``state``, begun if there are none."""
        if state.notes is None or state.notes.day != today:
            state.notes = _DayNotes(
                day=today,
                version=APP_VERSION,
                os=_sendable(system_name()),
                arch=_sendable(architecture()),
                channel=_sendable(install_channel()),
                theme=self._theme(),
                scale=nearest_scale(self._device_pixel_ratio()),
            )
        return state.notes

    def _describe_display(self, notes: _DayNotes) -> bool:
        """Bring the theme and scale of the notes up to date."""
        theme = self._theme()
        scale = nearest_scale(self._device_pixel_ratio())
        if (notes.theme, notes.scale) == (theme, scale):
            return False
        notes.theme, notes.scale = theme, scale
        return True

    def _theme(self) -> str:
        theme = self._config.theme
        return theme if theme in THEMES else THEME_FOLLOW_SYSTEM

    def _roll(self, state: _State, today: date) -> bool:
        """Turn the notes of an earlier day into its report, and drop
        reports too old to send. Notes dated after today, left when the
        clock was set back, are let go: they cannot be told apart from
        today's."""
        changed = False
        notes = state.notes
        if notes is not None and notes.day != today:
            if notes.day < today and state.installation is not None:
                state.waiting.append(_Waiting(notes.day, notes.report(state.installation)))
            state.notes = None
            changed = True
        oldest = today - timedelta(days=USAGE_REPORT_MAX_AGE_DAYS)
        kept = [item for item in state.waiting if item.day >= oldest]
        if len(kept) != len(state.waiting):
            logger.info("Dropped %d usage report(s) that could not be sent", len(state.waiting) - len(kept))
            state.waiting = kept
            changed = True
        return changed

    def _read(self) -> tuple[_State, bool]:
        """What the file holds, and whether it must be written again
        because what it held could not be trusted."""
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _State(), False
        except OSError as exc:
            raise OSError(f"the usage notes could not be read: {exc}") from exc
        try:
            return _parse_state(json.loads(text)), False
        except (ValueError, KeyError, TypeError) as exc:
            # A file written by hand or damaged on disk is not trusted;
            # the notes start again.
            logger.warning("The usage notes were not readable and start again: %s", exc)
            return _State(), True

    def _write(self, state: _State) -> None:
        if state.empty():
            self._path.unlink(missing_ok=True)
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(str(self._path), json.dumps(state.stored(), indent=1))

    def _update(self, change: Callable[[_State], bool]) -> bool:
        """Read the file, apply ``change`` and write it back if anything
        changed, all while holding its lock. Whether it succeeded."""
        try:
            with held_lock(self._lock_path, SHARED_FILE_LOCK_TIMEOUT_S):
                state, damaged = self._read()
                rolled = self._roll(state, self._today())
                changed = change(state)
                if damaged or rolled or changed:
                    self._write(state)
        except OSError as exc:
            logger.warning("The usage notes could not be updated: %s", exc)
            return False
        self._state = state
        return True

    # ── The day and the session ──────────────────────────────────────────

    def _schedule_day_change(self) -> None:
        """Wake shortly after the next midnight, by the computer's clock."""
        now = self._now()
        midnight = datetime.combine(now.date() + timedelta(days=1), time.min, now.tzinfo)
        wait = midnight - now + timedelta(seconds=USAGE_DAY_CHANGE_DELAY_S)
        self._day_change.start(max(0, int(wait.total_seconds() * _MS_PER_SECOND)))

    def _on_day_changed(self) -> None:
        if self._run is None or not self.enabled:
            return
        today = self._today()
        run = self._run

        def roll(state: _State) -> bool:
            if state.installation is None:
                return False
            # A window open on a day is a day of use, even with nothing
            # else noted.
            self._today_notes(state, today)
            state.runs[run] = today
            return True

        self._update(roll)
        if self._state.installation is None:
            self._stop_noting()
            return
        self._send_next_report()
        self._schedule_day_change()
        self._schedule_session()

    def _session_seconds_today(self) -> float:
        """How long this window has been open today."""
        now = self._now()
        started = self._session_started or now
        midnight = datetime.combine(now.date(), time.min, now.tzinfo)
        return max(0.0, (now - max(started, midnight)).total_seconds())

    def _schedule_session(self) -> None:
        """Wake when this window's time open today reaches the next range."""
        seconds = self._session_seconds_today()
        for bound, _ in SESSION_RANGES:
            if seconds < bound:
                self._session.start(max(0, int((bound - seconds) * _MS_PER_SECOND)))
                return
        self._session.stop()

    def _on_session_threshold(self) -> None:
        measured = session_range(self._session_seconds_today())
        self._note(lambda notes: _raised(notes, "longest_session", measured, _SESSION_NAMES))
        if self._run is not None:
            self._schedule_session()

    # ── Sending ──────────────────────────────────────────────────────────

    def _send_next_report(self) -> None:
        """Send the oldest waiting report. The next follows after a wait."""
        if self._run is None or not self.enabled or self._report_task is not None:
            return
        self._update(lambda state: False)
        if not self._state.waiting:
            return
        item = self._state.waiting[0]
        body = json.dumps(item.report).encode("utf-8")
        opener = self._opener

        def work(task: Task) -> bool:
            """Send the report; whether it needs no further try."""
            status = send(
                USAGE_REPORT_URL, body, _headers(), opener=opener,
                max_bytes=USAGE_ANSWER_MAX_BYTES, timeout=USAGE_SEND_TIMEOUT_S,
            )
            return _finished(status, "Usage report")

        # The task announces its end on its own thread; bound methods of
        # this object are called on the main thread, where it lives.
        task = Task(work, (DownloadError,), self)
        task.succeeded.connect(self._on_report_answered)
        task.failed.connect(self._on_report_failed)
        task.cancelled.connect(self._on_report_cancelled)
        self._report_task, self._report_item = task, item
        task.start()

    def _on_report_answered(self, finished: object) -> None:
        item, self._report_item, self._report_task = self._report_item, None, None
        if finished is not True or item is None:
            return
        installation = item.report["installation"]
        self._update(lambda state: _remove_waiting(state, item.day, installation))
        if self._state.waiting and self._run is not None and self.enabled:
            self._send_soon.start(int(self._spacing() * _MS_PER_SECOND))

    def _on_report_failed(self, message: str) -> None:
        self._report_item, self._report_task = None, None
        logger.info("Usage report not sent, to be tried again: %s", message)

    def _on_report_cancelled(self) -> None:
        self._report_item, self._report_task = None, None

    def _schedule_deletions(self, delay_s: float) -> None:
        """Send the deletion requests after ``delay_s`` seconds, or later
        if none is due by then."""
        due = self._deletions_due_in()
        if due is None:
            self._deletion_retry.stop()
        else:
            self._deletion_retry.start(int(max(delay_s, due) * _MS_PER_SECOND))

    def _deletions_due_in(self) -> float | None:
        """In how many seconds a deletion request is next due; None if
        there is none."""
        now = self._now()
        waits = [_confirm_due_in(item, now) for item in self._state.deletions]
        return min(waits) if waits else None

    def _send_deletions(self) -> None:
        """Ask the server to delete the records of each identifier switched off."""
        if self._deletion_task is not None:
            return
        self._update(lambda state: False)
        now = self._now()
        due = [item for item in self._state.deletions if _confirm_due_in(item, now) == 0]
        if not due:
            self._schedule_deletions(0)
            return
        opener = self._opener

        def work(task: Task) -> tuple[list[tuple[_Deletion, bool]], bool]:
            """Send each request. Return each answered, with whether it is
            ended (delivered a second time, or refused as it is), and
            whether every request was answered."""
            answered: list[tuple[_Deletion, bool]] = []
            for item in due:
                if task.is_cancelled():
                    break
                body = json.dumps({"installation": item.installation}).encode("utf-8")
                try:
                    status = send(
                        USAGE_DELETE_URL, body, _headers(), opener=opener,
                        max_bytes=USAGE_ANSWER_MAX_BYTES, timeout=USAGE_SEND_TIMEOUT_S,
                    )
                except DownloadError as exc:
                    logger.info("Deletion request not delivered, to be tried again: %s", exc)
                    break
                if not _finished(status, "Deletion request"):
                    break
                accepted = _HTTP_OK_FIRST <= status <= _HTTP_OK_LAST
                answered.append((item, item.first_delivered is not None or not accepted))
            return answered, len(answered) == len(due)

        task = Task(work, (DownloadError,), self)
        task.succeeded.connect(self._on_deletions_answered)
        task.failed.connect(self._on_deletions_failed)
        task.cancelled.connect(self._on_deletions_failed)
        self._deletion_task = task
        task.start()

    def _on_deletions_answered(self, answer: object) -> None:
        self._deletion_task = None
        answered, complete = answer if isinstance(answer, tuple) else ([], False)
        if answered:
            now = self._now()
            self._update(lambda state: _note_answered(state, answered, now))
        # Those delivered once are next due a while later; one not
        # delivered is tried again in an hour.
        self._schedule_deletions(0 if complete else USAGE_DELETION_RETRY_S)

    def _on_deletions_failed(self, *_: object) -> None:
        self._deletion_task = None
        self._schedule_deletions(USAGE_DELETION_RETRY_S)


def _headers() -> dict[str, str]:
    return {"User-Agent": user_agent(), "Content-Type": "application/json"}


def _finished(status: int, what: str) -> bool:
    """Whether an answer means the request needs no further try."""
    if _HTTP_OK_FIRST <= status <= _HTTP_OK_LAST:
        return True
    if _HTTP_REFUSED_FIRST <= status <= _HTTP_REFUSED_LAST and status != _HTTP_TOO_MANY:
        # Refused as it is: sending it again would not help.
        logger.warning("%s refused by the server (%d)", what, status)
        return True
    logger.info("%s not accepted now (%d), to be tried again", what, status)
    return False


def _remove_waiting(state: _State, day: date, installation: object) -> bool:
    kept = [
        item for item in state.waiting
        if not (item.day == day and item.report["installation"] == installation)
    ]
    changed = len(kept) != len(state.waiting)
    state.waiting = kept
    return changed


def _sendable(name: str) -> str:
    """A name of the system as a report may hold it."""
    return name if _TOKEN.fullmatch(name) is not None else _OTHER


def _confirm_due_in(item: _Deletion, now: datetime) -> float:
    """In how many seconds ``item`` is next to be delivered: at once if it
    has not been yet, or if the clock was set back since it was."""
    if item.first_delivered is None or item.first_delivered > now:
        return 0
    waited = (now - item.first_delivered).total_seconds()
    return max(0.0, USAGE_DELETION_CONFIRM_AFTER_S - waited)


def _note_answered(state: _State, answered: list[tuple[_Deletion, bool]], now: datetime) -> bool:
    """Note the answers to deletion requests: an ended request is
    forgotten, a first delivery is remembered."""
    ended = {item.installation for item, done in answered if done}
    delivered = {item.installation for item, done in answered if not done}
    kept: list[_Deletion] = []
    for item in state.deletions:
        if item.installation in ended:
            continue
        if item.installation in delivered and item.first_delivered is None:
            item = _Deletion(item.installation, now)
        # Otherwise another window delivered it first in the meantime.
        kept.append(item)
    changed = kept != state.deletions
    state.deletions = kept
    return changed


def _listed(value: object) -> list[str]:
    """The names in a report's list field, as read back from the file."""
    return [str(item) for item in value] if isinstance(value, list) else []


def _added(names: set[str], name: str) -> bool:
    """Add ``name``; whether it was new."""
    if name in names:
        return False
    names.add(name)
    return True


def _raised(notes: _DayNotes, attribute: str, value: str, order: tuple[str, ...]) -> bool:
    """Set a range of ``notes`` to ``value`` if it is higher; whether it was."""
    if order.index(value) <= order.index(getattr(notes, attribute)):
        return False
    setattr(notes, attribute, value)
    return True
