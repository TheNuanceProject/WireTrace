# SPDX-License-Identifier: GPL-3.0-or-later
"""The theme, drawn by real Qt: stylesheet, system setting, font, dot grid."""

from __future__ import annotations

import sys

import pytest
from PySide6.QtCore import Qt, QtMsgType, qInstallMessageHandler
from PySide6.QtGui import QColor, QFont, QFontInfo, QPalette, QTextLayout
from PySide6.QtWidgets import QApplication, QCheckBox, QLineEdit, QPushButton, QWidget

import ui.themes.theme_manager as theme_manager_module
from app.config import ConfigManager
from app.constants import THEME_FALLBACK, THEME_FOLLOW_SYSTEM, ThemeID
from tests_gui.screens import every_screen
from tests_gui.support import pixel
from ui.device_tab import DeviceTab
from ui.dialogs.preferences_dialog import PreferencesDialog
from ui.main_window import MainWindow
from ui.themes import icons, tokens
from ui.themes.fonts import interface_font, monospace_font, register_bundled_fonts
from ui.themes.theme_manager import ThemeManager
from ui.themes.window_frames import redraw_frames
from ui.widgets.console_view import ConsoleView
from ui.widgets.dot_grid import DotGridWidget
from ui.widgets.styling import INVALID, PRIMARY, set_primary, set_state
from ui.widgets.toast import Toast


@pytest.fixture
def qt_warnings() -> list[str]:
    """Collect every warning Qt prints while the test runs."""
    messages: list[str] = []

    def handler(kind: QtMsgType, _context: object, message: str) -> None:
        if kind != QtMsgType.QtDebugMsg:
            messages.append(message)

    previous = qInstallMessageHandler(handler)
    yield messages
    qInstallMessageHandler(previous)


@pytest.mark.parametrize("theme_id", list(ThemeID))
def test_theme_applies_without_a_stylesheet_error(
    qapp: QApplication,
    theme_manager: ThemeManager,
    qt_warnings: list[str],
    theme_id: ThemeID,
) -> None:
    assert theme_manager.apply_theme(theme_id) is True
    button = QPushButton("Probe")
    button.show()
    qapp.processEvents()
    button.close()

    assert not [m for m in qt_warnings if "stylesheet" in m.lower()], qt_warnings
    assert theme_manager.current_theme is theme_id
    assert theme_manager.preference == theme_id.value
    assert tokens.active_theme() is theme_id

    palette = tokens.PALETTES[theme_id]
    assert qapp.palette().color(QPalette.ColorRole.Window) == QColor(palette.paper)
    assert qapp.palette().color(QPalette.ColorRole.WindowText) == QColor(palette.text)
    assert palette.paper in qapp.styleSheet()


@pytest.mark.parametrize(
    "folder",
    ["First Last", "name (work)", "caf\u00e9 \u0645\u062d\u0645\u062f", "it's", "a&b #1 100%"],
)
def test_icons_load_from_a_folder_with_an_awkward_name(
    qapp: QApplication,
    theme_manager: ThemeManager,
    qt_warnings: list[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    folder: str,
) -> None:
    """A user name with a space or a bracket must not break the stylesheet."""
    monkeypatch.setattr(icons, "get_cache_dir", lambda: str(tmp_path / folder))
    assert theme_manager.apply_theme(ThemeID.LIGHT) is True
    palette = tokens.palette()

    box = QCheckBox()
    box.setChecked(True)
    box.resize(24, 24)
    box.show()
    qapp.processEvents()
    image = box.grab().toImage()
    box.close()

    ink = QColor(palette.text)
    filled = [
        (x, y)
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y) == ink
    ]
    assert not [m for m in qt_warnings if "stylesheet" in m.lower()], qt_warnings
    assert filled, "the checked box is not filled"

    # The check mark is drawn in the paper colour on the filled box. If
    # the icon file failed to load, the box is filled and nothing more.
    columns = [x for x, _ in filled]
    rows = [y for _, y in filled]
    inside = [
        image.pixelColor(x, y)
        for x in range(min(columns), max(columns) + 1)
        for y in range(min(rows), max(rows) + 1)
    ]
    assert QColor(palette.paper) in inside, "the check mark was not drawn"


def test_theme_change_is_announced_once(theme_manager: ThemeManager) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)
    announced: list[str] = []
    theme_manager.theme_changed.connect(announced.append)
    theme_manager.apply_theme(ThemeID.DARK)
    assert announced == [ThemeID.DARK.value]


