# SPDX-License-Identifier: GPL-3.0-or-later
"""The list of the tour's chapters, opened from Help > Take the tour.

Each chapter can be taken on its own, or the whole tour from the first;
the chapters already taken say so.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from ui.dialogs.common import action_button, button_row, dialog_layout, heading, note, tighten
from ui.themes import tokens
from ui.tour.chapters import Chapter
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size
from ui.widgets.vertical_scroll import VerticalScrollArea
from version import APP_NAME


def chapter_hint(chapter: Chapter, taken: bool) -> str:
    """What a chapter's row says under its title."""
    steps = len(chapter.steps)
    count = f"{steps} step" if steps == 1 else f"{steps} steps"
    return f"{chapter.summary} {count}{', taken' if taken else ''}."


class TourDialog(DotGridDialog):
    """The chapters of the tour, to start one or all.

    Signals:
        chosen(object):  The chapters to take, in order (a tuple).
    """

    chosen = Signal(object)

    def __init__(
        self,
        chapters: Sequence[Chapter],
        whole: Sequence[Chapter],
        taken: Sequence[str],
        parent: QWidget | None = None,
    ) -> None:
        """``chapters`` are listed; ``whole`` is the whole tour."""
        super().__init__(parent)
        self._chapters = tuple(chapters)
        self._whole = tuple(whole)
        self.setWindowTitle("Take the tour")
        keep_least_size(self, tokens.METRICS.dialog_width)
        self.setModal(True)
        layout = dialog_layout(self)
        layout.addWidget(heading("Take the tour", "pageTitle"))
        layout.addWidget(note(
            f"Each chapter points at {APP_NAME}'s controls one at a time. A sample board built "
            "in shows every tool, so no board is needed. Take any chapter on its own, or the "
            "whole tour.",
        ))
        # The chapters scroll, so that the list fits a small screen.
        rows = QWidget()
        rows_layout = QVBoxLayout(rows)
        tighten(rows_layout)
        # Room between the buttons and the scroll bar, when there is one.
        rows_layout.setContentsMargins(0, 0, tokens.METRICS.space_3, 0)
        rows_layout.setSpacing(tokens.METRICS.space_4)
        for chapter in self._chapters:
            rows_layout.addLayout(self._row(chapter, chapter.id in taken))
        rows_layout.addStretch(1)
        self._list = VerticalScrollArea(rows)
        layout.addWidget(self._list, 1)
        close = action_button("Close")
        close.clicked.connect(self.reject)
        whole = action_button("Take the whole tour", primary=True)
        whole.setDefault(True)
        whole.clicked.connect(lambda: self._choose(self._whole))
        layout.addLayout(button_row(close, whole))

    def _row(self, chapter: Chapter, taken: bool) -> QHBoxLayout:
        """A chapter: its title and what it holds, and a button to start it."""
        metrics = tokens.METRICS
        words = QVBoxLayout()
        words.setSpacing(metrics.space_half)
        words.addWidget(heading(chapter.title))
        words.addWidget(note(chapter_hint(chapter, taken), small=True))
        start = action_button("Start")
        start.setAccessibleName(f"Start {chapter.title}")
        start.clicked.connect(lambda _checked=False: self._choose((chapter,)))
        row = QHBoxLayout()
        row.setSpacing(metrics.space_4)
        row.addLayout(words, 1)
        row.addWidget(start, 0, Qt.AlignmentFlag.AlignTop)
        return row

    def _choose(self, chapters: tuple[Chapter, ...]) -> None:
        self.accept()
        self.chosen.emit(chapters)
