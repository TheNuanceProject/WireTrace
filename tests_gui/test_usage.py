# SPDX-License-Identifier: GPL-3.0-or-later
"""Usage statistics: what is noted, what a report holds, and when it is sent.

The statistics are off unless switched on, a report holds exactly the
fields the privacy statement names, notes survive a sudden end and are
shared safely by several windows, a run that ends without closing is
noticed, a report that cannot be sent waits without ever getting in the
way, and switching off deletes everything here and asks the server to
delete everything there.
"""

from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

import pytest
from PySide6.QtWidgets import QApplication

import updater.usage as usage_module
from app.config import ConfigManager
from app.constants import (
    USAGE_DAY_CHANGE_DELAY_S,
    USAGE_DELETION_CONFIRM_AFTER_S,
    USAGE_DELETION_RETRY_S,
    USAGE_REPORT_MAX_AGE_DAYS,
    USAGE_SEND_SPACING_MIN_S,
    DisplayMode,
    ThemeID,
)
from app.file_lock import FileLock
from tests_gui.support import LoopbackPort, wait_until
from ui.device_tab import DeviceTab
from ui.main_window import MainWindow
from ui.themes.theme_manager import ThemeManager
from ui.widgets.banner import BannerArea
from ui.widgets.connect_page import ConnectPage
from updater.privacy import PRIVACY_STATEMENT
from updater.usage import (
    REPORT_FIELDS,
    Feature,
    Problem,
    UsageStatistics,
    connections_range,
    line_rate_range,
    nearest_scale,
    session_range,
    starts_range,
)
from version import APP_VERSION, USAGE_DELETE_URL, USAGE_REPORT_URL

_DAY_ONE = datetime(2026, 10, 13, 9, 30)
_DAY_TWO = _DAY_ONE + timedelta(days=1)
_IDENTIFIER = re.compile(r"[0-9a-f]{32}")


class Clock:
    def __init__(self) -> None:
        self.now = _DAY_ONE

    def __call__(self) -> datetime:
        return self.now


class _Answer(io.BytesIO):
    def __init__(self, status: int) -> None:
        super().__init__(b"")
        self.status = status


class ReportServer:
    """Receives reports and deletion requests. ``status`` is what it
    answers; ``error`` is raised instead of answering, as when the
    network is down."""

    def __init__(self) -> None:
        self.requests: list[urllib.request.Request] = []
        self.status = 204
        self.error: Exception | None = None

    def open(self, request: urllib.request.Request, timeout: float) -> _Answer:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        if self.status >= 400:
            raise urllib.error.HTTPError(request.full_url, self.status, "refused", {}, None)
        return _Answer(self.status)

    def bodies(self, url: str) -> list[dict[str, object]]:
        return [json.loads(request.data) for request in self.requests if request.full_url == url]

    def reports(self) -> list[dict[str, object]]:
        return self.bodies(USAGE_REPORT_URL)

    def deletions(self) -> list[dict[str, object]]:
        return self.bodies(USAGE_DELETE_URL)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def server() -> ReportServer:
    return ReportServer()


@pytest.fixture
def make_usage(qapp: QApplication, config: ConfigManager, clock: Clock, server: ReportServer):
    made: list[UsageStatistics] = []

    def build(**options: object) -> UsageStatistics:
        # Reports follow one another almost at once, unless a test sets
        # the wait itself.
        options.setdefault("spacing", lambda: 0.01)
        statistics = UsageStatistics(
            config, opener=server, now=clock, device_pixel_ratio=lambda: 1.25, **options,
        )
        made.append(statistics)
        return statistics

    yield build
    for statistics in made:
        statistics.stop()


@pytest.fixture
def usage(make_usage) -> UsageStatistics:
    return make_usage()


def _stored(statistics: UsageStatistics) -> dict:
    return json.loads(statistics.path.read_text(encoding="utf-8"))


def _send_now(qapp: QApplication, statistics: UsageStatistics, server: ReportServer, count: int):
    statistics._send_next_report()
    wait_until(qapp, lambda: len(server.requests) >= count and not statistics.sending)
    qapp.processEvents()


def _deliver_deletions(qapp: QApplication, statistics: UsageStatistics, server: ReportServer, count: int):
    statistics._send_deletions()
    wait_until(qapp, lambda: len(server.requests) >= count and not statistics.sending)
    qapp.processEvents()


