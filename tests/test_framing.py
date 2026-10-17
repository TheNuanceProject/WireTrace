# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for core.framing: line assembly, hex rows, and line decoding."""

from __future__ import annotations

import random

import pytest

from core.framing import (
    HexFramer,
    LineFramer,
    decode_line,
    escape_control_characters,
    hex_row_marks,
)


class TestLineFramerTerminators:
    @pytest.mark.parametrize(
        ("stream", "expected"),
        [
            (b"one\ntwo\n", [b"one", b"two"]),
            (b"one\r\ntwo\r\n", [b"one", b"two"]),
            (b"one\rtwo\r", [b"one", b"two"]),
            (b"one\n\ntwo\n", [b"one", b"", b"two"]),
            (b"one\r\n\r\ntwo\r\n", [b"one", b"", b"two"]),
            (b"mixed\nline\r\nends\r", [b"mixed", b"line", b"ends"]),
        ],
    )
    def test_each_terminator_ends_one_line(self, stream, expected):
        assert LineFramer().feed(stream) == expected

    def test_form_feed_and_vertical_tab_do_not_end_a_line(self):
        assert LineFramer().feed(b"a\x0bb\x0cc\n") == [b"a\x0bb\x0cc"]

    def test_unterminated_tail_is_kept_until_completed(self):
        framer = LineFramer()
        assert framer.feed(b"par") == []
        assert framer.feed(b"tial\nnext") == [b"partial"]
        assert framer.flush() == [b"next"]
        assert framer.flush() == []

    def test_crlf_split_across_reads_is_one_line_ending(self):
        framer = LineFramer()
        assert framer.feed(b"one\r") == [b"one"]
        assert framer.feed(b"\ntwo\r\n") == [b"two"]

    def test_cr_then_new_data_is_not_swallowed(self):
        framer = LineFramer()
        assert framer.feed(b"one\r") == [b"one"]
        assert framer.feed(b"two\r") == [b"two"]

    def test_lone_lf_after_cr_read_yields_nothing(self):
        framer = LineFramer()
        assert framer.feed(b"one\r") == [b"one"]
        assert framer.feed(b"\n") == []
        assert framer.feed(b"two\n") == [b"two"]

    def test_empty_feed_keeps_pending_cr_state(self):
        framer = LineFramer()
        framer.feed(b"one\r")
        assert framer.feed(b"") == []
        assert framer.feed(b"\ntwo\n") == [b"two"]

    def test_any_chunking_gives_the_same_lines(self):
        stream = b"alpha\r\nbeta\n\rgamma\r\r\ndelta\n\n" * 20
        expected = LineFramer().feed(stream)
        rng = random.Random(7)
        for _ in range(50):
            framer = LineFramer()
            lines: list[bytes] = []
            position = 0
            while position < len(stream):
                size = rng.randint(1, 9)
                lines.extend(framer.feed(stream[position:position + size]))
                position += size
            lines.extend(framer.flush())
            assert lines == expected


class TestLineFramerLongLines:
    def test_stream_without_terminator_is_emitted_in_pieces(self):
        framer = LineFramer(max_line_bytes=8)
        assert framer.feed(b"x" * 20) == [b"x" * 8, b"x" * 8]
        assert framer.flush() == [b"x" * 4]

    def test_memory_stays_bounded_without_terminator(self):
        framer = LineFramer(max_line_bytes=16)
        emitted = 0
        for _ in range(1000):
            emitted += sum(len(piece) for piece in framer.feed(b"y" * 10))
        remainder = sum(len(piece) for piece in framer.flush())
        assert remainder < 16
        assert emitted + remainder == 10_000

    def test_terminator_after_exact_multiple_adds_no_blank_line(self):
        framer = LineFramer(max_line_bytes=4)
        assert framer.feed(b"abcdefgh") == [b"abcd", b"efgh"]
        assert framer.feed(b"\nnext\n") == [b"next"]

    def test_tail_after_split_is_emitted_at_terminator(self):
        framer = LineFramer(max_line_bytes=4)
        assert framer.feed(b"abcdef") == [b"abcd"]
        assert framer.feed(b"\n\n") == [b"ef", b""]


class TestHexFramer:
    def test_full_rows_and_running_offset(self):
        framer = HexFramer(bytes_per_row=4)
        rows = framer.feed(b"ABCDEFGH")
        assert rows == [
            "00000000  41 42 43 44  |ABCD|",
            "00000004  45 46 47 48  |EFGH|",
        ]

    def test_bytes_are_in_groups_of_eight_as_in_hexdump(self):
        framer = HexFramer()
        assert framer.feed(b"RPM: 1697, Curre") == [
            "00000000  52 50 4D 3A 20 31 36 39  37 2C 20 43 75 72 72 65  |RPM: 1697, Curre|",
        ]

    def test_partial_row_is_held_then_flushed_padded(self):
        framer = HexFramer(bytes_per_row=4)
        assert framer.feed(b"AB") == []
        assert framer.flush() == ["00000000  41 42        |AB|"]
        assert framer.feed(b"CDEF") == ["00000002  43 44 45 46  |CDEF|"]

    def test_a_partial_row_keeps_the_characters_in_their_column(self):
        framer = HexFramer()
        full = framer.feed(bytes(range(0x41, 0x51)))[0]
        framer.feed(b"0123456789")
        partial = framer.flush()[0]
        assert full.index("|") == partial.index("|")

    def test_unprintable_bytes_show_as_placeholder(self):
        framer = HexFramer(bytes_per_row=4)
        assert framer.feed(b"\x00\x7f\xffA") == ["00000000  00 7F FF 41  |...A|"]

    def test_flush_with_nothing_pending(self):
        assert HexFramer().flush() == []