@pytest.mark.parametrize("system", list(ThemeID))
def test_system_preference_follows_the_system(
    theme_manager: ThemeManager, monkeypatch: pytest.MonkeyPatch, system: ThemeID
) -> None:
    monkeypatch.setattr(theme_manager_module, "system_theme", lambda: system)
    assert theme_manager.apply_preference(THEME_FOLLOW_SYSTEM) is True
    assert theme_manager.preference == THEME_FOLLOW_SYSTEM
    assert theme_manager.current_theme is system


def test_system_change_switches_the_theme_only_when_following(
    theme_manager: ThemeManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(theme_manager_module, "system_theme", lambda: ThemeID.LIGHT)
    theme_manager.apply_preference(THEME_FOLLOW_SYSTEM)
    assert theme_manager.current_theme is ThemeID.LIGHT

    monkeypatch.setattr(theme_manager_module, "system_theme", lambda: ThemeID.DARK)
    theme_manager._on_system_scheme_changed()
    assert theme_manager.current_theme is ThemeID.DARK

    theme_manager.apply_theme(ThemeID.LIGHT)
    theme_manager._on_system_scheme_changed()
    assert theme_manager.current_theme is ThemeID.LIGHT


def test_system_theme_is_a_known_theme() -> None:
    assert theme_manager_module.system_theme() in set(ThemeID)
    assert THEME_FALLBACK is ThemeID.LIGHT


@pytest.mark.parametrize(
    ("stored", "expected"),
    [("studio_light", ThemeID.LIGHT), ("midnight_dark", ThemeID.DARK)],
)
def test_theme_saved_by_an_older_version_is_kept(
    theme_manager: ThemeManager, config: ConfigManager, stored: str, expected: ThemeID
) -> None:
    config.theme = stored
    assert theme_manager.apply_preference(config.theme) is True
    assert theme_manager.current_theme is expected
    assert theme_manager.preference == expected.value


def test_new_installation_follows_the_system(config: ConfigManager) -> None:
    assert config.theme == THEME_FOLLOW_SYSTEM


def test_broken_template_keeps_the_previous_theme(
    qapp: QApplication,
    theme_manager: ThemeManager,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)
    applied = qapp.styleSheet()
    broken = tmp_path / "broken.qss.template"
    broken.write_text("QWidget { color: {{no_such_colour}}; }", encoding="utf-8")
    monkeypatch.setattr(theme_manager_module, "_TEMPLATE_PATH", broken)

    assert theme_manager.apply_theme(ThemeID.DARK) is False
    assert theme_manager.current_theme is ThemeID.LIGHT
    assert tokens.active_theme() is ThemeID.LIGHT
    assert qapp.styleSheet() == applied


# ── Fonts ────────────────────────────────────────────────────────────────


def test_console_uses_the_bundled_fixed_width_font(
    qapp: QApplication, theme_manager: ThemeManager
) -> None:
    families = register_bundled_fonts()
    assert tokens.TYPOGRAPHY.console_family in families

    theme_manager.apply_theme(ThemeID.LIGHT)
    console = ConsoleView()
    console.show()
    qapp.processEvents()
    info = QFontInfo(console.font())
    console.close()

    assert info.family() == tokens.TYPOGRAPHY.console_family
    assert info.fixedPitch()


def test_console_font_size_survives_a_family_change(
    qapp: QApplication, theme_manager: ThemeManager
) -> None:
    register_bundled_fonts()
    theme_manager.apply_theme(ThemeID.LIGHT)
    console = ConsoleView()
    console.set_font_size(17)
    console.set_font_family("Some Font Nobody Has")
    assert console.font().pixelSize() == 17
    assert QFontInfo(console.font()).fixedPitch()
    console.close()


def _glyphs(font: QFont, text: str) -> list[int]:
    layout = QTextLayout(text, font)
    layout.beginLayout()
    layout.createLine()
    layout.endLayout()
    return [glyph for run in layout.glyphRuns() for glyph in run.glyphIndexes()]


@pytest.mark.parametrize("text", ["->", "!=", "|=", "<=", "==", "www", "0x0F", "::", "/*"])
def test_the_console_shows_each_character_on_its_own(qapp: QApplication, text: str) -> None:
    """Characters a device sends are never joined into one symbol, in
    the font the console starts with or after a change of size."""
    register_bundled_fonts()
    console = ConsoleView()
    console.set_font_size(15)
    font = console.font()
    console.close()

    separately = [glyph for character in text for glyph in _glyphs(font, character)]
    assert _glyphs(font, text) == separately


def test_monospace_font_lists_the_fallbacks_once() -> None:
    families = monospace_font("Consolas").families()
    assert families[0] == "Consolas"
    assert tokens.TYPOGRAPHY.console_family in families
    assert len(families) == len(set(families))


def test_interface_font_is_the_bundled_font_at_the_type_scale(qapp: QApplication) -> None:
    """Interface text is Poppins, shipped with the application, at the
    pixel sizes of the type scale: the same on every computer, whatever
    the system's own font and text size."""
    register_bundled_fonts()
    for name in ("ui", "caption", "title-sm"):
        style = tokens.TYPE_STYLES[name]
        font = interface_font(name)
        info = QFontInfo(font)
        assert info.family() == tokens.TYPOGRAPHY.interface_family, name
        assert info.pixelSize() == style.size, name
        assert font.weight() == style.weight, name
    assert interface_font().pixelSize() == tokens.TYPE_STYLES["ui"].size


def test_interface_font_is_never_a_console_style() -> None:
    with pytest.raises(KeyError):
        interface_font("mono")
    with pytest.raises(KeyError):
        interface_font("no-such-style")


def test_the_application_font_is_the_interface_font(
    qapp: QApplication, theme_manager: ThemeManager,
) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)
    font = qapp.font()
    assert font.family() == tokens.TYPOGRAPHY.interface_family
    assert font.pixelSize() == tokens.TYPE_STYLES["ui"].size


