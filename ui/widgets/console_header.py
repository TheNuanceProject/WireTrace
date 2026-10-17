# SPDX-License-Identifier: GPL-3.0-or-later
"""The row above the console: level buttons, text filter, time mode, following.

Each level button shows how many held lines have that level. Choosing
one shows only those lines. When the row is too narrow for the buttons
they give way to a single Levels drop-down.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Signal
from PySide6.QtGui import QAction, QActionGroup, QResizeEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLayout,
    QLineEdit,
    QMenu,
    QWidget,
)

from app.constants import TAG_COMMAND, TAG_DATA, TimestampMode
from ui import shortcuts
from ui.themes import tokens
from ui.widgets.button import Button
from ui.widgets.styling import LEVEL, set_compact, set_state


@dataclass(frozen=True)
class LevelChoice:
    """One level button: its name and the tags it stands for."""

    key: str
    label: str
    tags: frozenset[str] | None     # None means every line


LEVEL_CHOICES: tuple[LevelChoice, ...] = (
    LevelChoice("all", "All", None),
    LevelChoice("error", "Error", frozenset({"ERROR", "CRITICAL"})),
    LevelChoice("warning", "Warning", frozenset({"WARNING"})),
    LevelChoice("info", "Info", frozenset({"INFO"})),
    LevelChoice("debug", "Debug", frozenset({"DEBUG"})),
    LevelChoice("data", "Data", frozenset({TAG_DATA, TAG_COMMAND})),
)

_TIME_MODE_LABELS: dict[TimestampMode, str] = {
    TimestampMode.ABSOLUTE: "Time of day",
    TimestampMode.RELATIVE: "Since previous line",
}


class ConsoleHeader(QFrame):
    """Controls for what the console shows.

    Signals:
        levels_changed(object):         Tags to show, or None for all.
        filter_changed(str):            Text of the filter box.
        timestamp_mode_changed(object): The ``TimestampMode`` chosen.
        follow_requested(bool):         Follow the newest line, or stop.
    """

    levels_changed = Signal(object)
    filter_changed = Signal(str)
    timestamp_mode_changed = Signal(object)
    follow_requested = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("consoleHeader")
        metrics = tokens.METRICS
        self.setFixedHeight(metrics.header_height)

        self._choice = LEVEL_CHOICES[0]
        self._counts: dict[str, int] = {}
        self._timestamp_mode = TimestampMode.ABSOLUTE

        # Level buttons, and the drop-down that replaces them when narrow.
        self._level_group = QButtonGroup(self)
        self._level_group.setExclusive(True)
        self._level_buttons: dict[str, Button] = {}
        self._level_actions: dict[str, QAction] = {}
        self._levels_menu = QMenu(self)
        action_group = QActionGroup(self)
        action_group.setExclusive(True)
        for choice in LEVEL_CHOICES:
            button = Button(compact=True)
            button.setCheckable(True)
            button.setChecked(choice is self._choice)
            set_state(button, LEVEL, choice.key)
            button.clicked.connect(lambda _checked, c=choice: self._choose(c))
            self._level_group.addButton(button)
            self._level_buttons[choice.key] = button

            action = self._levels_menu.addAction("")
            action.setCheckable(True)
            action.setChecked(choice is self._choice)
            action.triggered.connect(lambda _checked, c=choice: self._choose(c))
            action_group.addAction(action)
            self._level_actions[choice.key] = action

        self._levels_dropdown = Button(compact=True)
        self._levels_dropdown.setMenu(self._levels_menu)
        self._levels_dropdown.hide()

        self._filter = QLineEdit()
        self._filter.setPlaceholderText("Filter lines")
        self._filter.setClearButtonEnabled(True)
        set_compact(self._filter)
        self._filter.setFixedWidth(metrics.filter_field_width)
        self._filter.setToolTip(shortcuts.with_keys("Show only lines that contain this text", "filter"))
        self._filter.setAccessibleName("Filter lines")
        self._filter.textChanged.connect(self.filter_changed)

        self._time_mode = Button(compact=True)
        self._time_mode.setToolTip("Switch between the time of day and the time since the previous line")
        self._time_mode.clicked.connect(self._toggle_timestamp_mode)

        self._follow = Button(compact=True)
        self._follow.setCheckable(True)
        self._follow.setToolTip("Stay on the newest line as lines arrive")
        self._follow.clicked.connect(self.follow_requested)

        layout = QHBoxLayout(self)
        # The row decides for itself what to show at the width it is
        # given, so its contents must not set a least width for the window.
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.setContentsMargins(metrics.space_3, 0, metrics.space_3, 0)
        layout.setSpacing(metrics.space_2)
        for button in self._level_buttons.values():
            layout.addWidget(button)
        layout.addWidget(self._levels_dropdown)
        layout.addStretch(1)
        layout.addWidget(self._filter)
        layout.addWidget(self._time_mode)
        layout.addWidget(self._follow)

        self.set_level_counts({})
        self.set_timestamp_mode(self._timestamp_mode)
        self.set_following(True)

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def level(self) -> LevelChoice:
        return self._choice

    @property
    def filter_text(self) -> str:
        return self._filter.text()

    @property
    def levels_collapsed(self) -> bool:
        """True while the Levels drop-down stands in for the buttons."""
        return self._levels_dropdown.isVisibleTo(self)

    def set_level_counts(self, counts: dict[str, int]) -> None:
        """Update the number shown on each level button."""
        self._counts = dict(counts)
        for choice in LEVEL_CHOICES:
            text = f"{choice.label}  {self._count_for(choice):,}"
            self._level_buttons[choice.key].setText(text)
            self._level_actions[choice.key].setText(text)
        self._levels_dropdown.setText(
            f"{self._choice.label}  {self._count_for(self._choice):,}"
        )
        self._fit_levels()

    def set_timestamp_mode(self, mode: TimestampMode) -> None:
        self._timestamp_mode = mode
        self._time_mode.setText(_TIME_MODE_LABELS[mode])

    def set_following(self, following: bool) -> None:
        self._follow.setChecked(following)
        self._follow.setText("Following" if following else "Not following")

    def focus_filter(self) -> None:
        self._filter.setFocus()
        self._filter.selectAll()

    def clear_filter(self) -> None:
        self._filter.clear()

    def set_filter_text(self, text: str) -> None:
        self._filter.setText(text)

    def level_texts(self) -> list[str]:
        return [button.text() for button in self._level_buttons.values()]

    # ── Internal ─────────────────────────────────────────────────────────

    def _count_for(self, choice: LevelChoice) -> int:
        if choice.tags is None:
            return sum(self._counts.values())
        return sum(self._counts.get(tag, 0) for tag in choice.tags)

    def _choose(self, choice: LevelChoice) -> None:
        changed = choice is not self._choice
        self._choice = choice
        self._level_buttons[choice.key].setChecked(True)
        self._level_actions[choice.key].setChecked(True)
        self._levels_dropdown.setText(f"{choice.label}  {self._count_for(choice):,}")
        if changed:
            self.levels_changed.emit(choice.tags)

    def _toggle_timestamp_mode(self) -> None:
        mode = (
            TimestampMode.RELATIVE
            if self._timestamp_mode == TimestampMode.ABSOLUTE
            else TimestampMode.ABSOLUTE
        )
        self.set_timestamp_mode(mode)
        self.timestamp_mode_changed.emit(mode)

    def _fit_levels(self) -> None:
        """Fit the row to its width.

        The filter field gives up width first, down to its least. If the level
        buttons still do not fit, one drop-down takes their place.
        """
        layout = self.layout()
        margins = layout.contentsMargins()
        widest = tokens.METRICS.filter_field_width
        narrowest = tokens.METRICS.filter_field_min_width
        levels = list(self._level_buttons.values())

        def spare(buttons: list[Button]) -> int:
            """Width left for the filter field beside ``buttons``."""
            return (
                self.width() - margins.left() - margins.right()
                - sum(button.sizeHint().width() for button in buttons)
                - layout.spacing() * (len(buttons) + 1)
            )

        others = [self._time_mode, self._follow]
        collapse = spare(levels + others) < narrowest
        for button in levels:
            button.setVisible(not collapse)
        self._levels_dropdown.setVisible(collapse)
        shown = [self._levels_dropdown] if collapse else levels
        self._filter.setFixedWidth(max(narrowest, min(widest, spare(shown + others))))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, tokens.METRICS.header_height)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_levels()