class TestHexRowMarks:
    def test_offset_bars_and_placeholders_are_structure(self):
        row = HexFramer().feed(b"\r\nRPM: 1697, Cur")[0]
        bar = row.index("|")
        assert hex_row_marks(row) == [
            (0, 8), (bar, bar + 1), (bar + 1, bar + 2), (bar + 2, bar + 3), (len(row) - 1, len(row)),
        ]

    def test_a_dot_or_a_bar_received_is_a_byte(self):
        framer = HexFramer()
        framer.feed(b".|A")
        row = framer.flush()[0]
        bar = row.index("|")
        assert hex_row_marks(row) == [(0, 8), (bar, bar + 1), (len(row) - 1, len(row))]

    @pytest.mark.parametrize(
        "text", ["", "RPM: 1697, Current: 2.39", "00000000  41 42  |A|", "|ab|"],
    )
    def test_text_that_is_not_a_hex_row_has_no_marks(self, text):
        assert hex_row_marks(text) == []


class TestDecodeLine:
    def test_plain_text(self):
        assert decode_line(b"Temp: 22.4") == "Temp: 22.4"

    def test_empty_line_stays_empty(self):
        assert decode_line(b"") == ""

    def test_utf8_is_decoded(self):
        assert decode_line("22.4 °C".encode()) == "22.4 °C"

    def test_invalid_bytes_keep_their_value(self):
        assert decode_line(b"temp \xb0C") == "temp \\xb0C"

    def test_ansi_colour_codes_are_removed(self):
        assert decode_line(b"\x1b[31mERROR\x1b[0m: failed") == "ERROR: failed"

    def test_line_of_only_escape_codes_is_dropped(self):
        assert decode_line(b"\x1b[2J\x1b[H") is None

    def test_tab_is_kept_and_other_controls_are_escaped(self):
        assert decode_line(b"a\tb\x00c\x07") == "a\tb\\x00c\\x07"


class TestEscapeSequenceKinds:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (b"\x1b[31mred\x1b[0m", "red"),
            (b"\x1b[?25lhidden cursor\x1b[?25h", "hidden cursor"),
            (b"\x1b[1;32;40mbold\x1b[m", "bold"),
            (b"\x1b]0;window title\x07prompt", "prompt"),
            (b"\x1b]2;title\x1b\\prompt", "prompt"),
            (b"\x1bMreverse index", "reverse index"),
        ],
    )
    def test_sequence_is_removed(self, raw, expected):
        assert decode_line(raw) == expected

    def test_lone_escape_byte_is_shown_not_dropped(self):
        assert decode_line(b"a\x1b b") == "a\\x1b b"

    def test_line_of_only_sequences_is_noise(self):
        assert decode_line(b"\x1b[2J\x1b[H") is None

    def test_printable_text_outside_ascii_is_unchanged(self):
        text = "temp\u00e9rature \u6e29\u5ea6 21.5\u00b0C"
        assert decode_line(text.encode()) == text


class TestCharactersThatChangeHowTextIsShown:
    @pytest.mark.parametrize(
        ("code", "shown"),
        [
            (0x7F, "\\x7f"),
            (0x85, "\\x85"),
            (0x9B, "\\x9b"),
            (0x2028, "\\u2028"),
            (0x2029, "\\u2029"),
            (0x202A, "\\u202a"),
            (0x202E, "\\u202e"),
            (0x2066, "\\u2066"),
            (0x2069, "\\u2069"),
        ],
    )
    def test_it_is_written_as_an_escape(self, code, shown):
        assert decode_line(f"a{chr(code)}b".encode()) == f"a{shown}b"

    def test_a_line_cannot_be_drawn_in_reverse(self):
        shown = decode_line(f"status {chr(0x202E)}KO{chr(0x202C)} done".encode())

        assert shown == "status \\u202eKO\\u202c done"
        assert shown.isprintable()

    def test_a_separator_cannot_start_a_new_line_in_a_log(self):
        shown = decode_line(f"one{chr(0x2028)}[2026-01-01 00:00:00.000] forged".encode())

        assert len(shown.splitlines()) == 1

    @pytest.mark.parametrize("code", [0x200B, 0x00A0, 0x00E9, 0x200D])
    def test_other_characters_outside_ascii_are_left_alone(self, code):
        text = f"a{chr(code)}b"

        assert escape_control_characters(text) == text
