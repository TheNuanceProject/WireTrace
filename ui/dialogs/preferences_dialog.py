# SPDX-License-Identifier: MIT
"""WireTrace Preferences — enterprise-grade settings dialog.

Single-page scrollable layout with grouped sections instead of cluttered tabs.
Each setting has a clear label and description. Professional save feedback.
Maps to preferences.ini via ConfigManager per spec section 8.3.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import ConfigManager
from app.constants import (
    BAUD_RATES,
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    GUI_UPDATE_INTERVAL_MS,
    LOG_BUFFER_MAX_ENTRIES,
    LOG_FLUSH_INTERVAL_MS,
    MAX_CONSOLE_LINES,
    ThemeID,
)
from ui.themes.theme_manager import ThemeManager

_LABEL_W = 160
_CTRL_W = 220


class PreferencesDialog(QDialog):
    """Production-grade preferences dialog with grouped sections."""

    def __init__(
        self,
        config: ConfigManager,
        theme_manager: ThemeManager,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.setMinimumSize(520, 540)
        self.setMaximumWidth(600)
        self.setModal(True)

        self._config = config
        self._theme_manager = theme_manager
        self._dirty = False

        self._setup_ui()
        self._load_values()

    # ══════════════════════════════════════════════════════════════════════
    # UI SETUP
    # ══════════════════════════════════════════════════════════════════════

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Scrollable content area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        self._layout = QVBoxLayout(content)
        self._layout.setContentsMargins(24, 20, 24, 12)
        self._layout.setSpacing(4)

        self._build_appearance_section()
        self._add_separator()
        self._build_display_section()
        self._add_separator()
        self._build_serial_section()
        self._add_separator()
        self._build_storage_section()
        self._add_separator()
        self._build_performance_section()
        self._add_separator()
        self._build_updates_section()

        self._layout.addStretch()
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        # Footer bar
        footer = QWidget()
        footer.setObjectName("preferencesFooter")
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(16, 8, 16, 8)

        self._status_label = QLabel("")
        self._status_label.setProperty("secondary", True)

        reset_btn = QPushButton("Reset to Defaults")
        reset_btn.setObjectName("clearBtn")
        reset_btn.setFixedHeight(26)
        reset_btn.clicked.connect(self._on_reset)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("clearBtn")
        cancel_btn.setFixedHeight(26)
        cancel_btn.clicked.connect(self.reject)

        save_btn = QPushButton("Save")
        save_btn.setFixedHeight(26)
        save_btn.setMinimumWidth(80)
        save_btn.clicked.connect(self._on_save)

        footer_layout.addWidget(self._status_label, 1)
        footer_layout.addWidget(reset_btn)
        footer_layout.addSpacing(8)
        footer_layout.addWidget(cancel_btn)
        footer_layout.addSpacing(4)
        footer_layout.addWidget(save_btn)

        root.addWidget(footer)

    # ── Section Builders ───────────────────────────────────────────────

    def _build_appearance_section(self) -> None:
        self._add_section_header("Appearance")

        self._theme_combo = QComboBox()
        self._theme_combo.setFixedWidth(_CTRL_W)
        for tid, name in self._theme_manager.available_themes():
            self._theme_combo.addItem(name, tid)
        self._add_row("Theme", "Visual theme for the application", self._theme_combo)

    def _build_display_section(self) -> None:
        self._add_section_header("Console Display")

        self._font_combo = QComboBox()
        self._font_combo.setFixedWidth(_CTRL_W)
        # Populate with system monospace fonts
        mono_families = []
        for family in QFontDatabase.families():
            if QFontDatabase.isFixedPitch(family):
                mono_families.append(family)
        mono_families.sort()
        for family in mono_families:
            self._font_combo.addItem(family)
        # If no monospace fonts found, add common fallbacks
        if not mono_families:
            for f in ("Consolas", "Courier New", "Monospace"):
                self._font_combo.addItem(f)
        self._add_row("Font family", "Monospace font for the console", self._font_combo)

        self._font_spin = QSpinBox()
        self._font_spin.setRange(FONT_SIZE_MIN, FONT_SIZE_MAX)
        self._font_spin.setFixedWidth(80)
        self._font_spin.setSuffix(" pt")
        self._add_row("Font size", "Console text size", self._font_spin)

        self._ts_combo = QComboBox()
        self._ts_combo.setFixedWidth(_CTRL_W)
        self._ts_combo.addItem("Timestamp (date and time)", "absolute")
        self._ts_combo.addItem("Elapsed (time between lines)", "relative")
        self._add_row("Default timestamps", "Can be toggled per session", self._ts_combo)

        self._color_cb = QCheckBox("Enable severity color coding")
        self._add_row("Colors", "Highlight messages by severity level", self._color_cb)

        self._scroll_cb = QCheckBox("Auto-scroll to latest data")
        self._add_row("Auto-scroll", "Follow new data as it arrives", self._scroll_cb)

    def _build_serial_section(self) -> None:
        self._add_section_header("Serial Connection")

        self._baud_combo = QComboBox()
        self._baud_combo.setFixedWidth(_CTRL_W)
        for baud in BAUD_RATES:
            self._baud_combo.addItem(f"{baud:,} baud", baud)
        self._add_row("Default baud rate", "Used when opening new connections", self._baud_combo)

    def _build_storage_section(self) -> None:
        self._add_section_header("Storage")

        dir_w = QWidget()
        dir_lay = QHBoxLayout(dir_w)
        dir_lay.setContentsMargins(0, 0, 0, 0)
        dir_lay.setSpacing(6)
        self._dir_input = QLineEdit()
        self._dir_input.setFixedHeight(26)
        self._dir_input.setMinimumWidth(200)
        self._dir_input.setReadOnly(True)
        self._dir_input.setPlaceholderText("Select a directory...")
        browse = QPushButton("Browse")
        browse.setObjectName("clearBtn")
        browse.setFixedSize(72, 26)
        browse.clicked.connect(self._browse_dir)
        dir_lay.addWidget(self._dir_input, 1)
        dir_lay.addWidget(browse)
        self._add_row("Log directory", "Default location for new log files", dir_w)

        self._format_combo = QComboBox()
        self._format_combo.setFixedWidth(_CTRL_W)
        self._format_combo.addItem("Text (.txt)", "txt")
        self._format_combo.addItem("CSV (.csv)", "csv")
        self._format_combo.addItem("Both (.txt + .csv)", "both")
        self._add_row("Default export", "Format for new log sessions", self._format_combo)

    def _build_performance_section(self) -> None:
        self._add_section_header("Performance")
        self._add_description(
            "These settings affect resource usage. Defaults are suitable for most hardware."
        )

        self._gui_spin = QSpinBox()
        self._gui_spin.setRange(16, 500)
        self._gui_spin.setFixedWidth(100)
        self._gui_spin.setSuffix(" ms")
        self._add_row(
            "GUI refresh rate",
            f"How often the display updates (default: {GUI_UPDATE_INTERVAL_MS} ms)",
            self._gui_spin,
        )

        self._buffer_spin = QSpinBox()
        self._buffer_spin.setRange(1000, 100000)
        self._buffer_spin.setSingleStep(5000)
        self._buffer_spin.setFixedWidth(100)
        self._add_row(
            "Write buffer size",
            f"Lines buffered before disk write (default: {LOG_BUFFER_MAX_ENTRIES:,})",
            self._buffer_spin,
        )

        self._flush_spin = QSpinBox()
        self._flush_spin.setRange(200, 5000)
        self._flush_spin.setFixedWidth(100)
        self._flush_spin.setSuffix(" ms")
        self._add_row(
            "Flush interval",
            f"Time between disk writes (default: {LOG_FLUSH_INTERVAL_MS} ms)",
            self._flush_spin,
        )

        self._lines_spin = QSpinBox()
        self._lines_spin.setRange(10000, 500000)
        self._lines_spin.setSingleStep(10000)
        self._lines_spin.setFixedWidth(100)
        self._add_row(
            "Console line limit",
            f"Maximum lines kept in display (default: {MAX_CONSOLE_LINES:,})",
            self._lines_spin,
        )

    def _build_updates_section(self) -> None:
        self._add_section_header("Updates")

        self._updates_cb = QCheckBox("Check for updates on startup")
        self._add_row("Auto-update", "Notify when a new version is available", self._updates_cb)

    # ── Layout Helpers ─────────────────────────────────────────────────

    def _add_section_header(self, title: str) -> None:
        lbl = QLabel(title)
        font = lbl.font()
        font.setPointSize(12)
        font.setWeight(QFont.Weight.DemiBold)
        lbl.setFont(font)
        lbl.setContentsMargins(0, 12, 0, 4)
        self._layout.addWidget(lbl)

    def _add_description(self, text: str) -> None:
        lbl = QLabel(text)
        lbl.setProperty("secondary", True)
        lbl.setWordWrap(True)
        f = lbl.font()
        f.setPointSize(10)
        lbl.setFont(f)
        lbl.setContentsMargins(0, 0, 0, 4)
        self._layout.addWidget(lbl)

    def _add_row(self, label: str, hint: str, widget: QWidget) -> None:
        row = QWidget()
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(0, 4, 0, 4)
        row_lay.setSpacing(12)

        label_col = QWidget()
        label_col.setFixedWidth(_LABEL_W)
        lc_lay = QVBoxLayout(label_col)
        lc_lay.setContentsMargins(0, 0, 0, 0)
        lc_lay.setSpacing(1)

        name_lbl = QLabel(label)
        name_font = name_lbl.font()
        name_font.setPointSize(11)
        name_lbl.setFont(name_font)
        lc_lay.addWidget(name_lbl)

        hint_lbl = QLabel(hint)
        hint_lbl.setProperty("secondary", True)
        hint_font = hint_lbl.font()
        hint_font.setPointSize(9)
        hint_lbl.setFont(hint_font)
        hint_lbl.setWordWrap(True)
        lc_lay.addWidget(hint_lbl)

        row_lay.addWidget(label_col)
        row_lay.addWidget(widget)
        row_lay.addStretch()

        self._layout.addWidget(row)

    def _add_separator(self) -> None:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setFixedHeight(1)
        line.setContentsMargins(0, 8, 0, 8)
        self._layout.addWidget(line)

    # ══════════════════════════════════════════════════════════════════════
    # LOAD / SAVE
    # ══════════════════════════════════════════════════════════════════════

    def _load_values(self) -> None:
        c = self._config

        # Appearance
        idx = self._theme_combo.findData(
            ThemeID(c.theme) if c.theme else ThemeID.STUDIO_LIGHT
        )
        if idx >= 0:
            self._theme_combo.setCurrentIndex(idx)

        # Display
        fi = self._font_combo.findText(c.font_family)
        if fi >= 0:
            self._font_combo.setCurrentIndex(fi)
        else:
            self._font_combo.setEditText(c.font_family)
        self._font_spin.setValue(c.font_size)
        ti = self._ts_combo.findData(c.timestamp_mode)
        if ti >= 0:
            self._ts_combo.setCurrentIndex(ti)
        self._color_cb.setChecked(c.color_mode)
        self._scroll_cb.setChecked(c.auto_scroll)

        # Serial
        bi = self._baud_combo.findData(c.default_baud_rate)
        if bi >= 0:
            self._baud_combo.setCurrentIndex(bi)

        # Storage
        self._dir_input.setText(c.default_log_directory)
        ei = self._format_combo.findData(c.default_export_format)
        if ei >= 0:
            self._format_combo.setCurrentIndex(ei)

        # Performance
        self._gui_spin.setValue(c.gui_update_interval_ms)
        self._buffer_spin.setValue(c.log_buffer_max_entries)
        self._flush_spin.setValue(c.log_flush_interval_ms)
        self._lines_spin.setValue(c.max_console_lines)

        # Updates
        self._updates_cb.setChecked(c.check_updates_on_startup)

    def _apply(self) -> None:
        c = self._config

        # Appearance
        c.theme = self._theme_combo.currentData().value
        theme_id = self._theme_combo.currentData()
        if theme_id != self._theme_manager.current_theme:
            self._theme_manager.apply_theme(theme_id)

        # Display
        c.font_family = self._font_combo.currentText()
        c.font_size = self._font_spin.value()
        c.timestamp_mode = self._ts_combo.currentData()
        c.color_mode = self._color_cb.isChecked()
        c.auto_scroll = self._scroll_cb.isChecked()

        # Serial
        c.default_baud_rate = self._baud_combo.currentData()

        # Storage
        c.default_log_directory = self._dir_input.text().strip()
        c.default_export_format = self._format_combo.currentData()

        # Performance
        c.set("Performance", "gui_update_interval_ms", self._gui_spin.value())
        c.set("Performance", "log_buffer_max_entries", self._buffer_spin.value())
        c.set("Performance", "log_flush_interval_ms", self._flush_spin.value())
        c.set("Performance", "max_console_lines", self._lines_spin.value())

        # Updates
        c.check_updates_on_startup = self._updates_cb.isChecked()

        c.save()

    def _on_save(self) -> None:
        self._apply()
        self._show_status("Preferences saved successfully")
        QTimer.singleShot(800, self.accept)

    def _on_reset(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle("Reset Preferences")
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setText("This will restore all settings to their default values.")
        msg.setInformativeText("This action cannot be undone.")
        cancel_btn = msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        reset_btn = msg.addButton("Reset to Defaults", QMessageBox.ButtonRole.AcceptRole)
        msg.setDefaultButton(cancel_btn)
        msg.exec()
        if msg.clickedButton() == reset_btn:
            self._config.reset_to_defaults()
            self._config.save()
            self._load_values()
            # Theme is NOT applied here — main_window._on_menu_preferences()
            # handles theme sync, tag colors, and fonts after dialog closes.
            # Applying theme mid-dialog would break the console display.
            self._show_status("Settings restored to defaults")

    def _browse_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Select Log Directory", self._dir_input.text()
        )
        if d:
            self._dir_input.setText(d)

    def _show_status(self, msg: str) -> None:
        self._status_label.setText(msg)
        QTimer.singleShot(4000, lambda: self._status_label.setText(""))
