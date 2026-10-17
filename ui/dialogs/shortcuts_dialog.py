# SPDX-License-Identifier: GPL-3.0-or-later
"""The list of keyboard shortcuts, opened from the Help menu.

It is built from ``ui.shortcuts``, the table the menus use, so it cannot
list a shortcut that does not work.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from ui import shortcuts
from ui.themes import tokens
from ui.widgets.button import Button
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size
from ui.widgets.styling import SECONDARY, plain_label, set_primary
from ui.widgets.vertical_scroll import VerticalScrollArea


class ShortcutsDialog(DotGridDialog):
    """Every keyboard shortcut, grouped the way the menus are."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.setModal(True)
        metrics = tokens.METRICS

        grid = QGridLayout()
        grid.setHorizontalSpacing(metrics.space_6)
        grid.setVerticalSpacing(metrics.space_2)
        grid.setColumnStretch(1, 1)
        grid.setContentsMargins(0, 0, metrics.space_6, 0)
        self._rows: list[tuple[str, str]] = []
        row = 0
        group = ""
        for name, shortcut in shortcuts.SHORTCUTS.items():
            if shortcut.group != group:
                group = shortcut.group
                if row:
                    grid.setRowMinimumHeight(row, metrics.space_3)
                    row += 1
                heading = plain_label(group)
                heading.setObjectName("sectionTitle")
                grid.addWidget(heading, row, 0, 1, 2)
                row += 1
            keys = plain_label(shortcuts.native_keys(name))
            keys.setObjectName("shortcutKeys")
            description = plain_label(shortcut.description)
            description.setProperty(SECONDARY, True)
            grid.addWidget(keys, row, 0)
            grid.addWidget(description, row, 1)
            self._rows.append((keys.text(), description.text()))
            row += 1

        content = QWidget()
        content.setLayout(grid)
        scroll = VerticalScrollArea(content)

        close = Button("Close")
        set_primary(close)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            metrics.space_6, metrics.space_6, metrics.space_6, metrics.space_4
        )
        layout.setSpacing(metrics.space_4)
        layout.addWidget(scroll, 1)
        layout.addLayout(buttons)

        # The least is what the content needs; it opens at the size of the list.
        keep_least_size(self)
        hint = content.sizeHint()
        self.resize(
            hint.width() + 2 * metrics.space_6,
            min(hint.height(), metrics.window_min_height - metrics.space_6)
            + close.height() + metrics.space_6 + 2 * metrics.space_4,
        )

    def rows(self) -> list[tuple[str, str]]:
        """Each shortcut shown, as (keys, description)."""
        return list(self._rows)