def _a_day_waiting(statistics: UsageStatistics, clock: Clock) -> None:
    statistics.set_enabled(True)
    statistics.note_feature(Feature.CSV)
    clock.now = _DAY_TWO
    statistics.note_connection()


# ── Off unless switched on ───────────────────────────────────────────────


class TestOffUnlessSwitchedOn:
    def test_off_by_default_notes_and_keeps_nothing(self, usage, config):
        assert config.usage_statistics is False

        usage.start()
        usage.note_feature(Feature.LOG)
        usage.note_problem(Problem.CONNECTION_LOST)

        assert not usage.path.exists()
        assert usage.installation is None

    def test_starting_while_off_removes_a_file_left_behind(self, usage):
        usage.path.parent.mkdir(parents=True, exist_ok=True)
        usage.path.write_text("{}", encoding="utf-8")

        usage.start()

        assert not usage.path.exists()

    def test_switching_on_makes_an_identifier_and_begins_the_day(self, usage, config):
        usage.set_enabled(True)

        assert config.usage_statistics is True
        assert ConfigManager().usage_statistics is True
        stored = _stored(usage)
        assert _IDENTIFIER.fullmatch(stored["installation"])
        assert usage.installation == stored["installation"]
        assert stored["notes"]["day"] == _DAY_ONE.date().isoformat()
        assert stored["notes"]["version"] == APP_VERSION
        assert stored["notes"]["starts"] == 1

    def test_the_identifier_is_kept_from_start_to_start(self, make_usage):
        first = make_usage()
        first.set_enabled(True)
        made = first.installation
        first.stop()

        second = make_usage()
        second.start()

        assert second.installation == made

    def test_switching_off_forgets_everything_but_the_deletion_to_ask_for(self, usage, clock):
        _a_day_waiting(usage, clock)
        made = usage.installation

        usage.set_enabled(False)

        stored = _stored(usage)
        assert stored["installation"] is None
        assert stored["notes"] is None
        assert stored["waiting"] == []
        assert stored["runs"] == {}
        assert stored["deletions"] == [{"installation": made, "first_delivered": None}]
        assert usage.installation is None
        assert not any(usage.runs_dir.glob("*.lock"))

    def test_switching_on_again_makes_a_new_identifier(self, usage):
        usage.set_enabled(True)
        first = usage.installation
        usage.set_enabled(False)

        usage.set_enabled(True)

        assert usage.installation not in (None, first)
        assert _stored(usage)["deletions"] == [{"installation": first, "first_delivered": None}]


# ── What a report holds ──────────────────────────────────────────────────


