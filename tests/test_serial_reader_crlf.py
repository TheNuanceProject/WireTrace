# SPDX-License-Identifier: MIT
"""B11 investigation + regression — double newline on ``\\r\\n``.

u/Stromi1011 reported (r/embedded, v1.1.0) seeing a doubled newline after
``\\r\\n``-terminated lines. The bug was filed as *suspected*: a static
reading of the line-assembly logic suggested it was already correct, and
no reproduction existed. The audit asked for a synthetic replay through
``SerialReader._process_lines`` to confirm or rule out a parser defect.

This module is that replay. It feeds the canonical CRLF-1 mixed
line-ending stream (see SIMULATOR_SCENARIOS.md / VERIFY S-8) through the
REAL reader, under three read chunkings:

  1. one read (whole stream at once),
  2. per-write reads (the chunk boundaries the simulator actually emits),
  3. byte-by-byte (the worst-case boundary: every ``\\r`` and ``\\n`` can
     land in a separate read).

Outcome: the parser produces exactly the 11 expected lines in every
chunking — no blank lines, no doubles, no merges. B11 is therefore NOT
reproducible at the parser layer; the line assembly is correct including
when ``\\r`` and ``\\n`` arrive in different reads. These tests remain as a
permanent regression guard so a future change can't silently reintroduce
the doubling the reporter described.
"""

from __future__ import annotations

from app.constants import DisplayMode
from core.serial_reader import SerialReader

# The CRLF-1 simulator's writes, in order (SIMULATOR_SCENARIOS.md).
CRLF1_WRITES = [
    b"line-LF-1\nline-LF-2\nline-LF-3\n",
    b"line-CRLF-1\r\nline-CRLF-2\r\nline-CRLF-3\r\n",
    b"line-blank-before\r\n\r\nline-blank-after\r\n",
    b"line-split-1\r",
    b"\nline-split-2\r",
    b"\nline-split-3\r\n",
]

EXPECTED_LINES = [
    "line-LF-1",
    "line-LF-2",
    "line-LF-3",
    "line-CRLF-1",
    "line-CRLF-2",
    "line-CRLF-3",
    "line-blank-before",
    "line-blank-after",
    "line-split-1",
    "line-split-2",
    "line-split-3",
]


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def __call__(self, decoded: str, tag: str) -> None:
        self.events.append((decoded, tag))

    @property
    def lines(self) -> list[str]:
        return [d for d, _ in self.events]


def _fresh_reader() -> tuple[SerialReader, _Recorder]:
    reader = SerialReader(display_mode=DisplayMode.TEXT)
    reader.line_received.disconnect()  # shared stub signal — clear prior slots
    rec = _Recorder()
    reader.line_received.connect(rec)
    return reader, rec


def _feed_chunks(reader: SerialReader, chunks: list[bytes]) -> None:
    """Mimic the run loop: append each read chunk, then process."""
    for chunk in chunks:
        reader._line_buffer.extend(chunk)
        reader._process_lines()
    reader._flush_remaining()  # end-of-stream: must not add a spurious line


class TestCrlfHandlingByChunking:
    def test_single_read(self):
        reader, rec = _fresh_reader()
        _feed_chunks(reader, [b"".join(CRLF1_WRITES)])
        assert rec.lines == EXPECTED_LINES

    def test_per_write_reads(self):
        reader, rec = _fresh_reader()
        _feed_chunks(reader, list(CRLF1_WRITES))
        assert rec.lines == EXPECTED_LINES

    def test_byte_by_byte_reads(self):
        # The most aggressive boundary: every byte is its own read, so a
        # \r and its following \n always land in separate reads.
        reader, rec = _fresh_reader()
        stream = b"".join(CRLF1_WRITES)
        _feed_chunks(reader, [stream[i:i + 1] for i in range(len(stream))])
        assert rec.lines == EXPECTED_LINES


class TestNoDoubleNewline:
    def test_exactly_eleven_lines(self):
        reader, rec = _fresh_reader()
        _feed_chunks(reader, list(CRLF1_WRITES))
        assert len(rec.lines) == 11

    def test_no_blank_lines_emitted(self):
        # The reporter's symptom is a blank line between data lines. The
        # deliberate \r\n\r\n in case 3 must collapse to nothing, leaving
        # no empty emission anywhere.
        reader, rec = _fresh_reader()
        _feed_chunks(reader, list(CRLF1_WRITES))
        assert all(line.strip() for line in rec.lines)

    def test_no_carriage_return_survives(self):
        # No emitted line should contain a stray \r (which a terminal
        # could render as an extra line break).
        reader, rec = _fresh_reader()
        _feed_chunks(reader, list(CRLF1_WRITES))
        assert all("\r" not in line for line in rec.lines)

    def test_blank_before_directly_followed_by_blank_after(self):
        reader, rec = _fresh_reader()
        _feed_chunks(reader, list(CRLF1_WRITES))
        i = rec.lines.index("line-blank-before")
        # The very next emitted line is line-blank-after — nothing between.
        assert rec.lines[i + 1] == "line-blank-after"


class TestSplitCrlfBoundary:
    def test_cr_then_lf_in_separate_reads_is_single_line(self):
        # Minimal isolation of the boundary case: "data\r" in one read,
        # "\n" in the next, must yield exactly one clean line.
        reader, rec = _fresh_reader()
        _feed_chunks(reader, [b"data\r", b"\n"])
        assert rec.lines == ["data"]

    def test_trailing_partial_line_flushed_once(self):
        # A final line with no terminator is flushed exactly once on stop,
        # with its trailing \r stripped — not duplicated.
        reader, rec = _fresh_reader()
        reader._line_buffer.extend(b"final-line\r")
        reader._flush_remaining()
        assert rec.lines == ["final-line"]
