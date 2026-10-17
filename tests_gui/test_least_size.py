# SPDX-License-Identifier: GPL-3.0-or-later
"""A window is never smaller than its design floor, nor than its content needs."""

from __future__ import annotations

import gc
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, QSize, QTimer
from PySide6.QtGui import QFontInfo, QGuiApplication
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from tests_gui.screens import every_screen
from ui.widgets.least_size import keep_least_size

_FLOOR_WIDTH = 200
_FLOOR_HEIGHT = 100


def test_the_floor_holds_while_the_content_is_smaller(qapp):
    dialog = QDialog()
    QVBoxLayout(dialog).addWidget(QLabel("Short"))
    keep_least_size(dialog, _FLOOR_WIDTH, _FLOOR_HEIGHT)
    dialog.show()
    qapp.processEvents()

    assert dialog.minimumWidth() == _FLOOR_WIDTH
    assert dialog.minimumHeight() == _FLOOR_HEIGHT


def test_the_least_size_grows_with_the_content(qapp):
    dialog = QDialog()
    layout = QVBoxLayout(dialog)
    keep_least_size(dialog, _FLOOR_WIDTH, _FLOOR_HEIGHT)
    dialog.show()
    qapp.processEvents()

    wide = QLabel("A line of text much wider than the floor of this small dialog, " * 3)
    layout.addWidget(wide)
    qapp.processEvents()

    assert dialog.minimumWidth() >= wide.minimumSizeHint().width()
    assert dialog.minimumWidth() > _FLOOR_WIDTH
    assert dialog.width() >= dialog.minimumWidth()


def test_the_least_size_follows_larger_text(qapp):
    dialog = QDialog()
    label = QLabel("Wide enough at a larger size to pass the floor")
    QVBoxLayout(dialog).addWidget(label)
    keep_least_size(dialog, _FLOOR_WIDTH, _FLOOR_HEIGHT)
    dialog.show()
    qapp.processEvents()
    before = dialog.minimumWidth()

    font = label.font()
    font.setPixelSize(QFontInfo(font).pixelSize() * 3)
    label.setFont(font)
    qapp.processEvents()

    assert dialog.minimumWidth() > before
    assert dialog.minimumWidth() >= dialog.minimumSizeHint().width()


def test_setting_the_floor_again_replaces_it(qapp):
    dialog = QDialog()
    QVBoxLayout(dialog).addWidget(QLabel("Short"))
    keep_least_size(dialog, _FLOOR_WIDTH, _FLOOR_HEIGHT)
    keep_least_size(dialog, _FLOOR_WIDTH * 2, _FLOOR_HEIGHT)
    dialog.show()
    qapp.processEvents()

    assert dialog.minimumWidth() == _FLOOR_WIDTH * 2


class _SmallScreen:
    """A screen whose free area is ``size``."""

    def __init__(self, size: QSize) -> None:
        self._size = size

    def availableGeometry(self) -> QRect:
        return QRect(QPoint(0, 0), self._size)


def test_the_least_size_is_never_more_than_the_screen_holds(qapp):
    dialog = QDialog()
    QVBoxLayout(dialog).addWidget(QLabel("Short"))
    screen = QSize(_FLOOR_WIDTH // 2, _FLOOR_HEIGHT // 2)
    dialog.screen = lambda: _SmallScreen(screen)
    keep_least_size(dialog, _FLOOR_WIDTH, _FLOOR_HEIGHT)

    assert dialog.minimumSize() == screen


def test_no_window_can_be_made_smaller_than_its_content(qapp, config, theme_manager):
    checked = 0
    for name, screen in every_screen(qapp, config, theme_manager):
        if not screen.isWindow():
            continue
        needed, least = screen.minimumSizeHint(), screen.minimumSize()
        assert least.width() >= needed.width(), f"{name}: {least.width()} < {needed.width()}"
        assert least.height() >= needed.height(), f"{name}: {least.height()} < {needed.height()}"
        checked += 1
    assert checked > 10


def test_the_test_display_is_named_without_a_drive():
    """Qt cuts its platform setting at each colon; a Windows drive would cut it."""
    from tests_gui.offscreen import offscreen_platform

    platform = offscreen_platform(Path(__file__).resolve().parent.parent)
    name, _, argument = platform.partition(":")
    assert name == "offscreen"
    assert ":" not in argument
    assert QGuiApplication.primaryScreen().availableGeometry().width() > 800



def _run_and_let_go(owner: QWidget) -> None:
    """Run a dialog over ``owner`` as WireTrace does, then let go of it."""
    dialog = QDialog(owner)
    QVBoxLayout(dialog).addWidget(QLabel("Short"))
    keep_least_size(dialog, _FLOOR_WIDTH, _FLOOR_HEIGHT)
    QTimer.singleShot(0, dialog.accept)
    dialog.exec()


def test_a_dialog_run_and_let_go_ends_without_an_error(qapp, capfd):
    """Its last events reach the keeper after Python has cleared what the
    Python object held; the keeper must not depend on it."""
    owner = QWidget()
    _run_and_let_go(owner)
    gc.collect()
    qapp.processEvents()

    assert "Error calling Python override" not in capfd.readouterr().err
    assert owner.findChildren(QDialog) == []