class TestTheReport:
    def test_a_day_becomes_one_report_with_exactly_the_stated_fields(self, usage, clock):
        usage.set_enabled(True)
        usage.note_feature(Feature.LOG)
        usage.note_feature(Feature.LOG)
        usage.note_feature(Feature.HEX)
        usage.note_problem(Problem.LINES_LOST)
        usage.note_line_rate(250)
        usage.note_line_rate(40)
        for _ in range(3):
            usage.note_connection()

        clock.now = _DAY_TWO
        usage.note_feature(Feature.FIND)

        (waiting,) = _stored(usage)["waiting"]
        assert waiting["day"] == _DAY_ONE.date().isoformat()
        assert waiting["report"] == {
            "installation": usage.installation,
            "day": "2026-10-13",
            "version": APP_VERSION,
            "os": usage_module.system_name(),
            "arch": usage_module.architecture(),
            "channel": usage_module.install_channel(),
            "features": ["hex", "log"],
            "problems": ["lines_lost"],
            "peak_line_rate": "under_1000",
            "connections": "2_to_5",
            "theme": "system",
            "scale": 125,
            "longest_session": "under_15_minutes",
            "starts": "1",
        }
        assert tuple(waiting["report"]) == REPORT_FIELDS

    def test_a_day_left_open_from_the_day_before_has_no_start(self, usage, clock):
        usage.set_enabled(True)
        clock.now = _DAY_TWO
        usage._on_day_changed()
        clock.now = _DAY_TWO + timedelta(days=1)
        usage.note_connection()

        reports = [item["report"] for item in _stored(usage)["waiting"]]
        assert [report["starts"] for report in reports] == ["1", "none"]

    def test_the_longest_time_open_is_noted_as_it_passes_each_range(self, usage, clock):
        usage.set_enabled(True)
        assert usage._session.interval() == 15 * 60 * 1000

        clock.now = _DAY_ONE + timedelta(minutes=15)
        usage._on_session_threshold()
        assert _stored(usage)["notes"]["longest_session"] == "15_to_60_minutes"
        assert usage._session.interval() == 45 * 60 * 1000

        clock.now = _DAY_ONE + timedelta(hours=8)
        usage._on_session_threshold()
        assert _stored(usage)["notes"]["longest_session"] == "8_hours_or_more"
        assert not usage._session.isActive()

    def test_time_open_on_a_new_day_counts_from_midnight(self, usage, clock):
        clock.now = datetime(2026, 10, 13, 20, 0)
        usage.set_enabled(True)
        clock.now = datetime(2026, 10, 14, 0, 5)

        usage._on_day_changed()

        assert usage._session.interval() == 10 * 60 * 1000

    def test_a_day_with_no_use_of_any_feature_is_still_a_day_of_use(self, usage, clock):
        usage.set_enabled(True)
        clock.now = _DAY_TWO
        usage.note_connection()

        report = _stored(usage)["waiting"][0]["report"]
        assert report["features"] == []
        assert report["peak_line_rate"] == "none"
        assert report["connections"] == "none"

    def test_nothing_identifying_is_ever_kept(self, usage, clock):
        usage.set_enabled(True)
        usage.note_feature(Feature.SAVED_COMMANDS)
        clock.now = _DAY_TWO
        usage.note_connection()

        text = usage.path.read_text(encoding="utf-8")
        report = _stored(usage)["waiting"][0]["report"]
        assert set(report) == set(REPORT_FIELDS)
        for value in report.values():
            assert isinstance(value, (str, int, list))
        assert "PORT" not in text and "COM" not in text

    def test_the_theme_and_scale_of_the_day_are_noted(self, usage, config, clock):
        usage.set_enabled(True)
        config.theme = ThemeID.DARK.value
        usage.note_feature(Feature.LOG)
        clock.now = _DAY_TWO
        usage.note_connection()

        report = _stored(usage)["waiting"][0]["report"]
        assert (report["theme"], report["scale"]) == ("dark", 125)

    @pytest.mark.parametrize(
        ("rate", "expected"),
        [(0, "none"), (1, "under_100"), (99, "under_100"), (100, "under_1000"),
         (9_999, "under_10000"), (10_000, "10000_or_more"), (250_000, "10000_or_more")],
    )
    def test_line_rate_ranges(self, rate, expected):
        assert line_rate_range(rate) == expected

    @pytest.mark.parametrize(
        ("count", "expected"),
        [(0, "none"), (1, "1"), (2, "2_to_5"), (5, "2_to_5"), (6, "6_to_20"),
         (20, "6_to_20"), (21, "more_than_20"), (500, "more_than_20")],
    )
    def test_connection_ranges(self, count, expected):
        assert connections_range(count) == expected

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "under_15_minutes"), (899, "under_15_minutes"), (900, "15_to_60_minutes"),
         (3_600, "1_to_4_hours"), (14_400, "4_to_8_hours"), (28_799, "4_to_8_hours"),
         (28_800, "8_hours_or_more")],
    )
    def test_session_ranges(self, seconds, expected):
        assert session_range(seconds) == expected

    @pytest.mark.parametrize(
        ("count", "expected"),
        [(0, "none"), (1, "1"), (2, "2_to_3"), (3, "2_to_3"), (4, "4_to_10"),
         (10, "4_to_10"), (11, "more_than_10")],
    )
    def test_start_ranges(self, count, expected):
        assert starts_range(count) == expected

    @pytest.mark.parametrize(
        ("ratio", "expected"), [(1.0, 100), (1.1, 100), (1.2, 125), (1.5, 150), (1.75, 150), (3.0, 200)],
    )
    def test_screen_scale_is_the_nearest_listed(self, ratio, expected):
        assert nearest_scale(ratio) == expected

    def test_the_privacy_statement_names_every_field_a_report_holds(self):
        stated = {
            "installation": "a random installation identifier",
            "day": "the date of the day it describes",
            "version": "the version of WireTrace",
            "os": "the operating system",
            "arch": "the processor type",
            "channel": "how WireTrace was installed",
            "features": "which features were used",
            "problems": "whether lines were lost",
            "peak_line_rate": "the highest line rate",
            "connections": "the number of connections opened",
            "theme": "the theme",
            "scale": "the display scale",
            "longest_session": "the longest time one window stayed open",
            "starts": "how many times WireTrace was started",
        }
        statement = " ".join(PRIVACY_STATEMENT.split())
        assert set(stated) == set(REPORT_FIELDS)
        for field_name, phrase in stated.items():
            assert phrase in statement, field_name
        for problem in ("a log could not be written", "a connection was lost", "WireTrace closed unexpectedly"):
            assert problem in statement


