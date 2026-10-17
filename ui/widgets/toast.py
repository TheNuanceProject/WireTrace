# SPDX-License-Identifier: GPL-3.0-or-later
"""Toasts: brief messages that something finished.

A toast appears at the bottom of its tab, needs no answer, and leaves
by itself. It can carry one button, such as Show for a file just
written. A problem that must be noticed is a banner, not a toast.

A toast is never taller than the room its tab has for it. A message
longer than that room ends with an ellipsis, and is shown whole as the
toast's tooltip.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import pairwise

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QFont, QFontMetrics, QTextLayout, QTextOption
from PySide6.QtWidgets import QFrame, QHBoxLayout, QWidget

from app.constants import TOAST_DURATION_MS, TOAST_WITH_ACTION_DURATION_MS
from ui.themes import tokens
from ui.widgets.button import Button
from ui.widgets.styling import LEVEL, plain_label, set_state

INFO = "info"
SUCCESS = "success"
WARNING = "warning"
ERROR = "error"
_LEVELS = (INFO, SUCCESS, WARNING, ERROR)


def fit_lines(text: str, font: QFont, width: int, lines: int) -> str:
    """``text`` wrapped at ``width`` pixels, kept to ``lines`` lines.

    Each line ends where it wraps. If more lines would be needed, the
    last one kept ends with an ellipsis.
    """
    layout = QTextLayout(text, font)
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
    layout.setTextOption(option)
    starts: list[int] = []
    layout.beginLayout()
    while (line := layout.createLine()).isValid():
        line.setLineWidth(width)
        starts.append(line.textStart())
    layout.endLayout()
    if len(starts) <= lines:
        return text
    kept = [text[start:end].rstrip() for start, end in pairwise(starts[:lines])]
    last = text[starts[lines - 1]:]
    kept.append(QFontMetrics(font).elidedText(last, Qt.TextElideMode.ElideRight, width))
    return "\n".join(kept)


class Toast(QFrame):
    """A message shown over the bottom of its parent for a few seconds."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        metrics = tokens.METRICS

        self._message = ""
        self._label = plain_label()
        self._label.setWordWrap(True)
        self._button = Button(compact=True)
        self._button.clicked.connect(self._on_action)
        self._action: Callable[[], None] | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(metrics.space_4, metrics.space_2, metrics.space_2, metrics.space_2)
        layout.setSpacing(metrics.space_3)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._button, 0, Qt.AlignmentFlag.AlignVCenter)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        parent.installEventFilter(self)
        self.hide()

    # ── Showing ──────────────────────────────────────────────────────────

    @property
    def message(self) -> str:
        """The whole message, even when the toast shows it shortened."""
        return self._message

    @property
    def action_label(self) -> str:
        return self._button.text() if self._action is not None else ""

    def show_toast(
        self,
        message: str,
        level: str = INFO,
        action_label: str = "",
        action: Callable[[], None] | None = None,
    ) -> None:
        """Show ``message``. A second toast replaces the first."""
        set_state(self, LEVEL, level if level in _LEVELS else INFO)
        self._message = message
        self._action = action if action_label else None
        self._button.setText(action_label)
        self._button.setVisible(self._action is not None)
        self._place()
        self.show()
        self.raise_()
        self._timer.start(
            TOAST_WITH_ACTION_DURATION_MS if self._action is not None else TOAST_DURATION_MS
        )

    def trigger_action(self) -> None:
        """Do what the toast's button does."""
        self._on_action()

    def _on_action(self) -> None:
        action, self._action = self._action, None
        self.hide()
        if action is not None:
            action()

    def _place(self) -> None:
        """Centre the toast above the bottom edge of its parent."""
        parent = self.parentWidget()
        metrics = tokens.METRICS
        margins = self.layout().contentsMargins()
        beside = 0
        if self._action is not None:
            beside = self._button.sizeHint().width() + self.layout().spacing()
        font_metrics = self._label.fontMetrics()
        wanted = (
            font_metrics.horizontalAdvance(self._message)
            + margins.left() + margins.right() + metrics.space_2 + beside
        )
        widest = min(metrics.toast_max_width, parent.width() - 2 * metrics.space_6)
        # A long message wraps inside the widest toast that fits.
        self.setFixedWidth(max(metrics.toast_min_width, min(wanted, widest)))

        # Above the status bar and the command row, so neither is covered,
        # and a gap from the top of the parent as from the rows below.
        rows_below = metrics.status_height + metrics.toolbar_height
        room = parent.height() - rows_below - 2 * metrics.space_3
        frame = self.height() - self.contentsRect().height()
        lines = (room - frame - margins.top() - margins.bottom()) // font_metrics.lineSpacing()
        text_width = (
            self.contentsRect().width() - margins.left() - margins.right() - beside
        )
        shown = fit_lines(self._message, self._label.font(), text_width, max(1, lines))
        self._label.setText(shown)
        self.setToolTip(self._message if shown != self._message else "")

        self.setFixedHeight(self.layout().totalHeightForWidth(self.width()))
        x = (parent.width() - self.width()) // 2
        y = parent.height() - self.height() - rows_below - metrics.space_3
        self.move(max(0, x), max(0, y))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.parentWidget() and event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        return False

    # ── One toast per parent ─────────────────────────────────────────────

    @staticmethod
    def of(parent: QWidget) -> Toast:
        """The toast of ``parent``, created on first use."""
        for child in parent.children():
            if isinstance(child, Toast):
                return child
        return Toast(parent)

    @staticmethod
    def info(
        parent: QWidget, message: str,
        action_label: str = "", action: Callable[[], None] | None = None,
    ) -> Toast:
        return Toast._show(parent, message, INFO, action_label, action)

    @staticmethod
    def success(
        parent: QWidget, message: str,
        action_label: str = "", action: Callable[[], None] | None = None,
    ) -> Toast:
        return Toast._show(parent, message, SUCCESS, action_label, action)

    @staticmethod
    def warning(
        parent: QWidget, message: str,
        action_label: str = "", action: Callable[[], None] | None = None,
    ) -> Toast:
        return Toast._show(parent, message, WARNING, action_label, action)

    @staticmethod
    def error(
        parent: QWidget, message: str,
        action_label: str = "", action: Callable[[], None] | None = None,
    ) -> Toast:
        return Toast._show(parent, message, ERROR, action_label, action)

    @staticmethod
    def _show(
        parent: QWidget, message: str, level: str,
        action_label: str, action: Callable[[], None] | None,
    ) -> Toast:
        toast = Toast.of(parent)
        toast.show_toast(message, level, action_label, action)
        return toast
