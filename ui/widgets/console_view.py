# SPDX-License-Identifier: MIT
"""WireTrace high-performance console log viewport.

QPlainTextEdit-based display with:
  - Virtual viewport rendering (only visible lines are painted)
  - Color-coded severity tags per theme
  - Auto-scroll with smart lock (pauses when user scrolls up)
  - Line count management with configurable max
  - Filter-aware display (hides non-matching lines visually)
  - Dynamic timestamp re-rendering (absolute ↔ relative toggle updates ALL lines)
  - Theme-safe rebuild (re-renders all lines when tag colors change)

This is a display-only widget — it does NOT touch business logic, disk, or serial.
"""

from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import Signal, Slot
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from app.constants import (
    DEFAULT_FONT_FAMILY,
    DEFAULT_FONT_SIZE,
    MAX_CONSOLE_LINES,
    TimestampMode,
)

logger = logging.getLogger(__name__)


class ConsoleView(QPlainTextEdit):
    """High-performance log display widget.

    Signals:
        line_count_changed(int):  Emitted when visible line count changes.
    """

    line_count_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        # Read-only display
        self.setReadOnly(True)
        self.setUndoRedoEnabled(False)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setMaximumBlockCount(MAX_CONSOLE_LINES)

        # Font
        font = QFont(DEFAULT_FONT_FAMILY, DEFAULT_FONT_SIZE)
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.setFont(font)

        # Performance: disable cursor blinking in read-only mode
        self.setCursorWidth(0)

        # Auto-scroll state
        self._auto_scroll = True
        self._user_scrolled = False

        # Tag colors: tag → QTextCharFormat (set by theme manager)
        self._tag_formats: dict[str, QTextCharFormat] = {}

        # Timestamp mode
        self._timestamp_mode = TimestampMode.ABSOLUTE

        # Filtering
        self._filter_text = ""

        # Line storage: each entry stores the RAW datetime so we can
        # dynamically re-render timestamps when the user toggles mode.
        self._all_lines: list[tuple[datetime, str, str]] = []
        self._visible_count = 0
        self._total_count = 0

        # Connect scroll bar to detect user scroll
        vbar = self.verticalScrollBar()
        vbar.valueChanged.connect(self._on_scroll_changed)

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def auto_scroll(self) -> bool:
        return self._auto_scroll

    @auto_scroll.setter
    def auto_scroll(self, enabled: bool) -> None:
        self._auto_scroll = enabled
        if enabled:
            self._scroll_to_bottom()

    @property
    def filter_text(self) -> str:
        return self._filter_text

    @property
    def visible_count(self) -> int:
        return self._visible_count

    @property
    def total_count(self) -> int:
        return self._total_count

    def get_all_lines(self) -> list[tuple[datetime, str, str]]:
        """Return all stored lines as (datetime, line_text, tag) tuples.

        Used by the Export feature to snapshot the current console content.
        The returned list is a shallow copy — safe to iterate outside the UI thread.
        """
        return list(self._all_lines)

    def set_font_family(self, family: str) -> None:
        font = self.font()
        font.setFamily(family)
        self.setFont(font)

    def set_font_size(self, size: int) -> None:
        font = self.font()
        font.setPointSize(size)
        self.setFont(font)

    def set_max_lines(self, max_lines: int) -> None:
        self.setMaximumBlockCount(max_lines)

    def set_tag_colors(self, tag_colors: dict[str, tuple[str, bool]]) -> None:
        """Set tag color formatting from the theme manager.

        Also triggers a full rebuild so existing lines adopt the new colors.
        """
        self._tag_formats.clear()
        for tag, (color, bold) in tag_colors.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            if bold:
                fmt.setFontWeight(QFont.Weight.Bold)
            self._tag_formats[tag] = fmt

        # Rebuild display so ALL existing lines get new theme colors
        if self._all_lines:
            self._rebuild_display()

    def set_timestamp_mode(self, mode: TimestampMode) -> None:
        """Change the timestamp display mode and re-render ALL lines.

        When the user toggles between Absolute and Relative, every stored
        line is re-rendered with the new timestamp format. This means:
          - Switching to Relative: all lines show delta from previous
          - Switching to Absolute: all lines show their original wall-clock time
        """
        if mode == self._timestamp_mode:
            return
        self._timestamp_mode = mode
        self._rebuild_display()

    @Slot(str, str)
    def append_line(self, line: str, tag: str) -> None:
        """Append a new line to the console with color formatting."""
        now = datetime.now()

        # Store raw datetime for dynamic re-rendering
        self._total_count += 1
        self._all_lines.append((now, line, tag))

        # Apply filter
        if self._filter_text and self._filter_text not in line.lower():
            self.line_count_changed.emit(self._total_count)
            return

        # Format and display
        ts_str = self._format_timestamp_for_index(len(self._all_lines) - 1)
        self._append_formatted(ts_str, line, tag)
        self._visible_count += 1

        # Auto-scroll
        if self._auto_scroll and not self._user_scrolled:
            self._scroll_to_bottom()

        self.line_count_changed.emit(self._total_count)

    def set_filter(self, text: str) -> None:
        """Apply a filter — only matching lines are shown."""
        self._filter_text = text.lower().strip()
        self._rebuild_display()

    def clear_filter(self) -> None:
        self._filter_text = ""
        self._rebuild_display()

    def clear_console(self) -> None:
        self.clear()
        self._all_lines.clear()
        self._visible_count = 0
        self._total_count = 0
        self.line_count_changed.emit(0)

    # ── Internal ─────────────────────────────────────────────────────────

    def _append_formatted(self, timestamp: str, line: str, tag: str) -> None:
        """Append a formatted line with timestamp and tag color."""
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        ts_fmt = self._tag_formats.get("TIMESTAMP", QTextCharFormat())
        cursor.setCharFormat(ts_fmt)
        cursor.insertText(f"[{timestamp}] ")

        line_fmt = self._tag_formats.get(tag, QTextCharFormat())
        cursor.setCharFormat(line_fmt)
        cursor.insertText(line)

        cursor.insertText("\n")

    def _rebuild_display(self) -> None:
        """Re-render ALL lines with current timestamp mode, theme, and filter."""
        self.clear()
        self._visible_count = 0

        for i, (dt, line, tag) in enumerate(self._all_lines):
            if self._filter_text and self._filter_text not in line.lower():
                continue

            ts_str = self._format_timestamp_for_index(i)
            self._append_formatted(ts_str, line, tag)
            self._visible_count += 1

        if self._auto_scroll:
            self._scroll_to_bottom()

        self.line_count_changed.emit(self._total_count)

    def _format_timestamp_for_index(self, index: int) -> str:
        """Format a timestamp for the line at the given index.

        In ABSOLUTE mode: wall-clock time.
        In RELATIVE mode: delta from the previous line.
        """
        dt = self._all_lines[index][0]

        if self._timestamp_mode == TimestampMode.RELATIVE:
            if index == 0:
                return "+Δ 0.000s"
            prev_dt = self._all_lines[index - 1][0]
            delta = (dt - prev_dt).total_seconds()
            return f"+Δ {delta:.3f}s"
        else:
            return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"

    def _scroll_to_bottom(self) -> None:
        vbar = self.verticalScrollBar()
        vbar.setValue(vbar.maximum())

    def _on_scroll_changed(self, value: int) -> None:
        vbar = self.verticalScrollBar()
        at_bottom = value >= vbar.maximum() - 5
        self._user_scrolled = not at_bottom
        if at_bottom:
            self._user_scrolled = False
