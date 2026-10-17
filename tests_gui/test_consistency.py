# SPDX-License-Identifier: GPL-3.0-or-later
"""Rules every screen follows, checked on the real widgets.

The same kind of thing has the same size, the same spacing and the same
style of wording wherever it appears. Each test walks every screen of
the application and fails when one of them departs from the rule.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMessageBox,
    QRadioButton,
    QStackedLayout,
    QWidget,
)

from tests_gui.screens import every_screen
from ui.themes import tokens
from ui.themes.theme_manager import font_sizes, font_weights
from ui.widgets.styling import COMPACT
from version import APP_NAME

_HEADINGS = ("sectionTitle", "pageTitle", "displayTitle")

#: Words that keep their capital inside a sentence: the application and
#: the feature the menus call Find.
_PROPER = frozenset({APP_NAME, "Find"})

_ELLIPSIS_CHARACTER = chr(0x2026)


def _own_layouts(root: QWidget) -> Iterator[QLayout]:
    """Layouts the application made, leaving out those inside toolkit widgets.

    The whole list stays referenced while it is walked: letting go of an
    outer layout would invalidate the references to the layouts inside it.
    """
    everything = root.findChildren(QLayout)
    for layout in everything:
        owner = layout.parentWidget()
        if owner is None or type(layout) is QLayout or isinstance(layout, QStackedLayout):
            continue
        if owner.objectName().startswith("qt_") or isinstance(owner, (QDialogButtonBox, QMessageBox)):
            continue
        if type(owner).__module__.startswith("pyqtgraph"):
            continue
        yield layout


def test_every_margin_and_gap_is_a_step_of_the_spacing_scale(qapp, config, theme_manager):
    """Nothing is left to the platform's default, which differs between systems."""
    metrics = tokens.METRICS
    scale = {
        0, metrics.space_half, metrics.space_1, metrics.space_2,
        metrics.space_3, metrics.space_4, metrics.space_6,
    }
    checked = 0
    for name, screen in every_screen(qapp, config, theme_manager):
        for layout in _own_layouts(screen):
            margins = layout.contentsMargins()
            values = [margins.left(), margins.top(), margins.right(), margins.bottom()]
            if isinstance(layout, QGridLayout):
                values += [layout.horizontalSpacing(), layout.verticalSpacing()]
            else:
                values.append(layout.spacing())
            owner = layout.parentWidget()
            where = f"{name}: {owner.objectName() or type(owner).__name__}"
            assert set(values) <= scale, f"{where} {values}"
            checked += 1
    assert checked > 100


def test_dialogs_share_one_outer_margin(qapp, config, theme_manager):
    metrics = tokens.METRICS
    usual = (metrics.space_6, metrics.space_6, metrics.space_6, metrics.space_4)
    flush = (0, 0, 0, 0)  # a dialog with a sidebar or a footer of its own
    for name, screen in every_screen(qapp, config, theme_manager):
        if not isinstance(screen, QDialog) or isinstance(screen, QMessageBox):
            continue
        margins = screen.layout().contentsMargins()
        found = (margins.left(), margins.top(), margins.right(), margins.bottom())
        assert found in (usual, flush), f"{name} {found}"


def test_one_line_fields_have_one_height_per_size(qapp, config, theme_manager):
    metrics = tokens.METRICS
    checked = 0
    for name, screen in every_screen(qapp, config, theme_manager):
        for field in screen.findChildren(QWidget):
            if not isinstance(field, (QLineEdit, QComboBox, QAbstractSpinBox)):
                continue
            if isinstance(field.parentWidget(), (QComboBox, QAbstractSpinBox)):
                continue
            if not field.isVisibleTo(screen):
                continue
            compact = bool(field.property(COMPACT))
            height = metrics.control_height_compact if compact else metrics.control_height
            label = field.accessibleName() or field.objectName() or type(field).__name__
            assert field.height() == height, f"{name}: {label} is {field.height()} high"
            checked += 1
    assert checked > 20


def test_interface_text_is_the_bundled_font_at_the_sizes_of_the_scale(
    qapp, config, theme_manager,
):
    """Every piece of interface text is Poppins, at a size and weight of
    the type scale: nothing falls back to a font of the system."""
    sizes = {int(size) for size in font_sizes().values()}
    weights = {int(weight) for weight in font_weights().values()}
    typography = tokens.TYPOGRAPHY
    checked = 0
    for name, screen in every_screen(qapp, config, theme_manager):
        for widget in screen.findChildren(QWidget):
            if not isinstance(widget, (QLabel, QAbstractButton, QLineEdit, QComboBox)):
                continue
            if not widget.isVisibleTo(screen) or widget.font().family() in typography.console_families:
                continue
            info = widget.fontInfo()
            label = widget.objectName() or type(widget).__name__
            assert info.family() == typography.interface_family, f"{name}: {label} {info.family()}"
            assert info.pixelSize() in sizes, f"{name}: {label} is {info.pixelSize()} px"
            assert widget.font().weight() in weights, f"{name}: {label} weight {widget.font().weight()}"
            checked += 1
    assert checked > 200


def _labels(screen: QWidget) -> list[tuple[str, str]]:
    """``(kind, text)`` of everything that names a control or a section."""
    found = [("title", screen.windowTitle())] if screen.isWindow() else []
    for widget in screen.findChildren(QWidget):
        if isinstance(widget, (QCheckBox, QRadioButton)):
            found.append(("choice", widget.text()))
        elif isinstance(widget, QAbstractButton):
            found.append(("button", widget.text()))
        elif isinstance(widget, QLabel) and widget.objectName() in _HEADINGS:
            found.append(("heading", widget.text()))
        elif isinstance(widget, QLabel) and widget.buddy() is not None:
            found.append(("label", widget.text()))
        elif isinstance(widget, QLineEdit) and not widget.isReadOnly():
            found.append(("placeholder", widget.placeholderText()))
    for action in screen.findChildren(QAction):
        found.append(("menu", action.text().split("\\t")[0]))
    return [(kind, text.replace("&&", "and").replace("&", "")) for kind, text in found if text]


def _capitals_inside(text: str) -> list[str]:
    """Words after the first that are capitalised without being a name."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9+-]*", text)
    return [
        word for word in words[1:]
        if word[0].isupper() and word not in _PROPER and not word.isupper()
    ]


def test_labels_are_written_one_way(qapp, config, theme_manager):
    """Sentence case, no colon at the end, three dots for "opens a dialog"."""
    seen: set[tuple[str, str]] = set()
    problems: list[str] = []
    for name, screen in every_screen(qapp, config, theme_manager):
        for kind, text in _labels(screen):
            if (kind, text) in seen:
                continue
            seen.add((kind, text))
            if text.rstrip().endswith(":"):
                problems.append(f"{name}: {kind} {text!r} ends with a colon")
            if _ELLIPSIS_CHARACTER in text:
                problems.append(f"{name}: {kind} {text!r} uses the single ellipsis character")
            if kind != "placeholder" and _capitals_inside(text):
                problems.append(f"{name}: {kind} {text!r} is not in sentence case")
    assert len(seen) > 150
    assert problems == []


def test_no_label_decides_by_itself_whether_its_text_is_markup(qapp, config, theme_manager):
    """A label left to guess would read text from a device, a file name
    or a server as markup whenever it looked like markup."""
    checked = 0
    for name, screen in every_screen(qapp, config, theme_manager):
        for label in screen.findChildren(QLabel):
            if not label.text():
                continue
            assert label.textFormat() is not Qt.TextFormat.AutoText, f"{name}: {label.text()!r}"
            checked += 1
    assert checked > 100
