# SPDX-License-Identifier: GPL-3.0-or-later
"""The colours a device sets with ANSI escape codes, for the console.

Coloured firmware (ESP-IDF's log, Zephyr's shell, many command lines)
colours its text with SGR codes such as ``ESC[0;31m``. The text logged
is always the text without the codes (``core/framing.decode_line``);
when the user switches ANSI colours on, this module also says which
part of each line was in which colour, so the console can show it.

Only the colour of the text is followed, as one of the 16 ANSI colours,
which the console draws in the theme's own versions of them, each
readable on its background. A colour from the 256-colour or true-colour
codes is shown as the nearest of the 16 by its standard xterm value.
Background colours, bold, underline and the rest are not shown. As in
a terminal, a colour lasts until it is changed or reset, across lines.

This module does not touch the GUI, the disk, the serial port or any other I/O.
"""

from __future__ import annotations

from core.framing import ANSI_ESCAPE_RE, escape_control_characters

#: A part of a line's text and its colour: start, end, colour 0 to 15.
ColourSpan = tuple[int, int, int]

_SGR_START = "\x1b["
_SGR_END = "m"
_RESET = 0
_FOREGROUND = range(30, 38)
_BRIGHT_FOREGROUND = range(90, 98)
_BRIGHT_OFFSET = 8
_DEFAULT_FOREGROUND = 39
_EXTENDED_FOREGROUND = 38
_EXTENDED_BACKGROUND = 48
_INDEXED = 5
_DIRECT = 2
_CODE_DIGITS_MAX = 3
_CODE_UNKNOWN = 10 ** _CODE_DIGITS_MAX

#: The standard xterm values of the 16 colours, to find the nearest.
_XTERM_16 = (
    (0x00, 0x00, 0x00), (0xCD, 0x00, 0x00), (0x00, 0xCD, 0x00), (0xCD, 0xCD, 0x00),
    (0x00, 0x00, 0xEE), (0xCD, 0x00, 0xCD), (0x00, 0xCD, 0xCD), (0xE5, 0xE5, 0xE5),
    (0x7F, 0x7F, 0x7F), (0xFF, 0x00, 0x00), (0x00, 0xFF, 0x00), (0xFF, 0xFF, 0x00),
    (0x5C, 0x5C, 0xFF), (0xFF, 0x00, 0xFF), (0x00, 0xFF, 0xFF), (0xFF, 0xFF, 0xFF),
)
_CUBE_FIRST = 16
_CUBE_SIDE = 6
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)
_GREY_FIRST = 232
_GREY_START = 8
_GREY_STEP = 10
_COLOURS_256 = 256


class AnsiColours:
    """Follows the text colour of one session's lines."""

    def __init__(self) -> None:
        self._colour: int | None = None

    def reset(self) -> None:
        """Back to the default colour, as at the start of a session."""
        self._colour = None

    def decode(self, raw: bytes) -> tuple[str, tuple[ColourSpan, ...]] | None:
        """The text of one line, as ``decode_line`` gives it, and its coloured parts.

        Returns None for a line of escape codes only, as ``decode_line``
        does; its codes still set the colour of the lines after it.
        """
        text = raw.decode("utf-8", errors="backslashreplace")
        if text.isprintable():
            return text, self._whole(text)
        parts: list[str] = []
        spans: list[ColourSpan] = []
        position = 0
        last = 0
        for match in ANSI_ESCAPE_RE.finditer(text):
            position = self._add(text[last:match.start()], position, parts, spans)
            sequence = match.group(0)
            if sequence.startswith(_SGR_START) and sequence.endswith(_SGR_END):
                self._colour = _apply_sgr(sequence[len(_SGR_START):-len(_SGR_END)], self._colour)
            last = match.end()
        self._add(text[last:], position, parts, spans)
        shown = "".join(parts)
        if not shown:
            return None
        return shown, tuple(spans)

    def _whole(self, text: str) -> tuple[ColourSpan, ...]:
        if self._colour is None or not text:
            return ()
        return ((0, len(text), self._colour),)

    def _add(self, segment: str, position: int, parts: list[str], spans: list[ColourSpan]) -> int:
        """Add a stretch of text between codes; returns where the next starts."""
        segment = escape_control_characters(segment)
        if not segment:
            return position
        parts.append(segment)
        end = position + len(segment)
        if self._colour is not None:
            if spans and spans[-1][1] == position and spans[-1][2] == self._colour:
                spans[-1] = (spans[-1][0], end, self._colour)
            else:
                spans.append((position, end, self._colour))
        return end


def _apply_sgr(parameters: str, colour: int | None) -> int | None:
    """The text colour after an SGR sequence with ``parameters``."""
    codes: list[int] = []
    for piece in parameters.replace(":", ";").split(";"):
        if not piece.isdigit():
            if piece:
                return colour  # private or malformed: leave the colour as it was
            codes.append(_RESET)
            continue
        digits = piece.lstrip("0")
        # No code means anything past three digits; a longer number is
        # never converted, so a garbled line costs no more than any other.
        codes.append(int(digits or "0") if len(digits) <= _CODE_DIGITS_MAX else _CODE_UNKNOWN)
    index = 0
    while index < len(codes):
        code = codes[index]
        index += 1
        if code in (_RESET, _DEFAULT_FOREGROUND):
            colour = None
        elif code in _FOREGROUND:
            colour = code - _FOREGROUND.start
        elif code in _BRIGHT_FOREGROUND:
            colour = code - _BRIGHT_FOREGROUND.start + _BRIGHT_OFFSET
        elif code in (_EXTENDED_FOREGROUND, _EXTENDED_BACKGROUND):
            found, index = _extended(codes, index)
            if code == _EXTENDED_FOREGROUND and found is not None:
                colour = found
    return colour


def _extended(codes: list[int], index: int) -> tuple[int | None, int]:
    """Read a 256-colour or true-colour choice at ``index``; returns the
    nearest of the 16 colours (None if malformed) and where reading stops."""
    if index >= len(codes):
        return None, index
    kind = codes[index]
    if kind == _INDEXED and index + 1 < len(codes):
        value = codes[index + 1]
        return (_nearest_of_256(value) if value < _COLOURS_256 else None), index + 2
    if kind == _DIRECT and index + 3 < len(codes):
        rgb = tuple(codes[index + 1:index + 4])
        if all(0 <= part <= 255 for part in rgb):
            return _nearest(rgb), index + 4
        return None, index + 4
    return None, index + 1


def _nearest_of_256(value: int) -> int:
    if value < _CUBE_FIRST:
        return value
    if value < _GREY_FIRST:
        cube = value - _CUBE_FIRST
        red, rest = divmod(cube, _CUBE_SIDE * _CUBE_SIDE)
        green, blue = divmod(rest, _CUBE_SIDE)
        return _nearest((_CUBE_LEVELS[red], _CUBE_LEVELS[green], _CUBE_LEVELS[blue]))
    level = _GREY_START + (value - _GREY_FIRST) * _GREY_STEP
    return _nearest((level, level, level))


def _nearest(rgb: tuple[int, ...]) -> int:
    """The index of the standard colour nearest ``rgb``, by distance in RGB."""
    return min(
        range(len(_XTERM_16)),
        key=lambda index: sum((a - b) ** 2 for a, b in zip(_XTERM_16[index], rgb, strict=True)),
    )
