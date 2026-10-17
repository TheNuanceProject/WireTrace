# SPDX-License-Identifier: GPL-3.0-or-later
"""The Preferences dialog.

One page per subject, chosen from the list on the left. Every setting
shown here has an effect; the rarely needed ones are on the Advanced
page. Values are read from and written to ``ConfigManager``.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from app.config import ConfigManager
from app.constants import (
    BAUD_RATES,
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    GUI_UPDATE_INTERVAL_MS_MAX,
    GUI_UPDATE_INTERVAL_MS_MIN,
    LOG_BUFFER_MAX_ENTRIES_MAX,
    LOG_BUFFER_MAX_ENTRIES_MIN,
    LOG_FLUSH_INTERVAL_MS_MAX,
    LOG_FLUSH_INTERVAL_MS_MIN,
    LOG_LINE_ENDINGS,
    LOG_SPLIT_MAX_MB,
    MAX_CONSOLE_LINES_MAX,
    MAX_CONSOLE_LINES_MIN,
    UPDATE_CHECK_INTERVAL_HOURS,
    ExportFormat,
    TimestampMode,
)
from app.plot_config import PlotProfileStore
from ui.dialogs.common import (
    ChoiceRow,
    action_button,
    button_row,
    heading,
    note,
    rule,
    tighten,
)
from ui.dialogs.messages import confirm
from ui.themes import tokens
from ui.themes.theme_manager import ThemeManager, normalise_preference
from ui.widgets.command_bar import LINE_ENDING_NAMES
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size
from ui.widgets.styling import set_primary
from ui.widgets.toast import Toast
from ui.widgets.vertical_scroll import VerticalScrollArea
from updater.install import UPDATE_CHECK_OPTION
from version import APP_NAME, APP_PRIVACY_URL

_UPDATES_AND_PRIVACY = "Updates and privacy"

#: The pages, in the order of the list. Advanced comes after a divider.
PAGES: tuple[str, ...] = (
    "Appearance", "Console", "Connection", "Logging", "Plot", _UPDATES_AND_PRIVACY, "Advanced",
)
_PAGE_AFTER_DIVIDER = "Advanced"

#: The stored word for each timestamp mode, and its name in the list.
_TIMESTAMP_CHOICES: tuple[tuple[str, TimestampMode], ...] = (
    ("Time of day", TimestampMode.ABSOLUTE),
    ("Since previous line", TimestampMode.RELATIVE),
)
_TIMESTAMP_STORED: dict[TimestampMode, str] = {
    TimestampMode.ABSOLUTE: "absolute",
    TimestampMode.RELATIVE: "relative",
}


class PreferencesDialog(DotGridDialog):
    """Edits the saved preferences. Save writes them; Cancel changes nothing."""

    def __init__(
        self,
        config: ConfigManager,
        theme_manager: ThemeManager,
        parent: QWidget | None = None,
        open_plot_config: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setModal(True)
        self._config = config
        self._theme_manager = theme_manager
        self._open_plot_config = open_plot_config
        self._plot_store = PlotProfileStore(config)
        self._hints: dict[str, str] = {}

        self._build()
        self._load_values()
        metrics = tokens.METRICS
        keep_least_size(self, metrics.dialog_wide_width, metrics.dialog_wide_height)

    # ── Building ─────────────────────────────────────────────────────────

    def _build(self) -> None:
        metrics = tokens.METRICS

        self._sidebar = QListWidget()
        self._sidebar.setObjectName("sidebar")
        self._sidebar.setFixedWidth(metrics.sidebar_width)
        self._sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._sidebar.setAccessibleName("Pages")
        self._pages = QStackedWidget()
        self._page_rows: dict[str, int] = {}

        builders: dict[str, Callable[[QVBoxLayout], None]] = {
            "Appearance": self._build_appearance,
            "Console": self._build_console,
            "Connection": self._build_connection,
            "Logging": self._build_logging,
            "Plot": self._build_plot,
            _UPDATES_AND_PRIVACY: self._build_updates_and_privacy,
            "Advanced": self._build_advanced,
        }
        for name in PAGES:
            if name == _PAGE_AFTER_DIVIDER:
                divider = QListWidgetItem()
                divider.setFlags(Qt.ItemFlag.NoItemFlags)
                divider.setData(Qt.ItemDataRole.AccessibleTextRole, "")
                divider.setSizeHint(QSize(0, metrics.space_4))
                self._sidebar.addItem(divider)
                self._sidebar.setItemWidget(divider, rule())
            self._page_rows[name] = self._sidebar.count()
            entry = QListWidgetItem(name)
            entry.setSizeHint(QSize(0, metrics.control_height))
            self._sidebar.addItem(entry)

            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(
                metrics.space_6, metrics.space_6, metrics.space_6, metrics.space_4
            )
            page_layout.setSpacing(metrics.space_4)
            page_layout.addWidget(heading(name, "pageTitle"))
            builders[name](page_layout)
            page_layout.addStretch(1)
            self._pages.addWidget(VerticalScrollArea(page))
        self._fit_sidebar_to_names()
        self._sidebar.currentRowChanged.connect(self._on_row_changed)
        self.show_page(PAGES[0])

        restore = action_button("Restore defaults")
        restore.clicked.connect(self._on_restore)
        cancel = action_button("Cancel")
        cancel.clicked.connect(self.reject)
        save = action_button("Save")
        set_primary(save)
        save.setDefault(True)
        save.clicked.connect(self._on_save)
        footer = QFrame()
        footer.setObjectName("dialogFooter")
        footer_layout = button_row(cancel, save, leading=restore)
        footer_layout.setContentsMargins(
            metrics.space_4, metrics.space_3, metrics.space_4, metrics.space_3
        )
        footer.setLayout(footer_layout)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._sidebar)
        body.addWidget(self._pages, 1)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addLayout(body, 1)
        root.addWidget(footer)

    def _fit_sidebar_to_names(self) -> None:
        """Make the list of pages wide enough for its longest name, in its
        own font, so that no name is cut at any size of text."""
        metrics = tokens.METRICS
        # The style sheet sets the font, so it is applied before measuring.
        self._sidebar.ensurePolished()
        widest = max(self._sidebar.fontMetrics().horizontalAdvance(name) for name in PAGES)
        # On both sides: the list's padding, the item's padding and the
        # margin Qt keeps around the text of an item; then the frame and
        # the line between the list and the page.
        text_margin = self._sidebar.style().pixelMetric(QStyle.PixelMetric.PM_FocusFrameHMargin) + 1
        needed = (
            widest
            + 2 * (metrics.space_2 + metrics.space_3 + text_margin + self._sidebar.frameWidth())
            + metrics.border
        )
        self._sidebar.setFixedWidth(max(metrics.sidebar_width, needed))

    def _add_row(self, page: QVBoxLayout, label: str, hint: str, field: QWidget) -> None:
        """Add a setting: its name and hint on the left, its field on the right."""
        metrics = tokens.METRICS
        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(metrics.space_half)
        text.addWidget(heading(label))
        text.addWidget(note(hint, small=True))
        text_column = QWidget()
        text_column.setLayout(text)
        text_column.setFixedWidth(metrics.form_label_width)

        row = QHBoxLayout()
        row.setSpacing(metrics.space_6)
        row.addWidget(text_column, 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(field, 0, Qt.AlignmentFlag.AlignTop)
        row.addStretch(1)
        page.addLayout(row)
        field.setAccessibleName(label)
        self._hints[label] = hint

    @staticmethod
    def _combo() -> QComboBox:
        combo = QComboBox()
        combo.setFixedWidth(tokens.METRICS.form_field_width)
        return combo

    @staticmethod
    def _number(low: int, high: int, suffix: str, step: int = 1) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(low, high)
        spin.setSingleStep(step)
        spin.setSuffix(f" {suffix}")
        spin.setGroupSeparatorShown(True)
        spin.setFixedWidth(tokens.METRICS.number_field_width)
        return spin

    # ── Pages ────────────────────────────────────────────────────────────

    def _build_appearance(self, page: QVBoxLayout) -> None:
        self._theme = self._combo()
        for preference, name in self._theme_manager.available_preferences():
            self._theme.addItem(name, preference)
        self._add_row(
            page, "Theme", "System follows the light or dark setting of your computer.",
            self._theme,
        )

    def _build_console(self, page: QVBoxLayout) -> None:
        self._font_family = self._combo()
        self._font_family.addItems(sorted(
            family for family in QFontDatabase.families()
            if QFontDatabase.isFixedPitch(family)
        ))
        self._add_row(
            page, "Font", "Fixed-width fonts only, so columns line up.", self._font_family,
        )

        self._font_size = self._number(FONT_SIZE_MIN, FONT_SIZE_MAX, "px")
        self._add_row(page, "Text size", "Also in the View menu.", self._font_size)

        self._timestamps = self._combo()
        for name, mode in _TIMESTAMP_CHOICES:
            self._timestamps.addItem(name, mode)
        self._add_row(
            page, "Timestamps", "For new tabs. Each tab can switch at any time.",
            self._timestamps,
        )

        self._severity_colours = QCheckBox("Colour lines by severity")
        self._add_row(
            page, "Colours", "Errors and warnings stand out. Off shows every line alike.",
            self._severity_colours,
        )

        self._follow = QCheckBox("Follow new lines")
        self._add_row(
            page, "Following", "For new tabs. The view stays on the newest line.",
            self._follow,
        )

        self._hide_blank = QCheckBox("Hide blank lines")
        self._add_row(
            page, "Blank lines", "On screen only. Logs and exports keep every line.",
            self._hide_blank,
        )

    def _build_connection(self, page: QVBoxLayout) -> None:
        self._baud = self._combo()
        for baud in BAUD_RATES:
            self._baud.addItem(str(baud), baud)
        self._add_row(
            page, "Baud rate",
            "Offered for a port that has not been used before. "
            "A port used before is offered what it last used.",
            self._baud,
        )

    def _build_logging(self, page: QVBoxLayout) -> None:
        metrics = tokens.METRICS
        self._log_folder = QLineEdit()
        self._log_folder.setMinimumWidth(metrics.form_field_width)
        browse = action_button("Browse")
        browse.clicked.connect(self._browse_log_folder)
        folder_row = QHBoxLayout()
        tighten(folder_row)
        folder_row.addWidget(self._log_folder, 1)
        folder_row.addWidget(browse)
        folder = QWidget()
        folder.setLayout(folder_row)
        self._add_row(
            page, "Log folder",
            "Where new logs are written. Choosing another folder when starting a log changes it.",
            folder,
        )

        self._log_format = ChoiceRow(
            (("Text", ExportFormat.TXT), ("Text and CSV", ExportFormat.BOTH)), ExportFormat.TXT,
        )
        self._add_row(
            page, "Log format",
            "The text log is always written. CSV adds a spreadsheet file beside it.",
            self._log_format,
        )

        self._split = self._number(0, LOG_SPLIT_MAX_MB, "MB")
        self._split.setSpecialValueText("Off")
        self._add_row(
            page, "Maximum log file size",
            "A log that reaches this size continues in a new file. Off keeps each log in one file.",
            self._split,
        )

        self._line_ending = ChoiceRow(
            tuple((LINE_ENDING_NAMES[ending], ending) for ending in LOG_LINE_ENDINGS),
            LOG_LINE_ENDINGS[0],
        )
        self._add_row(
            page, "Line endings in files",
            "For logs and exports. LF is the same on every system. "
            "CR LF suits older Windows tools.",
            self._line_ending,
        )

    def _build_plot(self, page: QVBoxLayout) -> None:
        self._plot_profile = self._combo()
        self._add_row(
            page, "Default profile", "Used by a port that has no profile of its own yet.",
            self._plot_profile,
        )

        self._configure_plot = action_button("Configure plot...")
        self._configure_plot.setEnabled(self._open_plot_config is not None)
        self._configure_plot.clicked.connect(self._on_configure_plot)
        self._add_row(
            page, "Profiles", "Add, rename, delete or test plot profiles.",
            self._configure_plot,
        )

    def _build_updates_and_privacy(self, page: QVBoxLayout) -> None:
        self._check_updates = QCheckBox(UPDATE_CHECK_OPTION)
        self._add_row(
            page, "Updates",
            f"Looks up whether a newer version exists: when {APP_NAME} starts, and every "
            f"{UPDATE_CHECK_INTERVAL_HOURS} hours while it stays open. You decide whether "
            "to install.",
            self._check_updates,
        )

        self._usage_statistics = QCheckBox("Share usage statistics")
        what_is_sent = action_button("What is sent")
        what_is_sent.clicked.connect(self._show_privacy_policy)
        usage_row = QVBoxLayout()
        tighten(usage_row)
        usage_row.addWidget(self._usage_statistics)
        usage_row.addWidget(what_is_sent, 0, Qt.AlignmentFlag.AlignLeft)
        usage = QWidget()
        usage.setLayout(usage_row)
        self._add_row(
            page, "Usage statistics",
            "Off unless you switch it on. At most once a day: which features were used "
            "and whether anything went wrong, with no identifier and nothing your "
            "devices send.",
            usage,
        )

    def _build_advanced(self, page: QVBoxLayout) -> None:
        page.addWidget(note(
            "The defaults suit almost every machine. Change these only if you have a reason to."
        ))
        self._console_lines = self._number(
            MAX_CONSOLE_LINES_MIN, MAX_CONSOLE_LINES_MAX, "lines", step=10_000,
        )
        self._add_row(
            page, "Console line limit",
            "Lines kept on screen. Older lines stay in the session and the log.",
            self._console_lines,
        )
        self._refresh = self._number(
            GUI_UPDATE_INTERVAL_MS_MIN, GUI_UPDATE_INTERVAL_MS_MAX, "ms",
        )
        self._add_row(
            page, "Screen refresh",
            "How often new lines are drawn. A larger value eases a slow machine.",
            self._refresh,
        )
        self._flush = self._number(LOG_FLUSH_INTERVAL_MS_MIN, LOG_FLUSH_INTERVAL_MS_MAX, "ms")
        self._add_row(
            page, "Write to disk every",
            "How long a received line may wait in memory before it is written.",
            self._flush,
        )
        self._buffer = self._number(
            LOG_BUFFER_MAX_ENTRIES_MIN, LOG_BUFFER_MAX_ENTRIES_MAX, "lines", step=5_000,
        )
        self._add_row(
            page, "Lines that may wait for a slow disk",
            "Beyond this, lines are counted as lost and marked in the log.",
            self._buffer,
        )

    # ── Public API ───────────────────────────────────────────────────────────

    def show_page(self, name: str) -> None:
        self._sidebar.setCurrentRow(self._page_rows[name])

    @property
    def current_page(self) -> str:
        return PAGES[self._pages.currentIndex()]

    def setting_names(self) -> list[str]:
        """The name of every setting shown, across all pages."""
        return list(self._hints)

    # ── Load / save ──────────────────────────────────────────────────────────

    @staticmethod
    def _select(combo: QComboBox, data: object) -> None:
        index = combo.findData(data)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _load_values(self) -> None:
        config = self._config
        self._select(self._theme, normalise_preference(config.theme))

        # A font that is saved but not installed is still shown, so that
        # saving the dialog does not silently change it.
        if self._font_family.findText(config.font_family) < 0:
            self._font_family.addItem(config.font_family)
        self._font_family.setCurrentText(config.font_family)
        self._font_size.setValue(config.font_size)
        stored_mode = next(
            (mode for mode, word in _TIMESTAMP_STORED.items() if word == config.timestamp_mode),
            TimestampMode.ABSOLUTE,
        )
        self._select(self._timestamps, stored_mode)
        self._severity_colours.setChecked(config.color_mode)
        self._follow.setChecked(config.auto_scroll)
        self._hide_blank.setChecked(config.hide_blank_lines)

        if self._baud.findData(config.default_baud_rate) < 0:
            self._baud.addItem(str(config.default_baud_rate), config.default_baud_rate)
        self._select(self._baud, config.default_baud_rate)

        self._log_folder.setText(config.default_log_directory)
        self._log_format.set_value(config.default_log_format)
        self._split.setValue(config.log_split_size_mb)
        self._line_ending.set_value(config.log_line_ending)

        self._load_plot_profiles()
        self._check_updates.setChecked(config.check_updates_on_startup)
        self._usage_statistics.setChecked(config.usage_statistics)

        self._console_lines.setValue(config.max_console_lines)
        self._refresh.setValue(config.gui_update_interval_ms)
        self._flush.setValue(config.log_flush_interval_ms)
        self._buffer.setValue(config.log_buffer_max_entries)

    def _load_plot_profiles(self) -> None:
        chosen = self._plot_profile.currentData() or self._plot_store.default_name
        self._plot_profile.clear()
        for name in self._plot_store.names():
            self._plot_profile.addItem(name, name)
        self._select(self._plot_profile, chosen)
        if self._plot_profile.currentData() != chosen:
            self._select(self._plot_profile, self._plot_store.default_name)

    def _apply(self) -> None:
        config = self._config
        config.theme = self._theme.currentData()
        if config.theme != self._theme_manager.preference:
            self._theme_manager.apply_preference(config.theme)

        config.font_family = self._font_family.currentText()
        config.font_size = self._font_size.value()
        config.timestamp_mode = _TIMESTAMP_STORED[self._timestamps.currentData()]
        config.color_mode = self._severity_colours.isChecked()
        config.auto_scroll = self._follow.isChecked()
        config.hide_blank_lines = self._hide_blank.isChecked()

        config.default_baud_rate = self._baud.currentData()

        config.default_log_directory = self._log_folder.text().strip()
        config.default_log_format = self._log_format.value
        config.log_split_size_mb = self._split.value()
        config.log_line_ending = self._line_ending.value

        if self._plot_profile.currentData():
            self._plot_store.set_default(self._plot_profile.currentData())
        config.check_updates_on_startup = self._check_updates.isChecked()
        config.usage_statistics = self._usage_statistics.isChecked()

        config.max_console_lines = self._console_lines.value()
        config.gui_update_interval_ms = self._refresh.value()
        config.log_flush_interval_ms = self._flush.value()
        config.log_buffer_max_entries = self._buffer.value()
        config.save()

    # ── Actions ──────────────────────────────────────────────────────────

    def _on_row_changed(self, row: int) -> None:
        for index, name in enumerate(PAGES):
            if self._page_rows[name] == row:
                self._pages.setCurrentIndex(index)

    def _on_save(self) -> None:
        self._apply()
        self.accept()

    def _on_restore(self) -> None:
        if not confirm(
            self, "Restore defaults", "Return every setting to its default?",
            detail=(
                "This happens at once and cannot be undone. Plot profiles, saved "
                "commands, what each port was last used with and the update check "
                "setting are kept."
            ),
            proceed="Restore defaults",
        ):
            return
        self._config.reset_to_defaults()
        self._config.save()
        self._load_values()

    def _show_privacy_policy(self) -> None:
        if not QDesktopServices.openUrl(QUrl(APP_PRIVACY_URL)):
            Toast.warning(self, f"The page could not be opened: {APP_PRIVACY_URL}")

    def _browse_log_folder(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose the log folder", self._log_folder.text(),
        )
        if chosen:
            self._log_folder.setText(chosen)

    def _on_configure_plot(self) -> None:
        if self._open_plot_config is not None:
            self._open_plot_config()
            # Profiles may have been added, renamed or deleted.
            self._load_plot_profiles()
