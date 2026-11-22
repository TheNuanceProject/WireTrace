# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the privacy policy: its one file, read strictly, shown the
same everywhere, and held to what the code sends and keeps."""

from __future__ import annotations

import pytest

from app.constants import (
    DIAGNOSTIC_LOG_TOTAL_BYTES,
    UPDATE_CHANGE_HEADER,
    UPDATE_CHECK_INTERVAL_HOURS,
    UPDATE_LAST_CHECK_HEADER,
    UPDATE_LAST_VERSION_HEADER,
    UPDATE_UNKNOWN_UNTIL_HEADER,
    USAGE_DELETION_COMPLETE_DAYS,
    USAGE_RECORDS_KEPT_MONTHS,
)
from app.privacy_policy import (
    POLICY_FILENAME,
    PolicyError,
    find_policy,
    load_policy,
    parse_policy,
    to_html,
    to_plain_text,
)
from updater.version_change import prepare
from version import APP_AUTHOR, APP_DEVELOPER, APP_MAINTAINER_EMAIL

_HEAD = "# Privacy policy\n\nLast updated 19 November 2026\n\n## 1. Who we are\n\n"


def _policy(body: str) -> str:
    return _HEAD + body + "\n"


class TestTheFile:
    def test_is_found_at_the_top_of_the_source_tree(self):
        assert find_policy().name == POLICY_FILENAME
        assert (find_policy().parent / "LICENSE").is_file()

    def test_is_taken_whole(self):
        policy = load_policy()

        assert policy.title == "Privacy policy"
        assert [section.label for section in policy.sections][:2] == ["1. Who we are", "2. Summary"]
        assert policy.sections[-1].label == "13. Contact"


class TestReading:
    def test_paragraphs_lists_bold_and_links(self):
        policy = parse_policy(_policy(
            "One line\ngoes on.\n\n- **First:** item\n- [Second](https://example.org/a) item"
            "\n\nMail [a@b.c](mailto:a@b.c)."
        ))

        paragraph, items, mail = policy.sections[0].blocks
        assert not paragraph.is_list and [run.text for run in paragraph.texts[0]] == ["One line goes on."]
        assert items.is_list and len(items.texts) == 2
        assert items.texts[0][0].bold and items.texts[0][0].text == "First:"
        assert items.texts[1][0].url == "https://example.org/a"
        assert mail.texts[0][1].url == "mailto:a@b.c"

    @pytest.mark.parametrize(("markdown", "why"), [
        ("Phé text", "only ASCII"),
        ("Text\twith a tab", "control character"),
        ("Text ", "ends with a space"),
        ("A *single* star", "markup"),
        ("Some _emphasis_", "markup"),
        ("A `code` span", "markup"),
        ("An <b>element</b>", "markup"),
        ("A [plain](http://example.org) link", "markup"),
        ("A [script](javascript:alert) link", "markup"),
        ("> A quote", "markup"),
        ("### A smaller heading", "markup"),
        ("1. A numbered list", "markup"),
        ("* Another list", "markup"),
        ("- An item\nand a line that is not", "every line of a list"),
        ("## Heading\nwith text under it", "a line of its own"),
        ("A back\\slash", "markup"),
    ])
    def test_refuses_what_it_does_not_read(self, markdown, why):
        with pytest.raises(PolicyError, match=why):
            parse_policy(_policy(markdown))

    @pytest.mark.parametrize(("markdown", "why"), [
        ("Privacy policy\n\nLast updated 1 May 2026\n\n## A\n\nText\n", "the title"),
        ("# Privacy policy\n\nUpdated 1 May 2026\n\n## A\n\nText\n", "Last updated"),
        ("# Privacy policy\n\nLast updated 1 May 2026\n\nText first\n\n## A\n\nText\n", "before the first section"),
        ("# Privacy policy\n\nLast updated 1 May 2026\n\n## A\n\n## B\n\nText\n", "is empty"),
        ("# Privacy policy\r\n\r\nLast updated 1 May 2026\r\n\r\n## A\r\n\r\nText\r\n", "line feed"),
        ("# Privacy policy\n", "sections are needed"),
        ("# Privacy policy\n\nLast updated 1 May 2026\n\n## A\n\nText\x1f\n", "control character"),
        ("# \x1fPrivacy policy\n\nLast updated 1 May 2026\n\n## A\n\nText\n", "control character"),
        ("# Privacy policy\n\nLast updated 1 May 2026\n\n## A\n\n\x1cText\n", "control character"),
        ("# Privacy policy\n\nLast updated 1 May 2026\n\n## A\n\nA bell \x07 rings\n", "control character"),
    ])
    def test_refuses_a_policy_out_of_shape(self, markdown, why):
        with pytest.raises(PolicyError, match=why):
            parse_policy(markdown)


class TestShowing:
    def test_as_plain_text_a_link_shows_its_address_and_markup_never_shows(self):
        text = to_plain_text(parse_policy(_policy(
            "**Bold.** See [the page](https://example.org/p). Mail [a@b.c](mailto:a@b.c).\n\n- One\n- Two"
        )))

        assert text == (
            "Privacy policy\n\nLast updated 19 November 2026\n\n1. Who we are\n\n"
            "Bold. See the page (https://example.org/p). Mail a@b.c.\n\n- One\n- Two\n"
        )

    def test_as_html_everything_is_escaped(self):
        html = to_html(parse_policy(_policy('Tom & "Jerry" (GDPR)\n\n- [x & y](https://e.org/?a=1&b=2)')))

        assert html == (
            "<p>Last updated 19 November 2026</p>\n<h3>1. Who we are</h3>\n"
            "<p>Tom &amp; &quot;Jerry&quot; (GDPR)</p>\n"
            '<ul><li><a href="https://e.org/?a=1&amp;b=2">x &amp; y</a></li></ul>'
        )

    def test_the_real_policy_shows_no_markup_as_text(self):
        text = to_plain_text(load_policy())

        for mark in ("**", "](", "##", "[", "]"):
            assert mark not in text


#: The preferences' Updates section as each kind of check finds it: the
#: first check, an unchanged version, an unknown one, a change, and a
#: change after a check whose version was not recorded.
_EVERY_KIND_OF_CHECK = (
    {},
    {"last_check_day": "20000", "last_check_version": "2.3.0@20000"},
    {"last_check_day": "20000"},
    {"last_check_day": "20000", "last_check_version": "2.2.0@20000"},
    {"last_check_day": "20000", "last_check_version": "2.2.0@20000", "unknown_until": "19990"},
)


class TestHeldToTheCode:
    """What the policy says against what the code sends and keeps."""

    @pytest.fixture
    def text(self) -> str:
        return " ".join(to_plain_text(load_policy()).split())

    def test_names_who_publishes_and_how_to_reach_them(self, text):
        for stated in (APP_DEVELOPER, APP_AUTHOR, APP_MAINTAINER_EMAIL):
            assert stated in text

    def test_names_everything_an_update_check_sends(self, text):
        assert f"every {UPDATE_CHECK_INTERVAL_HOURS} hours" in text
        assert ("Each check states the WireTrace version, operating system, processor type, how "
                "WireTrace was installed, and the date of its previous check.") in text
        assert "A check contains no identifier" in text

    def test_names_every_header_a_check_can_send(self, text):
        # What each header a check sends is called in the policy. A header
        # added to the check fails here until the policy names it.
        named = {
            UPDATE_LAST_CHECK_HEADER: "the date of its previous check",
            UPDATE_LAST_VERSION_HEADER: "the version it changed from",
            UPDATE_CHANGE_HEADER: "a random number used only to count that change once, until one of them is counted",
            UPDATE_UNKNOWN_UNTIL_HEADER: "they also state the date of the last such check",
        }
        sent: set[str] = set()
        for stored in _EVERY_KIND_OF_CHECK:
            check, _ = prepare(stored, "2.3.0", make_token=lambda: "ab" * 16)
            sent.update(check.headers)

        assert sent == set(named)
        for header, words in named.items():
            assert words in text, header
        assert "by an update or by a return to an earlier version" in text

    def test_names_how_long_everything_is_kept(self, text):
        assert f"Usage statistics reports: {USAGE_RECORDS_KEPT_MONTHS} months" in text
        assert f"from all backups within {USAGE_DELETION_COMPLETE_DAYS} days" in text
        assert ("Records of version changes (the version changed from and to, their dates and the "
                "random number): 2 years.") in text

    def test_says_usage_statistics_are_off_and_the_installer_leaves_them_off(self, text):
        assert "Usage statistics are off by default, and the installer does not switch them on." in text
        assert "Usage statistics never reach GitHub." in text

    def test_says_the_diagnostic_log_stays_on_the_computer_within_its_size(self, text):
        assert DIAGNOSTIC_LOG_TOTAL_BYTES == 5 * 1024 * 1024
        assert "It takes at most 5 MB, removing its oldest files." in text
        assert ("It never contains anything your devices sent or anything you typed, "
                "and it is never sent to us.") in text
