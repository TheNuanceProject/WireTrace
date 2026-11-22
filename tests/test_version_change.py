# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of what an update check says of the check before it, and of what
is kept once a check is counted."""

from __future__ import annotations

import re

import pytest

from app.constants import (
    UPDATE_CHANGE_HEADER,
    UPDATE_CHANGE_TOKEN_BYTES,
    UPDATE_FIRST_CHECK_VALUE,
    UPDATE_LAST_CHECK_HEADER,
    UPDATE_LAST_VERSION_HEADER,
    UPDATE_UNKNOWN_UNTIL_HEADER,
)
from updater.version_change import (
    CHANGE,
    LAST_DAY,
    LAST_VERSION,
    UNKNOWN_UNTIL,
    Check,
    counted,
    new_token,
    prepare,
    unstated,
)

TOKEN = "3c" * UPDATE_CHANGE_TOKEN_BYTES
OTHER_TOKEN = "a5" * UPDATE_CHANGE_TOKEN_BYTES


def _token() -> str:
    return TOKEN


def _never() -> str:
    raise AssertionError("no number should be made")


class TestWhatACheckSends:
    def test_a_first_check_says_so_and_nothing_else(self):
        check, updates = prepare({}, "2.2.0", _never)

        assert check.headers == {UPDATE_LAST_CHECK_HEADER: UPDATE_FIRST_CHECK_VALUE}
        assert updates == {}
        assert check.previous_day == "" and check.token is None and not check.version_unknown

    def test_a_version_that_has_not_changed_sends_the_day_alone(self):
        stored = {LAST_DAY: "20771", LAST_VERSION: "2.2.0@20771"}

        check, updates = prepare(stored, "2.2.0", _never)

        assert check.headers == {UPDATE_LAST_CHECK_HEADER: "20771"}
        assert updates == {}
        assert not check.version_unknown

    def test_a_changed_version_names_the_earlier_one_with_a_new_number_kept(self):
        stored = {LAST_DAY: "20771", LAST_VERSION: "2.2.0@20771"}

        check, updates = prepare(stored, "2.3.0", _token)

        assert check.headers == {
            UPDATE_LAST_CHECK_HEADER: "20771",
            UPDATE_LAST_VERSION_HEADER: "2.2.0",
            UPDATE_CHANGE_HEADER: TOKEN,
        }
        assert updates == {CHANGE: f"{TOKEN} 2.2.0@20771"}
        assert check.token == TOKEN

    def test_the_number_kept_for_the_change_is_sent_again_until_counted(self):
        stored = {LAST_DAY: "20771", LAST_VERSION: "2.2.0@20771", CHANGE: f"{TOKEN} 2.2.0@20771"}

        check, updates = prepare(stored, "2.3.0", _never)

        assert check.headers[UPDATE_CHANGE_HEADER] == TOKEN
        assert updates == {}

    def test_a_later_version_before_any_answer_keeps_the_change_and_its_number(self):
        # The server moves it on from the version it last recorded.
        stored = {LAST_DAY: "20771", LAST_VERSION: "2.2.0@20771", CHANGE: f"{TOKEN} 2.2.0@20771"}

        check, _ = prepare(stored, "2.4.0", _never)

        assert check.headers[UPDATE_LAST_VERSION_HEADER] == "2.2.0"
        assert check.headers[UPDATE_CHANGE_HEADER] == TOKEN

    def test_back_to_the_earlier_version_before_any_answer_still_sends_the_number(self):
        # A check of the change may have been counted: the server is told
        # it is back.
        stored = {LAST_DAY: "20771", LAST_VERSION: "2.2.0@20771", CHANGE: f"{TOKEN} 2.2.0@20771"}

        check, _ = prepare(stored, "2.2.0", _never)

        assert check.headers[UPDATE_CHANGE_HEADER] == TOKEN
        assert check.headers[UPDATE_LAST_VERSION_HEADER] == "2.2.0"

    def test_a_number_kept_for_another_change_is_replaced(self):
        stored = {LAST_DAY: "20775", LAST_VERSION: "2.3.0@20775", CHANGE: f"{OTHER_TOKEN} 2.2.0@20771"}

        check, updates = prepare(stored, "2.4.0", _token)

        assert check.headers[UPDATE_CHANGE_HEADER] == TOKEN
        assert updates == {CHANGE: f"{TOKEN} 2.3.0@20775"}

    def test_a_number_kept_for_another_change_is_dropped_once_nothing_changes(self):
        stored = {LAST_DAY: "20775", LAST_VERSION: "2.3.0@20775", CHANGE: f"{OTHER_TOKEN} 2.2.0@20771"}

        check, updates = prepare(stored, "2.3.0", _never)

        assert UPDATE_CHANGE_HEADER not in check.headers
        assert updates == {CHANGE: ""}

    def test_names_the_last_day_of_unknown_version_with_a_change(self):
        stored = {LAST_DAY: "20771", LAST_VERSION: "2.2.0@20771", UNKNOWN_UNTIL: "20700"}

        check, _ = prepare(stored, "2.3.0", _token)

        assert check.headers[UPDATE_UNKNOWN_UNTIL_HEADER] == "20700"

    def test_never_names_that_day_without_a_change(self):
        stored = {LAST_DAY: "20771", LAST_VERSION: "2.2.0@20771", UNKNOWN_UNTIL: "20700"}

        check, _ = prepare(stored, "2.2.0", _never)

        assert check.headers == {UPDATE_LAST_CHECK_HEADER: "20771"}

    def test_a_day_written_without_its_version_states_no_change(self):
        # WireTrace 2.1 and earlier keep the day alone.
        check, updates = prepare({LAST_DAY: "20771"}, "2.2.0", _never)

        assert check.headers == {UPDATE_LAST_CHECK_HEADER: "20771"}
        assert check.version_unknown and check.previous_day == "20771"
        assert updates == {}

    def test_a_version_kept_for_another_day_states_no_change(self):
        # Since this version's check, an earlier one that keeps no version
        # checked and wrote its day.
        stored = {LAST_DAY: "20790", LAST_VERSION: "2.2.0@20771", CHANGE: f"{TOKEN} 2.2.0@20771"}

        check, updates = prepare(stored, "2.2.0", _never)

        assert check.headers == {UPDATE_LAST_CHECK_HEADER: "20790"}
        assert check.version_unknown
        assert updates == {CHANGE: ""}

    @pytest.mark.parametrize("value", ["2.2@20771", "2.2.0", "2.2.0@", "x@20771", "2.2.0@20771; a=b"])
    def test_a_kept_version_of_the_wrong_form_is_unknown(self, value):
        check, _ = prepare({LAST_DAY: "20771", LAST_VERSION: value}, "2.3.0", _never)

        assert check.version_unknown
        assert UPDATE_LAST_VERSION_HEADER not in check.headers

    @pytest.mark.parametrize("value", ["3C" * 16 + " 2.2.0@20771", "3c" * 15 + " 2.2.0@20771", TOKEN, TOKEN + " junk"])
    def test_a_kept_number_of_the_wrong_form_is_replaced(self, value):
        stored = {LAST_DAY: "20771", LAST_VERSION: "2.2.0@20771", CHANGE: value}

        check, _ = prepare(stored, "2.3.0", _token)

        assert check.headers[UPDATE_CHANGE_HEADER] == TOKEN


class TestUnstated:
    def test_a_change_not_stated_leaves_the_version_unknown(self):
        stored = {LAST_DAY: "20771", LAST_VERSION: "2.2.0@20771"}
        check, _ = prepare(stored, "2.3.0", _token)

        bare = unstated(check)

        assert bare.headers == {UPDATE_LAST_CHECK_HEADER: "20771"}
        assert bare.token is None and bare.version_unknown and bare.previous_day == "20771"

    def test_a_check_without_a_change_is_left_as_it_is(self):
        check = Check(headers={UPDATE_LAST_CHECK_HEADER: "20771"}, previous_day="20771")

        assert unstated(check) is check


class TestCounted:
    def test_keeps_the_day_with_the_version_that_made_the_check(self):
        check, _ = prepare({}, "2.2.0", _never)

        assert counted({}, check, "20771", "2.2.0") == {LAST_DAY: "20771", LAST_VERSION: "2.2.0@20771"}

    def test_deletes_the_number_it_sent(self):
        stored = {LAST_DAY: "20771", LAST_VERSION: "2.2.0@20771", CHANGE: f"{TOKEN} 2.2.0@20771"}
        check, _ = prepare(stored, "2.3.0", _never)

        updates = counted(stored, check, "20772", "2.3.0")

        assert updates == {LAST_DAY: "20772", LAST_VERSION: "2.3.0@20772", CHANGE: ""}

    def test_leaves_a_number_another_window_made_since(self):
        check = Check(headers={}, previous_day="20771", token=TOKEN)
        stored = {LAST_DAY: "20771", CHANGE: f"{OTHER_TOKEN} 2.3.0@20771"}

        assert CHANGE not in counted(stored, check, "20772", "2.3.0")

    def test_keeps_a_previous_day_of_unknown_version(self):
        check, _ = prepare({LAST_DAY: "20771"}, "2.2.0", _never)

        updates = counted({LAST_DAY: "20771"}, check, "20772", "2.2.0")

        assert updates == {LAST_DAY: "20772", LAST_VERSION: "2.2.0@20772", UNKNOWN_UNTIL: "20771"}

    def test_never_moves_the_last_day_of_unknown_version_back(self):
        check = Check(headers={}, previous_day="20700", version_unknown=True)

        updates = counted({UNKNOWN_UNTIL: "20750"}, check, "20772", "2.2.0")

        assert updates[UNKNOWN_UNTIL] == "20750"

    def test_writes_nothing_over_a_later_counted_check(self):
        check, _ = prepare({}, "2.2.0", _never)

        assert counted({LAST_DAY: "20773", LAST_VERSION: "2.2.0@20773"}, check, "20772", "2.2.0") == {}


def test_a_number_is_fresh_and_of_its_form():
    numbers = {new_token() for _ in range(64)}

    assert len(numbers) == 64
    assert all(re.fullmatch(rf"[0-9a-f]{{{2 * UPDATE_CHANGE_TOKEN_BYTES}}}", number) for number in numbers)
