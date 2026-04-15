# SPDX-License-Identifier: MIT
"""WireTrace search bar — find and highlight text in console with navigation.

Layout matches CommandBar and FilterBar exactly:
  - Margins: 8, 3, 8, 3
  - Spacing: 6
  - Label: bold, 64px fixed width
  - Input: height 26

Hidden by default. Shown via Ctrl+F, dismissed via Escape.
Appears directly below the filter bar for perfect vertical alignment.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QWidget,
)

_HIGHLIGHT_BG = QColor("#FFEB3B")
_CURRENT_BG = QColor("#FF9800")

_LABEL_W = 64  # Must match CommandBar._LABEL_W and FilterBar._LABEL_W


class SearchBar(QWidget):
    """Search bar with highlight and prev/next navigation.

    Hidden by default. Appears below FilterBar when activated (Ctrl+F).
    All labels, inputs, and spacing are pixel-aligned with CommandBar
    and FilterBar for a uniform, professional appearance.
    """

    search_changed = Signal(str)
    visibility_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("searchBar")

        self._console: QPlainTextEdit | None = None
        self._matches: list[QTextCursor] = []
        self._idx = -1

        self._setup_ui()
        self.hide()

    def set_console(self, console: QPlainTextEdit) -> None:
        self._console = console

    def activate(self) -> None:
        self.show()
        self._input.setFocus()
        self._input.selectAll()
        self.visibility_changed.emit(True)

    def deactivate(self) -> None:
        self.hide()
        self._clear_highlights()
        self._input.clear()
        self._matches.clear()
        self._idx = -1
        self._count_label.setText("")
        self.visibility_changed.emit(False)

    @property
    def search_text(self) -> str:
        return self._input.text()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(6)

        lbl = QLabel("Search")
        sf = lbl.font()
        sf.setWeight(QFont.Weight.DemiBold)
        sf.setPointSize(10)
        lbl.setFont(sf)
        lbl.setFixedWidth(_LABEL_W)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Find text...")
        self._input.setMaximumWidth(260)
        self._input.setFixedHeight(26)
        self._input.setClearButtonEnabled(True)
        self._input.textChanged.connect(self._on_text_changed)
        self._input.returnPressed.connect(self._on_next)

        self._prev_btn = QPushButton("<")
        self._prev_btn.setObjectName("searchNavBtn")
        self._prev_btn.setFixedSize(26, 26)
        self._prev_btn.setToolTip("Previous (Shift+F3)")
        self._prev_btn.clicked.connect(self._on_prev)

        self._next_btn = QPushButton(">")
        self._next_btn.setObjectName("searchNavBtn")
        self._next_btn.setFixedSize(26, 26)
        self._next_btn.setToolTip("Next (F3)")
        self._next_btn.clicked.connect(self._on_next)

        self._count_label = QLabel("")
        self._count_label.setMinimumWidth(48)

        layout.addWidget(lbl)
        layout.addWidget(self._input)
        layout.addWidget(self._prev_btn)
        layout.addWidget(self._next_btn)
        layout.addWidget(self._count_label)
        layout.addStretch()

    def _on_text_changed(self, text: str) -> None:
        self._clear_highlights()
        self._matches.clear()
        self._idx = -1
        self.search_changed.emit(text)

        if not text or not self._console:
            self._count_label.setText("")
            return

        doc = self._console.document()
        cursor = QTextCursor(doc)
        flags = QTextDocument.FindFlag(0)

        while True:
            cursor = doc.find(text, cursor, flags)
            if cursor.isNull():
                break
            self._matches.append(QTextCursor(cursor))

        self._apply_highlights()

        if self._matches:
            self._idx = 0
            self._mark_current()
            self._scroll_to_current()

        self._update_count()

    def _on_next(self) -> None:
        if not self._matches:
            return
        self._idx = (self._idx + 1) % len(self._matches)
        self._apply_highlights()
        self._mark_current()
        self._scroll_to_current()
        self._update_count()

    def _on_prev(self) -> None:
        if not self._matches:
            return
        self._idx = (self._idx - 1) % len(self._matches)
        self._apply_highlights()
        self._mark_current()
        self._scroll_to_current()
        self._update_count()

    def _apply_highlights(self) -> None:
        if not self._console:
            return
        fmt = QTextCharFormat()
        fmt.setBackground(_HIGHLIGHT_BG)
        sels = []
        for c in self._matches:
            sel = QTextEdit.ExtraSelection()
            sel.format = fmt
            sel.cursor = c
            sels.append(sel)
        self._console.setExtraSelections(sels)

    def _mark_current(self) -> None:
        if not self._console or self._idx < 0:
            return
        sels = list(self._console.extraSelections())
        if 0 <= self._idx < len(sels):
            fmt = QTextCharFormat()
            fmt.setBackground(_CURRENT_BG)
            sels[self._idx].format = fmt
            self._console.setExtraSelections(sels)

    def _clear_highlights(self) -> None:
        if self._console:
            self._console.setExtraSelections([])

    def _scroll_to_current(self) -> None:
        if not self._console or self._idx < 0 or self._idx >= len(self._matches):
            return
        self._console.setTextCursor(self._matches[self._idx])
        self._console.ensureCursorVisible()

    def _update_count(self) -> None:
        n = len(self._matches)
        self._count_label.setText(f"{self._idx + 1}/{n}" if n else "No results")