# ── Kept safe on disk, and shared by several windows ─────────────────────


class TestKeptSafe:
    def test_notes_survive_a_sudden_end(self, make_usage):
        first = make_usage()
        first.set_enabled(True)
        first.note_feature(Feature.EXPORT)
        first.note_problem(Problem.LOG_WRITE_FAILED)

        second = make_usage()
        second.start()

        assert set(_stored(second)["notes"]["features"]) == {"export"}
        assert set(_stored(second)["notes"]["problems"]) == {"log_write_failed"}

    def test_two_open_windows_keep_what_each_notes(self, make_usage):
        first, second = make_usage(), make_usage()
        first.set_enabled(True)
        second.start()

        first.note_feature(Feature.EXPORT)
        second.note_feature(Feature.HEX)
        first.note_connection()
        second.note_connection()
        first.note_line_rate(50)
        second.note_line_rate(5_000)

        notes = _stored(first)["notes"]
        assert set(notes["features"]) == {"export", "hex"}
        assert notes["connections"] == 2
        assert notes["peak_line_rate"] == "under_10000"
        assert notes["starts"] == 2
        assert first.installation == second.installation

    def test_switched_off_in_one_window_stops_the_others_without_a_new_identifier(self, make_usage):
        first, second = make_usage(), make_usage()
        first.set_enabled(True)
        second.start()

        first.set_enabled(False)
        second.note_feature(Feature.PLOT)

        stored = _stored(first)
        assert stored["installation"] is None
        assert stored["notes"] is None
        assert second.installation is None

    def test_the_file_is_written_only_when_something_new_is_noted(self, usage, monkeypatch):
        usage.set_enabled(True)
        writes: list[str] = []
        real_write = usage_module.atomic_write_text
        monkeypatch.setattr(
            usage_module, "atomic_write_text",
            lambda path, text: (writes.append(path), real_write(path, text)),
        )

        usage.note_feature(Feature.FILTER)
        for _ in range(50):
            usage.note_feature(Feature.FILTER)
            usage.note_line_rate(10)

        assert len(writes) == 2

    @pytest.mark.parametrize(
        "content",
        [
            "not json",
            "[]",
            '{"format": 1}',
            '{"format": 2, "installation": "0F0F", "notes": null, "waiting": [], "deletions": [], "runs": {}}',
            '{"format": 2, "installation": null, "notes": {"day": "2026-10-13", "features": ["port COM7"]},'
            ' "waiting": [], "deletions": [], "runs": {}}',
            '{"format": 2, "installation": null, "notes": null,'
            ' "waiting": [{"day": "2026-10-12", "report": {"id": 1}}], "deletions": [], "runs": {}}',
            '{"format": 2, "installation": null, "notes": null, "waiting": [], "deletions": ["me"], "runs": {}}',
            '{"format": 2, "installation": null, "notes": null, "waiting": [], "runs": {},'
            ' "deletions": [{"installation": "00112233445566778899aabbccddeeff"}]}',
            '{"format": 2, "installation": null, "notes": null, "waiting": [], "runs": {},'
            ' "deletions": [{"installation": "00112233445566778899aabbccddeeff", "first_delivered": "soon"}]}',
            '{"format": 2, "installation": null, "notes": null, "waiting": [], "deletions": [],'
            ' "runs": {"../../etc": "2026-10-13"}}',
        ],
    )
    def test_a_damaged_or_altered_file_starts_again_and_never_fails(self, usage, config, content):
        config.usage_statistics = True
        usage.path.parent.mkdir(parents=True, exist_ok=True)
        usage.path.write_text(content, encoding="utf-8")

        usage.start()

        stored = _stored(usage)
        assert _IDENTIFIER.fullmatch(stored["installation"])
        assert stored["waiting"] == []
        assert stored["deletions"] == []
        assert stored["notes"]["features"] == []

    @pytest.mark.parametrize("name", ["", "a processor of the future", "x" * 33])
    def test_a_system_name_a_report_cannot_hold_is_noted_as_other(self, usage, monkeypatch, name):
        monkeypatch.setattr(usage_module, "architecture", lambda: name)
        monkeypatch.setattr(usage_module, "system_name", lambda: name)
        usage.set_enabled(True)
        made = usage.installation

        usage.note_feature(Feature.HEX)

        stored = _stored(usage)
        assert (stored["notes"]["os"], stored["notes"]["arch"]) == ("other", "other")
        assert stored["notes"]["features"] == ["hex"]
        assert usage.installation == made


