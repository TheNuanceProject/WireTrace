# SPDX-License-Identifier: GPL-3.0-or-later
"""The user guide names things as the interface names them.

The guide takes its shortcuts, limits and colours from the program. The
names of buttons, settings and menus are written in its text, so these
tests fail when one of them is renamed and the guide is not.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

import pytest
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMenu

import app.help_loader as help_loader
from tests_gui.screens import lose_connection, main_window, start_session
from ui.dialogs.preferences_dialog import PAGES, PreferencesDialog
from ui.widgets.banner import Banner
from ui.widgets.button import Button
from ui.widgets.command_bar import CommandBar
from ui.widgets.plot_view import PlotView
from ui.widgets.tab_toolbar import TabToolbar


@pytest.fixture(scope="module")
def guide() -> str:
    """The text of the filled guide, without its markup."""
    page = re.sub(r"data:image/png;base64,[A-Za-z0-9+/=]+", "", help_loader.load_user_guide())
    return html.unescape(re.sub(r"<[^>]+>", "", page))


def _button_texts(parent) -> set[str]:
    return {button.text() for button in parent.findChildren(Button) if button.text()}


def test_guide_names_every_page_and_setting_of_preferences(qapp, config, theme_manager, guide):
    dialog = PreferencesDialog(config, theme_manager, open_plot_config=lambda: None)
    try:
        for name in (*PAGES, *dialog.setting_names()):
            assert name in guide, name
    finally:
        dialog.close()
        dialog.deleteLater()


def test_guide_names_every_button_of_a_session(qapp, config, theme_manager, guide):
    with main_window(qapp, config, theme_manager) as (window, port):
        start_session(qapp, window, port)
        tab = window.current_tab()
        toolbar = tab.findChild(TabToolbar)
        names = (
            _button_texts(toolbar)
            | _button_texts(tab.findChild(CommandBar))
            | _button_texts(tab.findChild(PlotView))
        )
        # The other state of each button that changes its name.
        toolbar.set_logging(True, paused=True)
        names |= _button_texts(toolbar)
        lose_connection(qapp, window, port)
        names |= _button_texts(toolbar) | _button_texts(tab.findChild(Banner))

    assert {"Connect", "Disconnect", "Start log", "Stop log", "Pause", "Resume"} <= names
    for name in sorted(names):
        assert name in guide, name


def test_guide_names_every_menu(qapp, config, theme_manager, guide):
    with main_window(qapp, config, theme_manager) as (window, _port):
        titles = [
            menu.title().replace("&", "")
            for menu in window.menuBar().findChildren(QMenu)
            if menu.parent() is window.menuBar()
        ]

    assert len(titles) >= 5
    for title in titles:
        assert re.search(rf"\b{re.escape(title)}\b", guide), title


def test_f1_opens_the_filled_guide_from_the_cache(
    qapp, config, theme_manager, tmp_path, monkeypatch,
):
    opened: list[QUrl] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    monkeypatch.setattr(help_loader, "get_cache_dir", lambda: str(tmp_path))

    with main_window(qapp, config, theme_manager) as (window, _port):
        window.shortcut_actions()["user_guide"].trigger()

    assert len(opened) == 1
    path = Path(opened[0].toLocalFile())
    assert path.is_relative_to(tmp_path)
    with open(path, encoding="utf-8") as page:
        assert "{{" not in page.read()
