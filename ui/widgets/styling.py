# SPDX-License-Identifier: GPL-3.0-or-later
"""Style states a widget can be put in.

The stylesheet draws a widget differently according to a few named
properties. They are set through this module so that each name is
written in one place, and so the widget is redrawn when a state changes
after it has been shown.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QWidget

from ui.themes import tokens

#: A button that is the main action of its screen or dialog.
PRIMARY = "primary"

#: A text field whose content is not acceptable.
INVALID = "invalid"

#: Text that is less important than its surroundings.
SECONDARY = "secondary"

#: The kind of a message or of a console level button.
LEVEL = "level"

#: Position of a button in a row of joined buttons: "left" or "right".
SEGMENT = "segment"

#: Text set one step smaller: hints, captions, the copyright line.
SMALL = "small"

#: A row that is the chosen one in a list.
SELECTED = "selected"

#: A button or a field of the smaller of the two sizes.
COMPACT = "compact"


def set_state(widget: QWidget, name: str, value: bool | str) -> None:
    """Set a style state and redraw the widget if the state changed."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def emphasis(markup: str) -> str:
    """Rich text that stands out by its weight: the medium weight of the
    design language. Bold is not used: the interface font ships without it."""
    return f'<span style="font-weight: {tokens.TYPOGRAPHY.medium};">{markup}</span>'


def set_primary(widget: QWidget, primary: bool = True) -> None:
    """Mark a button as the main action, or as an ordinary one."""
    set_state(widget, PRIMARY, primary)


def set_compact(widget: QWidget) -> None:
    """Give a field the smaller of the two sizes, for rows inside the working area."""
    set_state(widget, COMPACT, True)


def plain_label(text: str = "") -> QLabel:
    """A label that shows its text exactly as it is.

    Qt reads a label's text as markup when it looks like markup. Text
    that names a port, a file, a value a device sent or an answer from a
    server must never be read that way, so every label of the interface
    is made here unless it is written to show markup of its own.
    """
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def plain_tooltip(text: str) -> str:
    """``text`` as a tooltip that shows it exactly as it is.

    A tooltip has no plain mode, so the text is escaped and marked as
    markup, which Qt then shows unchanged.
    """
    return "<qt>" + html.escape(text).replace("\n", "<br>") + "</qt>"