class TestARunThatEndedWithoutClosing:
    def _crashed_run(self, statistics: UsageStatistics, day: date) -> None:
        """Leave a run listed whose lock no process holds, as a crash does."""
        stored = _stored(statistics)
        stored["runs"]["00112233445566ff"] = day.isoformat()
        statistics.path.write_text(json.dumps(stored), encoding="utf-8")
        (statistics.runs_dir / "00112233445566ff.lock").touch()

    def test_is_noted_on_the_day_it_last_noted(self, make_usage, clock):
        first = make_usage()
        first.set_enabled(True)
        first.note_feature(Feature.LOG)
        self._crashed_run(first, _DAY_ONE.date())
        first.stop()
        clock.now = _DAY_TWO

        second = make_usage()
        second.start()

        (waiting,) = _stored(second)["waiting"]
        assert waiting["report"]["problems"] == ["closed_unexpectedly"]
        assert "00112233445566ff" not in _stored(second)["runs"]
        assert not (second.runs_dir / "00112233445566ff.lock").exists()

    def test_a_normal_close_is_not_taken_for_one(self, make_usage):
        first = make_usage()
        first.set_enabled(True)
        first.stop()

        second = make_usage()
        second.start()

        assert _stored(second)["notes"]["problems"] == []
        assert list(_stored(second)["runs"]) == [second._run]

    def test_another_window_still_open_is_not_taken_for_one(self, make_usage):
        first = make_usage()
        first.set_enabled(True)

        second = make_usage()
        second.start()

        assert _stored(second)["notes"]["problems"] == []
        assert set(_stored(second)["runs"]) == {first._run, second._run}

    def test_a_lock_held_by_a_live_process_keeps_its_run(self, make_usage):
        first = make_usage()
        first.set_enabled(True)
        self._crashed_run(first, _DAY_ONE.date())
        held = FileLock(first.runs_dir / "00112233445566ff.lock")
        assert held.try_acquire()
        try:
            second = make_usage()
            second.start()
        finally:
            held.release()

        assert "00112233445566ff" in _stored(second)["runs"]


# ── Sending ──────────────────────────────────────────────────────────────