#: The classes Qt gives the operating system's own font when it replaces
#: the application's style (QApplicationPrivate::initializeWidgetFontHash);
#: on Windows it fills these from the system's menu, message, status and
#: icon title fonts.
_PLATFORM_FONT_CLASSES = ("QMenuBar", "QMenu", "QMessageBox", "QTipLabel", "QAbstractItemView")


def test_the_interface_font_outlasts_the_platform_fonts(
    qapp: QApplication, theme_manager: ThemeManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows the first style sheet brings back the system's fonts for
    menus, message boxes, tooltips and lists. This system brings none, so
    it is shown here the way Qt does it there."""
    platform = QFont("Segoe UI")
    platform.setPixelSize(tokens.TYPE_STYLES["ui"].size - 1)
    set_sheet = QApplication.setStyleSheet

    def replace_style(app: QApplication, sheet: str) -> None:
        set_sheet(app, sheet)
        for name in _PLATFORM_FONT_CLASSES:
            QApplication.setFont(platform, name)

    qapp.setStyleSheet("")
    monkeypatch.setattr(QApplication, "setStyleSheet", replace_style)
    theme_manager.apply_theme(ThemeID.LIGHT)

    for name in _PLATFORM_FONT_CLASSES:
        assert QApplication.font(name) == interface_font(), name


class _Hints:
    """Records the colour scheme asked for."""

    def __init__(self) -> None:
        self.asked: list[Qt.ColorScheme] = []

    def setColorScheme(self, scheme: Qt.ColorScheme) -> None:
        self.asked.append(scheme)

    def colorScheme(self) -> Qt.ColorScheme:
        return Qt.ColorScheme.Unknown


@pytest.mark.parametrize(
    ("preference", "scheme"),
    [
        (ThemeID.LIGHT.value, Qt.ColorScheme.Light),
        (ThemeID.DARK.value, Qt.ColorScheme.Dark),
        (THEME_FOLLOW_SYSTEM, Qt.ColorScheme.Unknown),
    ],
)
def test_the_system_is_asked_for_the_chosen_scheme(
    theme_manager: ThemeManager, monkeypatch: pytest.MonkeyPatch,
    preference: str, scheme: Qt.ColorScheme,
) -> None:
    """So that Windows draws the title bars in the theme chosen."""
    hints = _Hints()
    monkeypatch.setattr(theme_manager_module.QGuiApplication, "styleHints", lambda: hints)

    assert theme_manager.apply_preference(preference) is True
    assert hints.asked == [scheme]



def test_following_the_system_draws_the_theme_once(
    theme_manager: ThemeManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asking the system for its own scheme makes Qt announce the scheme at
    once, which draws the system's theme; the preference does not draw it again."""
    theme_manager.apply_theme(ThemeID.LIGHT)
    monkeypatch.setattr(theme_manager_module, "system_theme", lambda: ThemeID.DARK)

    class Announcing(_Hints):
        def setColorScheme(self, scheme: Qt.ColorScheme) -> None:
            super().setColorScheme(scheme)
            theme_manager._on_system_scheme_changed()

    monkeypatch.setattr(theme_manager_module.QGuiApplication, "styleHints", Announcing)
    drawn: list[str] = []
    theme_manager.theme_changed.connect(drawn.append)

    assert theme_manager.apply_preference(THEME_FOLLOW_SYSTEM) is True
    assert drawn == [ThemeID.DARK.value]


def test_frames_are_redrawn_once_the_new_theme_is_in_place(
    qapp: QApplication, theme_manager: ThemeManager, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows 10 shows a title bar's new colours only when told to draw it."""
    seen: list[QColor] = []
    monkeypatch.setattr(
        theme_manager_module, "redraw_frames",
        lambda: seen.append(qapp.palette().color(QPalette.ColorRole.Window)),
    )
    theme_manager.apply_theme(ThemeID.LIGHT)
    qapp.processEvents()
    seen.clear()

    theme_manager.apply_theme(ThemeID.DARK)
    assert seen == []
    qapp.processEvents()

    assert seen == [QColor(tokens.PALETTES[ThemeID.DARK].paper)]


@pytest.mark.skipif(sys.platform != "win32", reason="the frames Windows draws")
def test_frames_are_redrawn_on_windows(qapp: QApplication, theme_manager: ThemeManager) -> None:
    shown = QWidget()
    shown.show()
    qapp.processEvents()

    redraw_frames()

    assert shown.isVisible()
    shown.close()


def test_every_widget_draws_text_in_a_font_of_the_language(
    qapp: QApplication, config: ConfigManager, theme_manager: ThemeManager,
) -> None:
    """Every screen and dialog, every widget in it: Poppins or the console's
    fixed-width font, drawn as designed."""
    theme_manager.apply_theme(ThemeID.LIGHT)
    bundled = {*tokens.TYPOGRAPHY.interface_families, *tokens.TYPOGRAPHY.console_families}
    checked = 0
    for name, screen in every_screen(qapp, config, theme_manager):
        for widget in [screen, *screen.findChildren(QWidget)]:
            font = widget.font()
            where = f"{name}: {type(widget).__name__} {widget.objectName()!r}"
            assert QFontInfo(font).family() in bundled, where
            assert font.hintingPreference() == QFont.HintingPreference.PreferNoHinting, where
            checked += 1
    assert checked > 0


# ── Dot grid ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("theme_id", list(ThemeID))
def test_dot_grid_draws_dots_on_paper(
    qapp: QApplication, theme_manager: ThemeManager, theme_id: ThemeID
) -> None:
    theme_manager.apply_theme(theme_id)
    pitch = tokens.METRICS.grid_pitch
    area = DotGridWidget()
    area.resize(pitch * 3, pitch * 3)
    area.show()
    qapp.processEvents()
    paper = QColor(tokens.PALETTES[theme_id].paper)
    centre = pitch / 2
    assert pixel(area, 0, 0) == paper
    assert pixel(area, pitch, pitch) == paper
    for column in range(3):
        for row in range(3):
            dot = pixel(area, centre + column * pitch, centre + row * pitch)
            assert dot != paper, (column, row)
    area.close()


# ── Style states and icons ───────────────────────────────────────────────────


def test_primary_button_is_filled_with_the_text_colour(
    qapp: QApplication, theme_manager: ThemeManager
) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)
    palette = tokens.palette()
    button = QPushButton("Go")
    button.resize(120, 40)
    button.show()
    qapp.processEvents()
    assert pixel(button, 6, 20) == QColor(palette.paper)

    set_primary(button)
    qapp.processEvents()
    assert button.property(PRIMARY) is True
    assert pixel(button, 6, 20) == QColor(palette.text)

    set_primary(button, False)
    qapp.processEvents()
    assert pixel(button, 6, 20) == QColor(palette.paper)
    button.close()


def test_invalid_field_gets_the_error_outline(
    qapp: QApplication, theme_manager: ThemeManager
) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)
    palette = tokens.palette()
    window = DotGridWidget()
    field = QLineEdit(window)
    field.setGeometry(10, 10, 160, 32)
    elsewhere = QPushButton("Elsewhere", window)
    elsewhere.setGeometry(10, 60, 160, 32)
    window.resize(200, 110)
    window.show()
    qapp.processEvents()

    def outline() -> QColor:
        qapp.processEvents()
        return pixel(field, 80, 0)

    elsewhere.setFocus()
    assert outline() == QColor(palette.edge)

    # The error outline wins over the focus outline: the field being
    # typed in is exactly the one that must show it.
    set_state(field, INVALID, True)
    assert outline() == QColor(palette.error)
    field.setFocus()
    assert outline() == QColor(palette.error)

    set_state(field, INVALID, False)
    elsewhere.setFocus()
    assert outline() == QColor(palette.edge)
    window.close()


