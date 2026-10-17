# SPDX-License-Identifier: GPL-3.0-or-later
"""The Find row: search the console and step through the matches.

Hidden until Find is asked for. The console finds and draws the
matches; this row holds the text box, the two step buttons and the
count.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLineEdit,
    QWidget,
)

from app.constants import CONSOLE_SEARCH_MATCH_LIMIT, SEARCH_DEBOUNCE_MS
from ui import shortcuts
from ui.themes import tokens
from ui.themes.icons import bind_icon
from ui.widgets.button import Button
from ui.widgets.console_view import ConsoleView
from ui.widgets.styling import SECONDARY, plain_label, set_compact


class SearchBar(QWidget):
    """Find row with previous and next.

    Signals:
        search_changed(str):       The text in the box changed.
        visibility_changed(bool):  The row was opened or closed.
    """

    search_changed = Signal(str)
    visibility_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("searchBar")
        self._console: ConsoleView | None = None
        # A search over a full console takes long enough to be felt, so
        # it runs once typing pauses and not on every keystroke.
        self._typing_timer = QTimer(self)
        self._typing_timer.setSingleShot(True)
        self._typing_timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._typing_timer.timeout.connect(self._search_now)
        self._setup_ui()
        self.hide()

    def set_console(self, console: ConsoleView) -> None:
        self._console = console
        console.search_changed.connect(self._on_matches_changed)

    def activate(self) -> None:
        self.show()
        self._input.setFocus()
        self._input.selectAll()
        self._search_now()
        self.visibility_changed.emit(True)

    def deactivate(self) -> None:
        self.hide()
        self._input.clear()
        self._search_now()
        self._count_label.setText("")
        self.visibility_changed.emit(False)

    @property
    def search_text(self) -> str:
        return self._input.text()

    def search_for(self, text: str) -> None:
        """Open the bar and search for ``text`` at once."""
        self.show()
        self._input.setText(text)
        self._search_now()
        self.visibility_changed.emit(True)

    def find_next(self) -> None:
        if self._console is None:
            return
        if self._typing_timer.isActive():
            # The first match of the text just typed is the "next" one.
            self._search_now()
        else:
            self._console.search_next()

    def find_previous(self) -> None:
        if self._console is None:
            return
        if self._typing_timer.isActive():
            self._search_now()
        self._console.search_previous()

    def _setup_ui(self) -> None:
        metrics = tokens.METRICS
        self.setFixedHeight(metrics.header_height)
        # A plain QWidget draws its stylesheet background only with this.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)

        self._input = QLineEdit()
        self._input.setPlaceholderText("Find in the console")
        set_compact(self._input)
        self._input.setFixedWidth(metrics.filter_field_width)
        self._input.setClearButtonEnabled(True)
        self._input.setAccessibleName("Find")
        self._input.textChanged.connect(self._on_text_changed)
        self._input.returnPressed.connect(self.find_next)

        self._prev_btn = self._icon_button(
            "chevron_up", "Previous match", "find_previous", self.find_previous,
        )
        self._next_btn = self._icon_button(
            "chevron_down", "Next match", "find_next", self.find_next,
        )
        self._count_label = plain_label("")
        self._count_label.setProperty(SECONDARY, True)
        close = self._icon_button("close_tab", "Close Find", "close_find", self.deactivate)
        close.setObjectName("flatIconButton")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(metrics.space_3, 0, metrics.space_3, 0)
        layout.setSpacing(metrics.space_2)
        layout.addWidget(self._input)
        layout.addWidget(self._prev_btn)
        layout.addWidget(self._next_btn)
        layout.addWidget(self._count_label)
        layout.addStretch(1)
        layout.addWidget(close)

    @staticmethod
    def _icon_button(icon_name: str, text: str, shortcut: str, run: Callable[[], None]) -> Button:
        button = Button(compact=True)
        button.setObjectName("iconButton")
        button.setToolTip(shortcuts.with_keys(text, shortcut))
        button.setAccessibleName(text)
        bind_icon(button, icon_name, "text_secondary")
        button.clicked.connect(run)
        return button

    def _on_text_changed(self, text: str) -> None:
        self.search_changed.emit(text)
        self._typing_timer.start()

    def _search_now(self) -> None:
        self._typing_timer.stop()
        if self._console is not None:
            self._console.set_search(self._input.text())

    def _on_matches_changed(self, count: int, current: int) -> None:
        if self.isHidden() or not self._input.text():
            self._count_label.setText("")
        elif count == 0:
            self._count_label.setText("No results")
        else:
            more = "+" if count >= CONSOLE_SEARCH_MATCH_LIMIT else ""
            self._count_label.setText(f"{current + 1:,} of {count:,}{more}")
