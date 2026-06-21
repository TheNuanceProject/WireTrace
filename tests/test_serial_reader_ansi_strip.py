# SPDX-License-Identifier: MIT
"""Regression tests for B5 — ANSI/VT100 escape codes leaking into output.

The bug: firmware that emits ANSI/VT100 escape sequences (Zephyr shell,
U-Boot, coloured FreeRTOS CLIs) produced lines with raw codes like
``\x1b[31mERROR\x1b[0m`` in the console, the disk log, the CSV export,
and the plot parsers. The codes made logs unreadable, confused CSV
columns, and broke plot regex matches on colour-wrapped numbers.

The fix strips CSI escape sequences at the serial read layer — after
decode, before tag classification and emission — so every downstream
consumer receives clean text. Scope is stripping only; WireTrace is a
serial monitor, not a terminal emulator (rendering is explicitly out of
scope per the audit).

Two emit paths exist in ``SerialReader`` and both must strip:
``_process_lines`` (normal newline-terminated lines) and
``_flush_remaining`` (a partial final line on shutdown). These tests
cover the pure ``_strip_ansi`` helper plus both real paths, driven
through the conftest PySide6 stubs (no Qt binary needed).
"""

from __future__ import annotations

from app.constants import DisplayMode
from core.serial_reader import SerialReader, _strip_ansi
from core.tag_detector import TagDetector


class _Recorder:
    """Captures (decoded, tag) emissions from line_received."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def __call__(self, decoded: str, tag: str) -> None:
        self.events.append((decoded, tag))

    @property
    def lines(self) -> list[str]:
        return [d for d, _ in self.events]


def _make_reader_recording() -> tuple[SerialReader, _Recorder]:
    """A SerialReader with a fresh recorder on line_received.

    The stubbed Signal is shared at class level, so clear any slots
    left by a prior test before connecting this test's recorder.
    """
    reader = SerialReader(display_mode=DisplayMode.TEXT)
    reader.line_received.disconnect()
    rec = _Recorder()
    reader.line_received.connect(rec)
    return reader, rec


def _feed(reader: SerialReader, payload: bytes) -> None:
    reader._line_buffer.extend(payload)
    reader._process_lines()


class TestStripAnsiHelper:
    """Direct unit tests for the regex helper."""

    def test_strips_sgr_colour_codes(self):
        assert _strip_ansi("\x1b[31mERROR\x1b[0m") == "ERROR"

    def test_strips_compound_sgr(self):
        assert _strip_ansi("\x1b[1;33mWARNING\x1b[0m") == "WARNING"

    def test_strips_embedded_code_keeping_payload(self):
        assert _strip_ansi("RPM: \x1b[32m1450\x1b[0m, Current: 3.2") == \
            "RPM: 1450, Current: 3.2"

    def test_strips_screen_and_cursor_control(self):
        assert _strip_ansi("\x1b[2J\x1b[H") == ""

    def test_leaves_plain_text_untouched(self):
        assert _strip_ansi("Boot complete.") == "Boot complete."

    def test_no_partial_match_on_bare_escape(self):
        # A lone ESC without a complete CSI sequence is not a colour
        # code; the regex requires a final letter, so an incomplete
        # sequence is left as-is rather than eating following text.
        assert _strip_ansi("value\x1b[") == "value\x1b["


class TestProcessLinesStrips:
    """The normal newline-terminated path."""

    def test_colour_codes_removed_from_emitted_line(self):
        reader, rec = _make_reader_recording()
        _feed(reader, b"\x1b[31mERROR\x1b[0m: sensor offline\n")
        assert rec.lines == ["ERROR: sensor offline"]

    def test_embedded_number_survives_clean(self):
        reader, rec = _make_reader_recording()
        _feed(reader, b"RPM: \x1b[32m1450\x1b[0m, Current: 3.2\n")
        assert rec.lines == ["RPM: 1450, Current: 3.2"]

    def test_escape_only_line_is_skipped(self):
        reader, rec = _make_reader_recording()
        _feed(reader, b"\x1b[2J\x1b[H\n")
        assert rec.events == [], "a line of only escape codes must be skipped"

    def test_line_after_clear_screen_emitted_normally(self):
        reader, rec = _make_reader_recording()
        _feed(reader, b"\x1b[2J\x1b[H\nRPM: 1455, Current: 3.3\n")
        assert rec.lines == ["RPM: 1455, Current: 3.3"]

    def test_crlf_terminated_colour_line(self):
        reader, rec = _make_reader_recording()
        _feed(reader, b"\x1b[1;33mWARNING\x1b[0m: low voltage\r\n")
        assert rec.lines == ["WARNING: low voltage"]

    def test_tag_classification_runs_on_cleaned_text(self):
        # The tag must be derived from the stripped line, not from text
        # polluted with escape codes. Compare against the detector run
        # on the clean string directly.
        reader, rec = _make_reader_recording()
        _feed(reader, b"\x1b[31mERROR\x1b[0m: sensor offline\n")
        assert len(rec.events) == 1
        _, tag = rec.events[0]
        expected = TagDetector().detect("ERROR: sensor offline")
        assert tag == expected

    def test_plain_lines_unaffected(self):
        reader, rec = _make_reader_recording()
        _feed(reader, b"Boot complete.\n")
        assert rec.lines == ["Boot complete."]


class TestFlushRemainingStrips:
    """The shutdown path for a partial final line (no trailing newline)."""

    def test_partial_line_colour_codes_removed(self):
        reader, rec = _make_reader_recording()
        reader._line_buffer.extend(b"\x1b[31mERROR\x1b[0m: late line")
        reader._flush_remaining()
        assert rec.lines == ["ERROR: late line"]

    def test_partial_escape_only_line_skipped(self):
        reader, rec = _make_reader_recording()
        reader._line_buffer.extend(b"\x1b[0m")
        reader._flush_remaining()
        assert rec.events == []