def test_bound_icon_is_redrawn_when_the_theme_changes(
    qapp: QApplication, theme_manager: ThemeManager
) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)
    button = QPushButton()
    assert icons.bind_icon(button, "refresh") is True
    light_key = button.icon().cacheKey()

    theme_manager.apply_theme(ThemeID.DARK)
    assert button.icon().cacheKey() != light_key
    assert not button.icon().isNull()
    button.close()


def test_unknown_icon_leaves_the_button_unchanged(theme_manager: ThemeManager) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)
    button = QPushButton("Text")
    assert icons.bind_icon(button, "no_such_icon") is False
    assert button.icon().isNull()
    button.close()


def test_destroyed_button_does_not_break_a_theme_change(
    qapp: QApplication, theme_manager: ThemeManager
) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)
    button = QPushButton()
    icons.bind_icon(button, "refresh")
    button.deleteLater()
    qapp.sendPostedEvents(None, 0)
    qapp.processEvents()
    assert theme_manager.apply_theme(ThemeID.DARK) is True


@pytest.mark.parametrize("level", ["info", "success", "warning", "error", "nonsense"])
def test_toast_shows_its_message_at_every_level(
    qapp: QApplication, theme_manager: ThemeManager, level: str
) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)
    parent = DotGridWidget()
    parent.resize(600, 200)
    parent.show()
    toast = Toast(parent)
    toast.show_toast("Saved", level)
    qapp.processEvents()
    assert toast.isVisible()
    assert toast.message == "Saved"
    assert toast.property("level") == (level if level != "nonsense" else "info")
    parent.close()


