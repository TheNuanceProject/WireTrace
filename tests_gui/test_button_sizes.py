# SPDX-License-Identifier: GPL-3.0-or-later
"""One rule for the size of every button, checked on the real widgets.

A button is regular or compact. Each size has one height and one least
width. A button that carries only an icon is a square of its size's
height. Joined buttons share one width. These tests fail when any button
in the window or in a dialog departs from that.
"""

from __future__ import annotations

from PySide6.QtGui import QColor
from PySide6.QtWidgets import QCheckBox, QLineEdit, QPushButton, QWidget

from app.constants import ThemeID
from tests_gui.screens import every_screen
from ui.themes import tokens
from ui.widgets.button import ICON_ONLY_NAMES, Button, join_buttons
from ui.widgets.styling import COMPACT, LEVEL, SEGMENT, set_state


def _departures(root: QWidget) -> list[str]:
    """Every visible button under ``root`` that breaks the rule, described."""
    metrics = tokens.METRICS
    found: list[str] = []
    for button in root.findChildren(QPushButton):
        if not button.isVisibleTo(root):
            continue
        compact = bool(button.property(COMPACT))
        height = metrics.control_height_compact if compact else metrics.control_height
        least = metrics.button_min_width_compact if compact else metrics.button_min_width
        label = f"{button.text() or button.accessibleName()!r} {button.width()}x{button.height()}"
        if not isinstance(button, Button):
            found.append(f"{label}: not made from the shared Button")
        if button.height() != height:
            found.append(f"{label}: height is not {height}")
        if button.objectName() in ICON_ONLY_NAMES:
            if button.width() != height:
                found.append(f"{label}: an icon button is not square")
        elif button.property(SEGMENT):
            widths = {
                other.width()
                for other in button.parentWidget().findChildren(QPushButton)
                if other.property(SEGMENT)
            }
            if len(widths) != 1:
                found.append(f"{label}: joined buttons differ in width")
        else:
            if button.width() < least:
                found.append(f"{label}: narrower than {least}")
            if button.width() < button.sizeHint().width():
                found.append(f"{label}: narrower than its text")
    return found


def _count(root: QWidget) -> int:
    return sum(1 for button in root.findChildren(QPushButton) if button.isVisibleTo(root))


def test_every_button_follows_the_rule(qapp, config, theme_manager):
    counted = 0
    for name, screen in every_screen(qapp, config, theme_manager):
        counted += _count(screen)
        assert _departures(screen) == [], name
    assert counted > 100


def test_a_button_grows_with_its_text_and_never_goes_below_the_least_width(
    qapp, theme_manager,
):
    theme_manager.apply_theme(ThemeID.LIGHT)
    host = QWidget()
    regular = Button("Ok", host)
    compact = Button("Ok", host, compact=True)
    host.show()
    qapp.processEvents()
    try:
        metrics = tokens.METRICS
        assert regular.sizeHint().width() == metrics.button_min_width
        assert regular.sizeHint().height() == metrics.control_height
        assert compact.sizeHint().width() == metrics.button_min_width_compact
        assert compact.sizeHint().height() == metrics.control_height_compact

        regular.setText("A label much longer than the least width of a button")
        assert regular.sizeHint().width() > metrics.button_min_width
        assert regular.sizeHint().height() == metrics.control_height
        assert regular.minimumSizeHint() == regular.sizeHint()
    finally:
        host.close()
        host.deleteLater()


def test_joined_buttons_are_each_as_wide_as_the_widest(qapp, theme_manager):
    theme_manager.apply_theme(ThemeID.LIGHT)
    short, long = Button("A"), Button("A much longer label")
    joined = join_buttons(short, long)
    joined.show()
    # A window of its own is first limited to a share of the screen.
    joined.resize(joined.sizeHint())
    qapp.processEvents()
    try:
        assert short.width() == long.width()
        assert short.width() >= long.sizeHint().width()
        assert (short.property(SEGMENT), long.property(SEGMENT)) == ("left", "right")
    finally:
        joined.close()
        joined.deleteLater()



def test_the_focus_ring_moves_nothing(qapp, theme_manager):
    """A focused control's outline is two pixels instead of one; the box
    keeps its size, so nothing beside it moves when the focus arrives."""
    theme_manager.apply_theme(ThemeID.LIGHT)
    host = QWidget()
    controls = [
        Button("Connect", host),
        Button("Clear", host, compact=True),
        QLineEdit(host),
    ]
    icon_only = Button("", host)
    icon_only.setObjectName("iconButton")
    controls.append(icon_only)
    box = QCheckBox("Option", host)
    box.setChecked(True)
    controls.append(box)
    host.show()
    qapp.processEvents()
    try:
        for control in controls:
            host.setFocus()
            qapp.processEvents()
            before = control.sizeHint(), control.minimumSize(), control.maximumSize()
            control.setFocus()
            qapp.processEvents()
            assert control.hasFocus()
            after = control.sizeHint(), control.minimumSize(), control.maximumSize()
            assert after == before, type(control).__name__
    finally:
        host.close()


def _ring_pixels(widget: QWidget, colour: str) -> int:
    image = widget.grab().toImage()
    ring = QColor(colour)
    return sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y) == ring
    )


def test_every_kind_of_control_shows_the_focus_ring(qapp, theme_manager):
    """Rules that style a kind of button (a level, a joined segment) must
    not hide the ring that shows where the keyboard is."""
    theme_manager.apply_theme(ThemeID.LIGHT)
    host = QWidget()
    level = Button("Error", host, compact=True)
    set_state(level, LEVEL, "error")
    level.setCheckable(True)
    level.setChecked(True)
    left = Button("Text", host)
    right = Button("HEX", host)
    joined = join_buttons(left, right)
    joined.setParent(host)
    left.setCheckable(True)
    left.setChecked(True)
    box = QCheckBox("Option", host)
    box.setChecked(True)
    host.show()
    qapp.processEvents()
    ring = tokens.palette().accent_strong
    try:
        for control in (level, left, right, box):
            control.setFocus()
            qapp.processEvents()
            assert control.hasFocus()
            assert _ring_pixels(control, ring) > 0, control.text()
        # The ring of a check box is its indicator's, not the whole control's.
        image = box.grab().toImage()
        indicator = tokens.METRICS.indicator + 2 * tokens.METRICS.mark
        # The image is in device pixels; the sizes are in logical ones.
        beside = round((indicator + tokens.METRICS.space_2) * image.devicePixelRatio())
        assert all(
            image.pixelColor(x, y) != QColor(ring)
            for x in range(beside, image.width())
            for y in range(image.height())
        )
    finally:
        host.close()