class TestSending:
    def test_a_waiting_report_is_sent_once_and_then_forgotten(self, qapp, usage, clock, server):
        _a_day_waiting(usage, clock)

        _send_now(qapp, usage, server, 1)

        (request,) = server.requests
        assert request.full_url == USAGE_REPORT_URL
        assert request.get_method() == "POST"
        assert {name.lower() for name, _ in request.header_items()} == {"user-agent", "content-type"}
        (report,) = server.reports()
        assert report["features"] == ["csv"]
        assert report["installation"] == usage.installation
        assert report["day"] == "2026-10-13"
        assert usage.waiting_days() == []
        assert _stored(usage)["waiting"] == []

    def test_waiting_reports_are_sent_one_at_a_time(self, qapp, make_usage, clock, server):
        # The shortest real wait: one the test cannot outlast, however
        # slowly it runs.
        usage = make_usage(spacing=lambda: USAGE_SEND_SPACING_MIN_S)
        _a_day_waiting(usage, clock)
        clock.now = _DAY_TWO + timedelta(days=1)
        usage.note_connection()
        assert len(usage.waiting_days()) == 2

        _send_now(qapp, usage, server, 1)
        assert len(server.requests) == 1
        assert usage._send_soon.isActive()
        assert usage._send_soon.interval() == USAGE_SEND_SPACING_MIN_S * 1000

        # The wait is over.
        usage._send_soon.timeout.emit()
        wait_until(qapp, lambda: len(server.requests) >= 2 and not usage.sending)
        qapp.processEvents()

        assert [report["day"] for report in server.reports()] == ["2026-10-13", "2026-10-14"]
        assert usage.waiting_days() == []

    @pytest.mark.parametrize("failure", ["unreachable", "server error", "too many"])
    def test_a_report_that_could_not_be_sent_waits_for_the_next_try(
        self, qapp, usage, clock, server, failure,
    ):
        _a_day_waiting(usage, clock)
        if failure == "unreachable":
            server.error = OSError("network is down")
        else:
            server.status = 503 if failure == "server error" else 429

        _send_now(qapp, usage, server, 1)

        assert usage.waiting_days() == [_DAY_ONE.date()]

    def test_a_report_the_server_refuses_is_not_sent_again(self, qapp, usage, clock, server):
        _a_day_waiting(usage, clock)
        server.status = 400

        _send_now(qapp, usage, server, 1)

        assert usage.waiting_days() == []

    def test_a_report_too_old_is_dropped_unsent(self, qapp, usage, clock, server):
        _a_day_waiting(usage, clock)
        clock.now = _DAY_TWO + timedelta(days=USAGE_REPORT_MAX_AGE_DAYS + 1)

        usage._send_next_report()

        assert server.requests == []
        assert usage.waiting_days() == []

    def test_the_day_change_is_awaited_shortly_after_midnight(self, usage, clock):
        clock.now = datetime(2026, 10, 13, 23, 59, 0)
        usage.set_enabled(True)

        assert usage._day_change.isActive()
        assert usage._day_change.interval() == (60 + USAGE_DAY_CHANGE_DELAY_S) * 1000

    def test_a_day_change_while_open_sends_the_day_before(self, qapp, usage, clock, server):
        usage.set_enabled(True)
        usage.note_feature(Feature.PLOT)
        clock.now = _DAY_TWO

        usage._on_day_changed()
        wait_until(qapp, lambda: server.requests and not usage.sending)
        qapp.processEvents()

        assert server.reports()[0]["features"] == ["plot"]
        assert _stored(usage)["notes"]["day"] == date(2026, 10, 14).isoformat()


class TestTheDeletionRequest:
    def test_is_delivered_twice_after_switching_off_and_then_nothing_is_left(self, qapp, usage, clock, server):
        """The second delivery deletes a report that was already on its
        way when statistics were switched off, and reached the server
        after the first."""
        _a_day_waiting(usage, clock)
        made = usage.installation
        usage.set_enabled(False)

        _deliver_deletions(qapp, usage, server, 1)

        assert server.deletions() == [{"installation": made}]
        assert server.reports() == []
        assert _stored(usage)["deletions"] == [{"installation": made, "first_delivered": clock.now.isoformat()}]
        assert usage._deletion_retry.isActive()
        assert usage._deletion_retry.interval() == USAGE_DELETION_CONFIRM_AFTER_S * 1000

        clock.now += timedelta(seconds=USAGE_DELETION_CONFIRM_AFTER_S)
        _deliver_deletions(qapp, usage, server, 2)

        assert server.deletions() == [{"installation": made}, {"installation": made}]
        assert not usage.path.exists()
        assert not usage._deletion_retry.isActive()

    def test_is_not_delivered_the_second_time_too_soon(self, qapp, usage, clock, server):
        usage.set_enabled(True)
        usage.set_enabled(False)
        _deliver_deletions(qapp, usage, server, 1)

        clock.now += timedelta(seconds=USAGE_DELETION_CONFIRM_AFTER_S - 1)
        usage._send_deletions()
        qapp.processEvents()

        assert len(server.deletions()) == 1
        assert usage.pending_deletions() != []
        assert usage._deletion_retry.interval() == 1000

    @pytest.mark.parametrize("failure", ["unreachable", "server error"])
    def test_is_kept_and_tried_again_when_not_delivered(self, qapp, usage, server, failure):
        usage.set_enabled(True)
        usage.set_enabled(False)
        if failure == "unreachable":
            server.error = OSError("network is down")
        else:
            server.status = 503

        _deliver_deletions(qapp, usage, server, 1)

        assert len(usage.pending_deletions()) == 1
        assert usage._deletion_retry.isActive()
        assert usage._deletion_retry.interval() == USAGE_DELETION_RETRY_S * 1000

    def test_is_tried_again_at_the_next_start(self, qapp, make_usage, server):
        first = make_usage()
        first.set_enabled(True)
        server.error = OSError("network is down")
        first.set_enabled(False)
        _deliver_deletions(qapp, first, server, 1)
        first.stop()
        server.error = None

        second = make_usage()
        second.start()
        _deliver_deletions(qapp, second, server, 2)

        assert len(server.deletions()) == 2
        assert _stored(second)["deletions"][0]["first_delivered"] is not None

    def test_a_request_the_server_refuses_is_not_sent_again(self, qapp, usage, server):
        usage.set_enabled(True)
        usage.set_enabled(False)
        server.status = 400

        _deliver_deletions(qapp, usage, server, 1)

        assert usage.pending_deletions() == []