# ── The whole window follows the theme ───────────────────────────────────────


def test_open_console_is_redrawn_when_the_system_theme_changes(
    qapp: QApplication,
    config: ConfigManager,
    theme_manager: ThemeManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(theme_manager_module, "system_theme", lambda: ThemeID.LIGHT)
    theme_manager.apply_preference(THEME_FOLLOW_SYSTEM)
    tab = DeviceTab(theme_manager, config)
    tab.resize(600, 300)
    tab.show()
    console = tab.findChild(ConsoleView)
    console.show()
    qapp.processEvents()
    assert pixel(console.viewport(), 5, 5) == QColor(tokens.LIGHT.paper)

    monkeypatch.setattr(theme_manager_module, "system_theme", lambda: ThemeID.DARK)
    theme_manager._on_system_scheme_changed()
    qapp.processEvents()
    assert pixel(console.viewport(), 5, 5) == QColor(tokens.DARK.paper)

    tab.ordered_shutdown()
    tab.close()


def test_theme_menu_offers_system_light_and_dark(
    qapp: QApplication, config: ConfigManager, theme_manager: ThemeManager
) -> None:
    theme_manager.apply_preference(THEME_FOLLOW_SYSTEM)
    window = MainWindow(config, theme_manager)
    view = next(a.menu() for a in window.menuBar().actions() if a.text() == "&View")
    theme = next(a.menu() for a in view.actions() if a.text() == "Theme")
    actions = theme.actions()
    assert [a.text() for a in actions] == ["System", "Light", "Dark"]
    assert [a.isChecked() for a in actions] == [True, False, False]

    actions[2].trigger()
    assert theme_manager.current_theme is ThemeID.DARK
    assert config.theme == ThemeID.DARK.value
    assert [a.isChecked() for a in actions] == [False, False, True]

    actions[0].trigger()
    assert theme_manager.preference == THEME_FOLLOW_SYSTEM
    assert config.theme == THEME_FOLLOW_SYSTEM
    assert [a.isChecked() for a in actions] == [True, False, False]
    window.close()


def test_preferences_saves_the_theme_choice(
    qapp: QApplication, config: ConfigManager, theme_manager: ThemeManager
) -> None:
    theme_manager.apply_theme(ThemeID.LIGHT)
    config.theme = ThemeID.LIGHT.value
    dialog = PreferencesDialog(config, theme_manager)
    combo = dialog._theme
    assert [combo.itemText(i) for i in range(combo.count())] == ["System", "Light", "Dark"]
    assert combo.currentData() == ThemeID.LIGHT.value

    combo.setCurrentIndex(combo.findData(ThemeID.DARK.value))
    dialog._apply()
    assert config.theme == ThemeID.DARK.value
    assert theme_manager.current_theme is ThemeID.DARK
    dialog.close()
