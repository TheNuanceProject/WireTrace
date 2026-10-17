# SPDX-License-Identifier: GPL-3.0-or-later
"""Turns the raw byte stream of a serial port into displayable lines.

Two framers share one job, cutting an endless byte stream into units:

  LineFramer  Text mode. A line ends at ``\\n``, ``\\r\\n``, or a lone
              ``\\r``. A ``\\r\\n`` pair that is split across two reads
              still counts as one line ending.
  HexFramer   HEX mode. Fixed-width rows in the layout of ``hexdump -C``:
              the offset, the bytes in groups of eight, and the
              characters between bars, a dot for each byte that has no
              printable character.

``hex_row_marks`` says which characters of a hex row are its structure
(the offset, the bars and the dots) rather than the bytes, so they can
be drawn quieter.

``decode_line`` converts the bytes of one text line to the string that
is shown and logged.

Everything here is pure computation. This module does not touch GUI,
disk, serial, or any I/O.
"""

from __future__ import annotations

import re

from app.constants import HEX_BYTES_PER_GROUP, HEX_BYTES_PER_ROW, SERIAL_MAX_LINE_BYTES

_LF = b"\n"
_CR = b"\r"
_LINE_ENDINGS = b"\r\n"

# ANSI/VT100 escape sequences: CSI (colour, cursor and screen control),
# OSC (window title) and two-character sequences, as emitted by RTOS
# shells, bootloaders and coloured firmware CLIs.
# WireTrace is a serial monitor, not a terminal emulator, so these are
# removed rather than rendered.
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-?]*[ -/]*[@-~]"                 # CSI: colours, cursor, private modes
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"     # OSC: window title and the like
    r"|[@-Z\\^_]"                         # two-character sequences
    r")",
)

# Characters that would change how a line is shown or stored rather than
# appear in it are written as an escape, so they stay visible and cannot
# corrupt the console or a log file: control characters other than tab
# (C0, DEL and C1), the Unicode line and paragraph separators, which
# some editors treat as line breaks, and the characters that override
# the direction text is drawn in.
_BIDI_CONTROLS = (*range(0x202A, 0x202F), *range(0x2066, 0x206A))
_CONTROL_ESCAPES = {
    **{
        code: f"\\x{code:02x}"
        for code in (*range(0x00, 0x09), *range(0x0A, 0x20), *range(0x7F, 0xA0))
    },
    **{code: f"\\u{code:04x}" for code in (0x2028, 0x2029, *_BIDI_CONTROLS)},
}

_FIRST_PRINTABLE = 0x20
_LAST_PRINTABLE = 0x7E
_UNPRINTABLE_PLACEHOLDER = "."
_HEX_OFFSET_WIDTH = 8
# Between the offset, the bytes and the characters; between two bytes;
# and added between two groups of bytes.
_HEX_GAP = "  "
_HEX_BYTE_GAP = " "
_HEX_GROUP_GAP = " "
_HEX_BAR = "|"
_HEX_BYTE = re.compile(r"[0-9A-F]{2}")


class LineFramer:
    """Cuts a byte stream into lines, one ``feed`` at a time."""

    def __init__(self, max_line_bytes: int = SERIAL_MAX_LINE_BYTES) -> None:
        self._max_line_bytes = max_line_bytes
        self._partial = b""
        self._skip_leading_lf = False
        self._splitting_long_line = False

    def feed(self, data: bytes) -> list[bytes]:
        """Add received bytes and return every line they complete.

        Returned lines exclude their terminator. Bytes after the last
        terminator are kept for the next call. A stream with no
        terminator at all is returned in pieces of the maximum length.
        """
        if self._skip_leading_lf and data:
            self._skip_leading_lf = False
            if data.startswith(_LF):
                data = data[1:]
        if not data:
            return []

        parts = (self._partial + data).splitlines(keepends=True)
        last = parts[-1]
        if last.endswith((_LF, _CR)):
            self._partial = b""
            self._skip_leading_lf = last.endswith(_CR)
        else:
            self._partial = parts.pop()

        lines = [part.rstrip(_LINE_ENDINGS) for part in parts]
        if lines and self._splitting_long_line:
            # The terminator of a line that was already returned in
            # pieces: what precedes it is the last piece, and an empty
            # last piece is not a blank line.
            self._splitting_long_line = False
            if not lines[0]:
                del lines[0]

        while len(self._partial) >= self._max_line_bytes:
            lines.append(self._partial[:self._max_line_bytes])
            self._partial = self._partial[self._max_line_bytes:]
            self._splitting_long_line = True
        return lines

    def flush(self) -> list[bytes]:
        """Return the unterminated remainder as a final line, if any."""
        self._skip_leading_lf = False
        self._splitting_long_line = False
        if not self._partial:
            return []
        remainder = self._partial
        self._partial = b""
        return [remainder]


