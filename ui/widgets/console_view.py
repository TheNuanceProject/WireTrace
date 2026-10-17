# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace console: the list of lines received from a device.

A custom-drawn view. Only the rows that are on screen are drawn, so the
cost of showing a line does not depend on how many lines are held.

  - Three columns: time, level, message. The level is coloured; warning
    and error rows carry a faint tint.
  - A bounded line store, so memory stays flat however long a session
    runs. The session spool, not the console, is the record of the
    whole session.
  - Following: the view stays on the newest line until the user scrolls
    up, and counts the lines that arrive meanwhile.
  - Selection by mouse and keyboard, copy, and search with highlights.
  - Filtering by text and by level. WireTrace's own notices are always
    shown.

A row is addressed by a number that never changes while the row is
held, so a selection or a search match stays on its row when older rows
leave the store.

This is a display-only widget. It does not touch business logic, disk,
or serial.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, deque
from collections.abc import Iterable
from datetime import datetime
from typing import NamedTuple

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFontMetricsF,
    QGuiApplication,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QResizeEvent,
    QTextCharFormat,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QAbstractScrollArea, QWidget

from app.constants import (
    CONSOLE_MAX_CHARACTERS,
    CONSOLE_SEARCH_MATCH_LIMIT,
    CONSOLE_SHAPED_CHARACTERS,
    CONSOLE_TAB_WIDTH,
    DEFAULT_FONT_FAMILY,
    DEFAULT_FONT_SIZE,
    MAX_CONSOLE_LINES,
    TimestampMode,
)
from core.framing import hex_row_marks
from core.records import LineRecord, RecordOrigin
from ui.formatting import format_time_of_day
from ui.themes import tokens
from ui.themes.fonts import monospace_font

logger = logging.getLogger(__name__)

#: A width no row reaches, for a text line that must never wrap.
_UNBOUNDED_LINE_WIDTH = float(2**24)

# ── Row layout, in character cells ───────────────────────────────────────────

_TIME_COLS = 12                      # "HH:MM:SS.mmm"
_LEVEL_COLS = 3                      # "ERR", "WRN", ...
_GAP = "  "
_FIRST_RELATIVE_TIME = "+0.000 s"

#: A layout is kept for this many rows that need full text shaping.
_LAYOUT_CACHE_ROWS = 512

#: While a drag is held past the top or bottom edge, the view scrolls
#: one step every this many milliseconds.
_DRAG_SCROLL_INTERVAL_MS = 40

#: A fixed-width font is accepted as such when the widths of test
#: strings agree to within this many pixels.
_FIXED_PITCH_TOLERANCE = 0.01
_FIXED_PITCH_SAMPLE = 64

_WORD = re.compile(r"\w+")


class Position(NamedTuple):
    """A place in the text: a row's permanent number and a column."""

    row: int
    col: int


class _Entry(NamedTuple):
    """A held line and the time of the line received before it."""

    record: LineRecord
    previous: datetime | None