# ── The question, and where the notes come from ──────────────────────────


@pytest.fixture
def port() -> LoopbackPort:
    return LoopbackPort()


@pytest.fixture
def window(qapp, config, theme_manager: ThemeManager, clock, server, port):
    theme_manager.apply_theme(ThemeID.LIGHT)
    statistics = UsageStatistics(config, opener=server, now=clock, device_pixel_ratio=lambda: 1.0)
    main_window = MainWindow(config, theme_manager, transport_factory=port.factory, usage=statistics)
    main_window.show()
    qapp.processEvents()
    yield main_window
    main_window.shutdown_all_tabs()


def _question(window: MainWindow):
    return window.findChild(BannerArea)._banners.get("usage")


def _press(banner, text: str) -> None:
    next(button for button in banner.action_buttons if button.text() == text).click()


class TestTheQuestion:
    def test_it_asks_once_with_a_way_to_see_what_is_sent(self, window, config):
        window.ask_about_usage_statistics()

        banner = _question(window)
        assert banner.title_label.text() == "Help improve WireTrace?"
        assert [button.text() for button in banner.action_buttons] == [
            "What is sent", "Share", "No thanks",
        ]
        assert config.usage_question_shown == 1

    def test_share_switches_the_statistics_on(self, window, config):
        window.ask_about_usage_statistics()

        _press(_question(window), "Share")

        assert config.usage_statistics is True
        assert config.usage_question_answered is True
        assert _question(window) is None
        assert window.usage.path.exists()

    @pytest.mark.parametrize("answer", ["No thanks", "close"])
    def test_declining_or_closing_leaves_them_off_for_good(self, window, config, answer):
        window.ask_about_usage_statistics()
        banner = _question(window)

        if answer == "close":
            banner.close_button.click()
        else:
            _press(banner, answer)

        assert config.usage_statistics is False
        assert config.usage_question_answered is True
        window.ask_about_usage_statistics()
        assert _question(window) is None

    def test_unanswered_it_is_asked_on_one_more_start_only(self, window, config):
        for _ in range(2):
            window.ask_about_usage_statistics()
            window.findChild(BannerArea).clear("usage")

        window.ask_about_usage_statistics()

        assert _question(window) is None
        assert config.usage_question_shown == 2


class TestWhatIsNoted:
    def test_connections_tabs_hex_and_filter_are_noted(self, qapp, window, clock):
        window.usage.set_enabled(True)
        tab: DeviceTab = window.current_tab()
        tab.findChild(ConnectPage).connect_requested.emit("PORT0", 115200)
        wait_until(qapp, lambda: tab.is_connected)

        tab.set_display_mode(DisplayMode.HEX)
        tab._on_filter_changed("temp")
        window.new_tab()

        notes = _stored(window.usage)["notes"]
        assert {"hex", "filter", "tabs"} <= set(notes["features"])
        assert notes["connections"] == 1

    def test_a_lost_connection_is_noted_as_a_problem(self, qapp, window, port):
        window.usage.set_enabled(True)
        tab: DeviceTab = window.current_tab()
        tab.findChild(ConnectPage).connect_requested.emit("PORT0", 115200)
        wait_until(qapp, lambda: tab.is_connected)

        port.transport.drop()
        wait_until(qapp, lambda: not tab.is_connected)

        assert "connection_lost" in _stored(window.usage)["notes"]["problems"]