class HexFramer:
    """Cuts a byte stream into fixed-width hex dump rows."""

    def __init__(
        self, bytes_per_row: int = HEX_BYTES_PER_ROW, bytes_per_group: int = HEX_BYTES_PER_GROUP,
    ) -> None:
        self._bytes_per_row = bytes_per_row
        self._bytes_per_group = bytes_per_group
        self._partial = b""
        self._offset = 0

    def feed(self, data: bytes) -> list[str]:
        """Add received bytes and return every full row they complete."""
        buffer = self._partial + data
        full_length = len(buffer) - (len(buffer) % self._bytes_per_row)
        rows = [
            self._format_row(buffer[start:start + self._bytes_per_row])
            for start in range(0, full_length, self._bytes_per_row)
        ]
        self._partial = buffer[full_length:]
        return rows

    def flush(self) -> list[str]:
        """Return the incomplete row, if any. The offset keeps counting."""
        if not self._partial:
            return []
        remainder = self._partial
        self._partial = b""
        return [self._format_row(remainder)]

    def _hex_text(self, chunk: bytes) -> str:
        """The bytes in hex, in groups, a wider gap between groups."""
        group = self._bytes_per_group
        groups = (chunk[start:start + group] for start in range(0, len(chunk), group))
        return (_HEX_BYTE_GAP + _HEX_GROUP_GAP).join(
            _HEX_BYTE_GAP.join(f"{byte:02X}" for byte in part) for part in groups
        )

    def _format_row(self, chunk: bytes) -> str:
        # An incomplete row keeps the width of a full one, so the
        # characters of every row start in the same column.
        hex_width = len(self._hex_text(bytes(self._bytes_per_row)))
        text_part = "".join(
            chr(byte) if _is_printable(byte) else _UNPRINTABLE_PLACEHOLDER for byte in chunk
        )
        row = (
            f"{self._offset:0{_HEX_OFFSET_WIDTH}X}{_HEX_GAP}"
            f"{self._hex_text(chunk):<{hex_width}}{_HEX_GAP}{_HEX_BAR}{text_part}{_HEX_BAR}"
        )
        self._offset += len(chunk)
        return row


def _is_printable(byte: int) -> bool:
    return _FIRST_PRINTABLE <= byte <= _LAST_PRINTABLE


def hex_row_marks(row: str) -> list[tuple[int, int]]:
    """The spans of a hex row that are its structure, not its bytes.

    The offset, the two bars, and the dot standing for each byte that
    has no printable character; a byte that is itself a dot (2E) is not
    one of them. Read from the row's own hex digits, so it holds for any
    row width. A text that is not a hex row gives no spans.
    """
    start = _HEX_OFFSET_WIDTH + len(_HEX_GAP)
    bar = row.find(_HEX_BAR, start)
    if bar < 0 or not row.endswith(_HEX_BAR):
        return []
    values = [int(pair, 16) for pair in _HEX_BYTE.findall(row, start, bar)]
    if len(values) != len(row) - bar - 2:
        return []
    marks = [(0, _HEX_OFFSET_WIDTH), (bar, bar + 1)]
    marks += [
        (bar + 1 + index, bar + 2 + index)
        for index, value in enumerate(values) if not _is_printable(value)
    ]
    marks.append((len(row) - 1, len(row)))
    return marks


def decode_line(raw: bytes) -> str | None:
    """Convert the bytes of one line to the text that is shown and logged.

    Bytes that are not valid UTF-8, and control characters other than
    tab, are written as ``\\xNN`` so their value is preserved. Line
    separators and direction overrides are written as ``\\uNNNN``. ANSI
    escape sequences are removed.

    Returns ``None`` for a line that consisted only of escape sequences
    (for example a clear-screen code), which is noise rather than
    content. An empty line stays an empty line.
    """
    text = raw.decode("utf-8", errors="backslashreplace")
    if text.isprintable():
        # No control character of any kind, so nothing to remove or
        # escape. This is nearly every line of a normal stream.
        return text
    stripped = _ANSI_ESCAPE_RE.sub("", text)
    if not stripped:
        return None
    return escape_control_characters(stripped)


def escape_control_characters(text: str) -> str:
    """Write control characters other than tab, line separators and
    direction overrides as escapes."""
    return text.translate(_CONTROL_ESCAPES)