class ConsoleView(QAbstractScrollArea):
    """The console of one device tab.

    Signals:
        line_count_changed(int):   The number of lines held changed.
        following_changed(bool):   The view started or stopped following
                                   the newest line.
        unseen_lines_changed(int): Lines that arrived while the view was
                                   not following.
        selection_changed():       The selected text changed.
        search_changed(int, int):  Number of matches, and the index of
                                   the current one (-1 for none).
        level_counts_changed():    The number of lines per level changed.
    """

    line_count_changed = Signal(int)
    following_changed = Signal(bool)
    unseen_lines_changed = Signal(int)
    selection_changed = Signal()
    search_changed = Signal(int, int)
    level_counts_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("consoleView")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Console output")
        self.setAccessibleDescription("Lines received from the device")
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)

        # Lines held, and the subset that passes the filters.
        self._max_lines = MAX_CONSOLE_LINES
        self._entries: deque[_Entry] = deque(maxlen=self._max_lines)
        #: Characters of text in the lines held, for the character limit.
        self._characters = 0
        self._rows: deque[_Entry] = deque()
        self._row_base = 0               # permanent number of _rows[0]
        self._level_counts: Counter[str] = Counter()

        # What is shown, and how.
        self._timestamp_mode = TimestampMode.ABSOLUTE
        self._filter_text = ""
        self._levels: frozenset[str] | None = None
        self._hide_blank_lines = False
        self._severity_colours = True

        # Following the newest line.
        self._auto_follow = True
        self._following = True
        self._unseen = 0
        self._adjusting_scroll = False

        # Selection and search.
        self._anchor: Position | None = None
        self._cursor: Position | None = None
        self._dragging = False
        self._search_pattern: re.Pattern[str] | None = None
        self._search_first = ""
        self._matches: deque[tuple[int, int, int]] = deque()   # row, col, length
        self._matches_by_row: dict[int, list[tuple[int, int]]] = {}
        self._current_match = -1

        # Geometry, set by _apply_font().
        self._cell_width = 1.0
        self._row_height = 1
        self._ascent = 0.0
        self._fixed_pitch = False
        self._max_width = 0.0
        self._layouts: dict[int, QTextLayout] = {}

        self._drag_timer = QTimer(self)
        self._drag_timer.setInterval(_DRAG_SCROLL_INTERVAL_MS)
        self._drag_timer.timeout.connect(self._on_drag_scroll)
        self._drag_direction = 0

        self.verticalScrollBar().valueChanged.connect(self._on_vertical_scroll)
        self.horizontalScrollBar().valueChanged.connect(self.viewport().update)

        # The initial font goes through set_font_size(), the same path
        # every later change uses (Preferences, Ctrl+0, Ctrl++, Ctrl+-),
        # so the size at launch and after a change cannot differ.
        self.setFont(monospace_font(DEFAULT_FONT_FAMILY))
        self.set_font_size(DEFAULT_FONT_SIZE)

    # ── Lines ────────────────────────────────────────────────────────────

    @property
    def total_count(self) -> int:
        """Lines the console holds, shown or hidden by the filters."""
        return len(self._entries)

    @property
    def visible_count(self) -> int:
        """Lines that pass the filters."""
        return len(self._rows)

    @property
    def last_timestamp(self) -> datetime | None:
        """Timestamp of the newest line held, or None if there is none."""
        return self._entries[-1].record.timestamp if self._entries else None

    def shown_records(self) -> list[LineRecord]:
        """The lines that pass the filters, oldest first."""
        return [entry.record for entry in self._rows]

    def display_lines(self) -> list[str]:
        """Every shown row exactly as it is drawn, oldest first."""
        return [self._row_text(entry) for entry in self._rows]

    def visible_lines(self) -> list[str]:
        """The rows on screen right now, as they are drawn."""
        first = self.verticalScrollBar().value()
        last = min(len(self._rows), first + self._rows_per_page() + 1)
        return [self._row_text(self._rows[index]) for index in range(first, last)]

    def level_counts(self) -> dict[str, int]:
        """Number of held lines per tag. Notices are not counted."""
        return dict(self._level_counts)

    def append_records(self, records: list[LineRecord]) -> None:
        """Append a batch of lines.

        The whole batch costs one scroll-range update and one repaint,
        so the cost per line stays low at any line rate.
        """
        if not records:
            return

        entries = self._entries
        rows = self._rows
        evicted_rows = 0
        added_rows = 0
        widest = 0
        for record in records:
            # The oldest lines make room: for one line when the line
            # limit is reached, for as many as it takes when the lines
            # held are long enough to reach the character limit first.
            while entries and (
                len(entries) == self._max_lines
                or self._characters + len(record.text) > CONSOLE_MAX_CHARACTERS
            ):
                oldest = entries.popleft()
                self._characters -= len(oldest.record.text)
                self._count_level(oldest.record, -1)
                if rows and rows[0] is oldest:
                    rows.popleft()
                    evicted_rows += 1
            previous = entries[-1].record.timestamp if entries else None
            entry = _Entry(record, previous)
            entries.append(entry)
            self._characters += len(record.text)
            self._count_level(record, +1)
            if self._is_shown(record):
                rows.append(entry)
                added_rows += 1
                widest = max(widest, self._message_cols(record.text))
                if self._search_pattern is not None:
                    self._find_in_row(self._row_base + evicted_rows + len(rows) - 1, entry)

        if evicted_rows:
            self._row_base += evicted_rows
            self._drop_evicted(evicted_rows)
        if widest:
            self._max_width = max(self._max_width, self._prefix_width() + widest * self._cell_width)

        self._update_scroll_ranges(evicted_rows=evicted_rows, added_rows=added_rows)
        self.viewport().update()
        self.line_count_changed.emit(len(entries))
        self.level_counts_changed.emit()
        if self._search_pattern is not None and (added_rows or evicted_rows):
            self.search_changed.emit(len(self._matches), self._current_match)

    def clear_console(self) -> None:
        """Remove every line."""
        self._entries.clear()
        self._characters = 0
        self._level_counts.clear()
        self._rebuild_rows()
        self.line_count_changed.emit(0)
        self.level_counts_changed.emit()

    def set_max_lines(self, max_lines: int) -> None:
        """Set how many lines the console holds. Lowering it keeps the newest."""
        if max_lines <= 0:
            logger.warning("Ignoring invalid console line limit: %d", max_lines)
            return
        if max_lines == self._max_lines:
            return
        removed = len(self._entries) > max_lines
        self._max_lines = max_lines
        self._entries = deque(self._entries, maxlen=max_lines)
        if removed:
            self._level_counts = Counter()
            self._characters = 0
            for entry in self._entries:
                self._count_level(entry.record, +1)
                self._characters += len(entry.record.text)
            self._rebuild_rows()
            self.line_count_changed.emit(len(self._entries))
            self.level_counts_changed.emit()

    # ── What is shown ────────────────────────────────────────────────────────

    @property
    def filter_text(self) -> str:
        return self._filter_text

    def set_filter(self, text: str) -> None:
        """Show only lines that contain ``text`` (letter case is ignored)."""
        normalized = text.lower().strip()
        if normalized == self._filter_text:
            return
        self._filter_text = normalized
        self._rebuild_rows()

    def set_levels(self, levels: Iterable[str] | None) -> None:
        """Show only lines with one of these tags. ``None`` shows every level."""
        chosen = None if levels is None else frozenset(levels)
        if chosen == self._levels:
            return
        self._levels = chosen
        self._rebuild_rows()

    def set_hide_blank_lines(self, hide: bool) -> None:
        """Show or hide blank lines. They stay in the session either way."""
        if hide == self._hide_blank_lines:
            return
        self._hide_blank_lines = hide
        self._rebuild_rows()

    def set_timestamp_mode(self, mode: TimestampMode) -> None:
        """Show the time each line arrived, or the time since the previous line."""
        if mode == self._timestamp_mode:
            return
        self._timestamp_mode = mode
        # The time column changed, so positions inside rows moved.
        self._clear_selection()
        self._layouts.clear()
        self._search_again()
        self.viewport().update()

    def set_severity_colours(self, enabled: bool) -> None:
        """Colour rows by level, or draw every row in the plain text colour."""
        if enabled == self._severity_colours:
            return
        self._severity_colours = enabled
        self._layouts.clear()
        self.viewport().update()

    def set_font_family(self, family: str) -> None:
        size = self.font().pixelSize()
        self.setFont(monospace_font(family, size if size > 0 else None))
        self._apply_font()

    def set_font_size(self, size: int) -> None:
        """Set the text size, in logical pixels."""
        font = self.font()
        font.setPixelSize(size)
        self.setFont(font)
        self._apply_font()

    # ── Following ────────────────────────────────────────────────────────

    @property
    def following(self) -> bool:
        """True while the view stays on the newest line."""
        return self._following

    @property
    def unseen_lines(self) -> int:
        """Lines that arrived while the view was not following."""
        return self._unseen

    def follow(self) -> None:
        """Go to the newest line and stay there."""
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._set_following(self._auto_follow)

    def set_auto_follow(self, enabled: bool) -> None:
        """Follow new lines from now on, or stop following them.

        While switched off, the view stays where it is put: scrolling
        to the end does not start following again.
        """
        self._auto_follow = enabled
        if enabled:
            self.follow()
        else:
            self._set_following(False)

    # ── Selection ────────────────────────────────────────────────────────

    @property
    def has_selection(self) -> bool:
        return self._selection() is not None

    def selected_text(self) -> str:
        """The selected text as it is shown. Tabs in a message are kept."""
        return self._selected(with_prefix=True)

    def selected_messages(self) -> str:
        """The selected text without the time and level columns."""
        return self._selected(with_prefix=False)

    def select_all(self) -> None:
        if not self._rows:
            return
        last = self._row_base + len(self._rows) - 1
        self._set_selection(Position(self._row_base, 0), self._row_end(last))

    def copy(self) -> None:
        """Put the selected text on the clipboard."""
        text = self.selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)

    def copy_messages(self) -> None:
        """Put the selected text, without time and level, on the clipboard."""
        text = self.selected_messages()
        if text:
            QGuiApplication.clipboard().setText(text)

    # ── Search ───────────────────────────────────────────────────────────

    @property
    def match_count(self) -> int:
        return len(self._matches)

    @property
    def current_match(self) -> int:
        """Index of the current match, or -1."""
        return self._current_match

    @property
    def match_limit_reached(self) -> bool:
        return len(self._matches) >= CONSOLE_SEARCH_MATCH_LIMIT

    def set_search(self, text: str) -> None:
        """Highlight every occurrence of ``text`` (letter case is ignored)."""
        self._search_pattern = (
            re.compile(re.escape(text), re.IGNORECASE) if text else None
        )
        self._search_first = text[:1].lower()
        self._search_again()
        if self._matches:
            self._go_to_match(0)
        self.viewport().update()

    def search_next(self) -> None:
        if self._matches:
            self._go_to_match((self._current_match + 1) % len(self._matches))

    def search_previous(self) -> None:
        if self._matches:
            self._go_to_match((self._current_match - 1) % len(self._matches))

    # ── Rows ─────────────────────────────────────────────────────────────

    def _is_shown(self, record: LineRecord) -> bool:
        if record.origin is RecordOrigin.NOTICE:
            # WireTrace's own notices report something the user must
            # not miss, so no filter hides them.
            return True
        if self._levels is not None and record.tag not in self._levels:
            return False
        if self._hide_blank_lines and not record.text.strip():
            return False
        return not self._filter_text or self._filter_text in record.text.lower()

    def _count_level(self, record: LineRecord, change: int) -> None:
        if record.origin is RecordOrigin.NOTICE:
            return
        self._level_counts[record.tag] += change
        if self._level_counts[record.tag] <= 0:
            del self._level_counts[record.tag]

    def _rebuild_rows(self) -> None:
        """Choose the shown rows again. Row numbers start over."""
        self._rows = deque(e for e in self._entries if self._is_shown(e.record))
        self._row_base = 0
        self._layouts.clear()
        self._clear_selection()
        self._max_width = self._prefix_width() + self._cell_width * max(
            (self._message_cols(e.record.text) for e in self._rows), default=0
        )
        self._unseen = 0
        self._search_again()
        self._update_scroll_ranges()
        if self._auto_follow:
            self.follow()
        self.unseen_lines_changed.emit(0)
        self.viewport().update()

    def _drop_evicted(self, count: int) -> None:
        """Forget positions on rows that have left the store."""
        base = self._row_base
        for row in range(base - count, base):
            self._layouts.pop(row, None)

        if self._anchor is not None and self._cursor is not None:
            start, end = sorted((self._anchor, self._cursor))
            if end.row < base:
                self._clear_selection()
            elif start.row < base:
                self._anchor, self._cursor = Position(base, 0), end
                self.selection_changed.emit()

        dropped = 0
        while self._matches and self._matches[0][0] < base:
            row = self._matches.popleft()[0]
            self._matches_by_row.pop(row, None)
            dropped += 1
        if dropped:
            self._current_match = max(self._current_match - dropped, -1)
            if self._current_match < 0 and self._matches:
                self._current_match = 0

    def _entry(self, row: int) -> _Entry:
        return self._rows[row - self._row_base]

    def _last_row(self) -> int:
        return self._row_base + len(self._rows) - 1

    # ── Row text ─────────────────────────────────────────────────────────

    def _time_text(self, entry: _Entry) -> str:
        moment = entry.record.timestamp
        if self._timestamp_mode == TimestampMode.RELATIVE:
            if entry.previous is None:
                return _FIRST_RELATIVE_TIME
            return f"{(moment - entry.previous).total_seconds():+.3f} s"
        return format_time_of_day(moment)

    def _prefix(self, entry: _Entry) -> str:
        """The time and level columns of a row, ending where the message starts."""
        label = tokens.tag_style(entry.record.tag).label
        return f"{self._time_text(entry):>{_TIME_COLS}}{_GAP}{label:<{_LEVEL_COLS}}{_GAP}"

    @staticmethod
    def _message(text: str) -> str:
        """A message as drawn: tabs become spaces up to the next tab stop."""
        return text.expandtabs(CONSOLE_TAB_WIDTH) if "\t" in text else text

    def _message_cols(self, text: str) -> int:
        return len(self._message(text))

    def _row_text(self, entry: _Entry) -> str:
        return self._prefix(entry) + self._message(entry.record.text)

    def _row_end(self, row: int) -> Position:
        return Position(row, len(self._row_text(self._entry(row))))

    @staticmethod
    def _raw_index(text: str, col: int) -> int:
        """Index in ``text`` of the character drawn at message column ``col``.

        A column inside the space a tab takes up belongs to that tab.
        """
        if "\t" not in text:
            return min(col, len(text))
        drawn = 0
        for index, char in enumerate(text):
            width = CONSOLE_TAB_WIDTH - drawn % CONSOLE_TAB_WIDTH if char == "\t" else 1
            if drawn + width > col:
                return index
            drawn += width
        return len(text)

    # ── Geometry ─────────────────────────────────────────────────────────

    def _apply_font(self) -> None:
        metrics = QFontMetricsF(self.font())
        sample = _FIXED_PITCH_SAMPLE
        wide = metrics.horizontalAdvance("M" * sample)
        self._cell_width = max(wide / sample, 1.0)
        self._fixed_pitch = all(
            abs(metrics.horizontalAdvance(text) - wide) < _FIXED_PITCH_TOLERANCE
            for text in ("i" * sample, "iM" * (sample // 2), " " * sample)
        )
        self._row_height = max(1, round(metrics.height()) + tokens.METRICS.space_1)
        self._ascent = metrics.ascent() + tokens.METRICS.space_1 / 2
        self._layouts.clear()
        self._max_width = self._prefix_width() + self._cell_width * max(
            (self._message_cols(e.record.text) for e in self._rows), default=0
        )
        self.horizontalScrollBar().setSingleStep(max(1, round(self._cell_width * 4)))
        self._update_scroll_ranges()
        self.viewport().update()

    def _prefix_width(self) -> float:
        return (_TIME_COLS + _LEVEL_COLS + 2 * len(_GAP)) * self._cell_width

    def _left_margin(self) -> float:
        return float(tokens.METRICS.space_3)

    def _rows_per_page(self) -> int:
        return max(1, self.viewport().height() // self._row_height)

    def _update_scroll_ranges(self, *, evicted_rows: int = 0, added_rows: int = 0) -> None:
        """Fit the scroll bars to the rows, keeping the view where it is.

        While following, the view moves to the newest row. Otherwise it
        stays on the rows it shows, even as older rows leave the store.
        """
        bar = self.verticalScrollBar()
        page = self._rows_per_page()
        maximum = max(0, len(self._rows) - page)
        value = bar.value()

        self._adjusting_scroll = True
        try:
            bar.setPageStep(page)
            bar.setRange(0, maximum)
            if self._following:
                bar.setValue(maximum)
            elif evicted_rows:
                bar.setValue(max(0, value - evicted_rows))
        finally:
            self._adjusting_scroll = False

        if not self._following and added_rows:
            self._unseen += added_rows
            self.unseen_lines_changed.emit(self._unseen)

        horizontal = self.horizontalScrollBar()
        width = self.viewport().width()
        horizontal.setPageStep(width)
        horizontal.setRange(
            0, max(0, round(self._max_width + 2 * self._left_margin()) - width)
        )

    def _on_vertical_scroll(self, value: int) -> None:
        if not self._adjusting_scroll:
            at_end = value >= self.verticalScrollBar().maximum()
            self._set_following(at_end and self._auto_follow)
        self.viewport().update()

    def _set_following(self, following: bool) -> None:
        if following and self._unseen:
            self._unseen = 0
            self.unseen_lines_changed.emit(0)
        if following != self._following:
            self._following = following
            self.following_changed.emit(following)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._update_scroll_ranges()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.PaletteChange, QEvent.Type.StyleChange):
            # The theme changed: colours baked into cached layouts are stale.
            self._layouts.clear()
            self.viewport().update()

    # ── Text shaping ─────────────────────────────────────────────────────

    def _needs_layout(self, text: str) -> bool:
        """True when a row cannot be drawn by counting character cells."""
        return not (self._fixed_pitch and text.isascii())

    def _layout(self, row: int, entry: _Entry, text: str) -> QTextLayout:
        """A shaped layout of a row, for text that is not plain ASCII.

        Shaping handles combining marks, wide characters, right-to-left
        scripts and fonts substituted for missing glyphs.
        """
        cached = self._layouts.get(row)
        if cached is not None:
            return cached
        if len(self._layouts) >= _LAYOUT_CACHE_ROWS:
            self._layouts.clear()

        # The message of a very long row is shaped up to a limit; see
        # the constant.
        prefix_cols = len(text) - self._message_cols(entry.record.text)
        text = text[:prefix_cols + CONSOLE_SHAPED_CHARACTERS]
        layout = QTextLayout(text, self.font())
        layout.setCacheEnabled(True)
        # Rows always run left to right, so the time column stays on the
        # left even when a message begins in a right-to-left script. The
        # script itself is still drawn in its own direction.
        option = QTextOption()
        option.setTextDirection(Qt.LayoutDirection.LeftToRight)
        option.setWrapMode(QTextOption.WrapMode.NoWrap)
        layout.setTextOption(option)
        layout.setFormats([
            self._format_range(start, end - start, colour)
            for start, end, colour in self._spans(entry, text, prefix_cols)
        ])
        layout.beginLayout()
        line = layout.createLine()
        line.setLineWidth(_UNBOUNDED_LINE_WIDTH)
        line.setPosition(QPointF(0.0, 0.0))
        layout.endLayout()

        width = line.naturalTextWidth()
        if width > self._max_width:
            self._max_width = width
            QTimer.singleShot(0, self._update_scroll_ranges)
        self._layouts[row] = layout
        return layout

    @staticmethod
    def _format_range(
        start: int, length: int, colour: QColor | None, background: QColor | None = None,
    ) -> QTextLayout.FormatRange:
        """A span of a layout. A colour of None keeps the text's own colour."""
        char_format = QTextCharFormat()
        if colour is not None:
            char_format.setForeground(colour)
        if background is not None:
            char_format.setBackground(background)
        span = QTextLayout.FormatRange()
        span.start = start
        span.length = length
        span.format = char_format
        return span

    def _x_of(self, row: int, entry: _Entry, text: str, col: int) -> float:
        """Distance from the start of a row to column ``col``, in pixels."""
        if not self._needs_layout(text):
            return col * self._cell_width
        position, _ = self._layout(row, entry, text).lineAt(0).cursorToX(col)
        return position

    def _col_at(self, row: int, entry: _Entry, text: str, x: float) -> int:
        """Column nearest to ``x`` pixels from the start of a row."""
        if x <= 0:
            return 0
        if not self._needs_layout(text):
            return min(len(text), round(x / self._cell_width))
        return self._layout(row, entry, text).lineAt(0).xToCursor(x)

    def _position_at(self, point: QPoint) -> Position | None:
        """The text position under a point of the viewport."""
        if not self._rows:
            return None
        index = self.verticalScrollBar().value() + max(0, point.y()) // self._row_height
        if index >= len(self._rows):
            return self._row_end(self._last_row())
        row = self._row_base + index
        entry = self._rows[index]
        text = self._row_text(entry)
        x = point.x() - self._left_margin() + self.horizontalScrollBar().value()
        return Position(row, self._col_at(row, entry, text, x))

    # ── Painting ─────────────────────────────────────────────────────────

    def _row_colours(self, record: LineRecord) -> tuple[QColor, QColor, QColor]:
        """Colours of the time, level and message of a row."""
        palette = tokens.palette()
        time_colour = QColor(palette.text_secondary)
        if not self._severity_colours:
            return time_colour, QColor(palette.text), QColor(palette.text)
        style = tokens.tag_style(record.tag)
        return (
            time_colour,
            QColor(getattr(palette, style.label_role)),
            QColor(getattr(palette, style.text_role)),
        )

    def _spans(
        self, entry: _Entry, text: str, prefix_cols: int,
    ) -> list[tuple[int, int, QColor]]:
        """The colour of each part of ``text``, in order: time, level, message.

        ``prefix_cols`` is where the message starts; ``text`` may be the
        row shortened for shaping.

        In a row of the HEX display, the parts that are its structure (the
        offset, the bars and the dots for unprintable bytes) are drawn in
        the secondary colour, so the bytes and characters stand out.
        """
        time_colour, level_colour, message_colour = self._row_colours(entry.record)
        level_start = prefix_cols - _LEVEL_COLS - len(_GAP)
        spans = [(0, level_start, time_colour), (level_start, prefix_cols, level_colour)]
        position = prefix_cols
        if entry.record.hex_row:
            quiet = QColor(tokens.palette().text_secondary)
            for start, end in hex_row_marks(entry.record.text):
                start, end = prefix_cols + start, min(len(text), prefix_cols + end)
                if start >= end:
                    continue
                if start > position:
                    spans.append((position, start, message_colour))
                spans.append((start, end, quiet))
                position = end
        spans.append((position, len(text), message_colour))
        return spans

    def _row_tint(self, record: LineRecord) -> QColor | None:
        if not self._severity_colours:
            return None
        role = tokens.tag_style(record.tag).row_role
        return QColor(getattr(tokens.palette(), role)) if role else None

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self.viewport())
        palette = tokens.palette()
        painter.fillRect(event.rect(), QColor(palette.paper))
        painter.setFont(self.font())

        first = self.verticalScrollBar().value()
        top = event.rect().top() // self._row_height
        bottom = event.rect().bottom() // self._row_height
        selection = self._selection()
        for line in range(top, bottom + 1):
            index = first + line
            if index >= len(self._rows):
                break
            self._paint_row(
                painter, self._row_base + index, self._rows[index],
                line * self._row_height, selection,
            )
        painter.end()

    def _paint_row(
        self,
        painter: QPainter,
        row: int,
        entry: _Entry,
        y: int,
        selection: tuple[Position, Position] | None,
    ) -> None:
        palette = tokens.palette()
        text = self._row_text(entry)
        width = self.viewport().width()
        origin = self._left_margin() - self.horizontalScrollBar().value()

        tint = self._row_tint(entry.record)
        if tint is not None:
            painter.fillRect(QRectF(0, y, width, self._row_height), tint)

        # Marked ranges of this row: (start, end, background, text colour).
        # A text colour of None leaves the row's own colours in place.
        # Selected text is drawn in the text colour, the one colour the
        # design language checks against the selection.
        marks: list[tuple[int, int, QColor, QColor | None]] = []
        line_end: QColor | None = None
        if selection is not None and selection[0].row <= row <= selection[1].row:
            start = selection[0].col if row == selection[0].row else 0
            end = selection[1].col if row == selection[1].row else len(text)
            marks.append(
                (start, min(end, len(text)), QColor(palette.selection), QColor(palette.text))
            )
            if row < selection[1].row:
                # The selection runs on to the next row: mark the line end too.
                line_end = QColor(palette.selection)
        for col, length in self._matches_by_row.get(row, ()):
            if self._is_current_match(row, col):
                marks.append((col, col + length, QColor(palette.text), QColor(palette.paper)))
            else:
                marks.append((col, col + length, QColor(palette.search_hit), QColor(palette.text)))

        if self._needs_layout(text):
            end_x = self._paint_shaped(painter, row, entry, text, origin, y, marks)
        else:
            end_x = self._paint_cells(painter, entry, text, origin, y, marks)
        if line_end is not None:
            painter.fillRect(
                QRectF(end_x, y, self._cell_width, self._row_height), line_end
            )

    def _paint_cells(
        self,
        painter: QPainter,
        entry: _Entry,
        text: str,
        origin: float,
        y: int,
        marks: list[tuple[int, int, QColor, QColor | None]],
    ) -> float:
        """Draw an ASCII row in a fixed-width font by counting cells.

        Only the part of the row inside the viewport is drawn, so a very
        long line costs no more than a short one. Returns the x position
        of the end of the row.
        """
        cell = self._cell_width
        width = self.viewport().width()
        first = max(0, int(-origin // cell))
        last = min(len(text), int((width - origin) // cell) + 1)
        baseline = y + self._ascent

        for start, end, background, _ in marks:
            left = origin + start * cell
            painter.fillRect(
                QRectF(left, y, (end - start) * cell, self._row_height), background
            )

        prefix_cols = len(text) - self._message_cols(entry.record.text)
        for start, end, colour in self._spans(entry, text, prefix_cols):
            start, end = max(start, first), min(end, last)
            if start < end:
                painter.setPen(colour)
                painter.drawText(QPointF(origin + start * cell, baseline), text[start:end])

        for start, end, _, colour in marks:
            start, end = max(start, first), min(end, last)
            if colour is not None and start < end:
                painter.setPen(colour)
                painter.drawText(QPointF(origin + start * cell, baseline), text[start:end])
        return origin + len(text) * cell

    def _paint_shaped(
        self,
        painter: QPainter,
        row: int,
        entry: _Entry,
        text: str,
        origin: float,
        y: int,
        marks: list[tuple[int, int, QColor, QColor | None]],
    ) -> float:
        """Draw a row through a shaped layout. Returns the x position of its end."""
        layout = self._layout(row, entry, text)
        line = layout.lineAt(0)
        overlays = [
            self._format_range(start, end - start, colour, background)
            for start, end, background, colour in marks
            if start < end
        ]
        offset = (self._row_height - line.height()) / 2
        layout.draw(painter, QPointF(origin, y + offset), overlays)
        return origin + line.naturalTextWidth()

    # ── Selection internals ──────────────────────────────────────────────────

    def _selection(self) -> tuple[Position, Position] | None:
        """The selected range, ordered, or None when nothing is selected."""
        if self._anchor is None or self._cursor is None or self._anchor == self._cursor:
            return None
        start, end = sorted((self._anchor, self._cursor))
        return start, end

    def _set_selection(self, anchor: Position, cursor: Position) -> None:
        if (anchor, cursor) == (self._anchor, self._cursor):
            return
        self._anchor, self._cursor = anchor, cursor
        self.viewport().update()
        self.selection_changed.emit()

    def _clear_selection(self) -> None:
        if self._anchor is None and self._cursor is None:
            return
        had_selection = self._selection() is not None
        self._anchor = self._cursor = None
        if had_selection:
            self.selection_changed.emit()

    def _selected(self, *, with_prefix: bool) -> str:
        selection = self._selection()
        if selection is None:
            return ""
        start, end = selection
        parts: list[str] = []
        for row in range(start.row, end.row + 1):
            entry = self._entry(row)
            prefix = self._prefix(entry)
            raw = entry.record.text
            first = start.col if row == start.row else 0
            last = end.col if row == end.row else len(prefix) + self._message_cols(raw)
            shown_prefix = prefix[first:last] if with_prefix else ""
            message_first = self._raw_index(raw, max(0, first - len(prefix)))
            message_last = self._raw_index(raw, max(0, last - len(prefix)))
            parts.append(shown_prefix + raw[message_first:message_last])
        return "\n".join(parts)

    def _move_cursor(self, target: Position, *, extend: bool) -> None:
        """Move the selection end, or start a new selection at ``target``."""
        anchor = self._anchor if extend and self._anchor is not None else target
        self._set_selection(anchor, target)
        self._ensure_row_visible(target.row)

    def _ensure_row_visible(self, row: int) -> None:
        bar = self.verticalScrollBar()
        index = row - self._row_base
        page = self._rows_per_page()
        if index < bar.value():
            bar.setValue(index)
        elif index >= bar.value() + page:
            bar.setValue(index - page + 1)

    # ── Mouse ────────────────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        position = self._position_at(event.position().toPoint())
        if position is None:
            return
        extend = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        anchor = self._anchor if extend and self._anchor is not None else position
        self._set_selection(anchor, position)
        self._dragging = True

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging or self._anchor is None:
            return
        point = event.position().toPoint()
        position = self._position_at(point)
        if position is not None:
            self._set_selection(self._anchor, position)
        if point.y() < 0:
            self._drag_direction = -1
        elif point.y() >= self.viewport().height():
            self._drag_direction = 1
        else:
            self._drag_direction = 0
        if self._drag_direction and not self._drag_timer.isActive():
            self._drag_timer.start()
        elif not self._drag_direction:
            self._drag_timer.stop()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self._drag_timer.stop()
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        """Select the word under the pointer."""
        if event.button() != Qt.MouseButton.LeftButton:
            return
        position = self._position_at(event.position().toPoint())
        if position is None:
            return
        text = self._row_text(self._entry(position.row))
        for match in _WORD.finditer(text):
            if match.start() <= position.col <= match.end():
                self._set_selection(
                    Position(position.row, match.start()),
                    Position(position.row, match.end()),
                )
                return

    def _on_drag_scroll(self) -> None:
        """Keep scrolling while a drag is held past the top or bottom edge."""
        if not self._dragging or self._anchor is None or not self._rows:
            self._drag_timer.stop()
            return
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() + self._drag_direction)
        if self._drag_direction < 0:
            edge = Position(self._row_base + bar.value(), 0)
        else:
            index = min(len(self._rows), bar.value() + self._rows_per_page()) - 1
            edge = self._row_end(self._row_base + index)
        self._set_selection(self._anchor, edge)

    # ── Keyboard ─────────────────────────────────────────────────────────

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy()
            return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self.select_all()
            return

        key = event.key()
        extend = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if extend and self._rows and self._move_by_key(key):
            return
        if not extend and self._scroll_by_key(key):
            return
        super().keyPressEvent(event)

    def _scroll_by_key(self, key: int) -> bool:
        bar = self.verticalScrollBar()
        horizontal = self.horizontalScrollBar()
        page = self._rows_per_page()
        if key == Qt.Key.Key_Up:
            bar.setValue(bar.value() - 1)
        elif key == Qt.Key.Key_Down:
            bar.setValue(bar.value() + 1)
        elif key == Qt.Key.Key_PageUp:
            bar.setValue(bar.value() - page)
        elif key == Qt.Key.Key_PageDown:
            bar.setValue(bar.value() + page)
        elif key == Qt.Key.Key_Home:
            bar.setValue(0)
        elif key == Qt.Key.Key_End:
            self.follow() if self._auto_follow else bar.setValue(bar.maximum())
        elif key == Qt.Key.Key_Left:
            horizontal.setValue(horizontal.value() - horizontal.singleStep())
        elif key == Qt.Key.Key_Right:
            horizontal.setValue(horizontal.value() + horizontal.singleStep())
        else:
            return False
        return True

    def _move_by_key(self, key: int) -> bool:
        """Extend the selection with Shift and a navigation key."""
        cursor = self._cursor or self._row_end(self._last_row())
        first, last = self._row_base, self._last_row()
        page = self._rows_per_page()
        length = len(self._row_text(self._entry(cursor.row)))

        if key == Qt.Key.Key_Left:
            target = Position(cursor.row, max(0, cursor.col - 1))
        elif key == Qt.Key.Key_Right:
            target = Position(cursor.row, min(length, cursor.col + 1))
        elif key == Qt.Key.Key_Up:
            target = Position(max(first, cursor.row - 1), cursor.col)
        elif key == Qt.Key.Key_Down:
            target = Position(min(last, cursor.row + 1), cursor.col)
        elif key == Qt.Key.Key_PageUp:
            target = Position(max(first, cursor.row - page), cursor.col)
        elif key == Qt.Key.Key_PageDown:
            target = Position(min(last, cursor.row + page), cursor.col)
        elif key == Qt.Key.Key_Home:
            target = Position(first, 0)
        elif key == Qt.Key.Key_End:
            target = self._row_end(last)
        else:
            return False

        target_length = len(self._row_text(self._entry(target.row)))
        target = Position(target.row, min(target.col, target_length))
        if self._anchor is None:
            self._anchor = cursor
        self._move_cursor(target, extend=True)
        return True

    # ── Search internals ─────────────────────────────────────────────────────

    def _search_again(self) -> None:
        """Find every match in the shown rows from scratch."""
        self._matches.clear()
        self._matches_by_row.clear()
        self._current_match = -1
        if self._search_pattern is not None:
            row = self._row_base
            for entry in self._rows:
                if not self._find_in_row(row, entry):
                    break
                row += 1
            if self._matches:
                self._current_match = 0
        self.search_changed.emit(len(self._matches), self._current_match)

    def _match_may_start_before_message(self, tag: str) -> bool:
        """Whether a match could begin in the time or level column of a row.

        Building those columns for every row is most of the cost of a
        search. A search that starts with an ASCII letter can only begin
        there on the letters of the row's level label, or on the unit of
        a relative time, so most rows need their message alone.
        """
        first = self._search_first
        if not (first.isascii() and first.isalpha()):
            return True
        if first == "s" and self._timestamp_mode == TimestampMode.RELATIVE:
            return True
        return first in tokens.tag_style(tag).label.lower()

    def _find_in_row(self, row: int, entry: _Entry) -> bool:
        """Record the matches in one row. Returns False once the limit is reached."""
        if len(self._matches) >= CONSOLE_SEARCH_MATCH_LIMIT:
            return False
        pattern = self._search_pattern
        if self._match_may_start_before_message(entry.record.tag):
            text = self._row_text(entry)
            offset = 0
        else:
            text = self._message(entry.record.text)
            if pattern.search(text) is None:
                return True
            offset = len(self._prefix(entry))

        found: list[tuple[int, int]] = []
        for match in pattern.finditer(text):
            if len(self._matches) >= CONSOLE_SEARCH_MATCH_LIMIT:
                break
            col, length = offset + match.start(), match.end() - match.start()
            found.append((col, length))
            self._matches.append((row, col, length))
        if found:
            self._matches_by_row[row] = found
        return True

    def _is_current_match(self, row: int, col: int) -> bool:
        if self._current_match < 0:
            return False
        current = self._matches[self._current_match]
        return current[0] == row and current[1] == col

    def _go_to_match(self, index: int) -> None:
        self._current_match = index
        row, col, length = self._matches[index]
        self._ensure_row_visible(row)

        entry = self._entry(row)
        text = self._row_text(entry)
        left = self._x_of(row, entry, text, col)
        right = self._x_of(row, entry, text, col + length)
        horizontal = self.horizontalScrollBar()
        width = self.viewport().width() - 2 * self._left_margin()
        if left < horizontal.value():
            horizontal.setValue(int(left))
        elif right > horizontal.value() + width:
            horizontal.setValue(int(right - width))

        self.viewport().update()
        self.search_changed.emit(len(self._matches), self._current_match)
