# SPDX-License-Identifier: MIT
"""WireTrace Configure Plot dialog.

User-facing surface for the manual plotting configuration system.
Lets the engineer:

  1. Choose AUTO mode (the default — let WireTrace detect the format)
     or MANUAL mode (declare a regex with named groups).
  2. Save / load / rename / delete named profiles for reuse across
     sessions and devices. Profiles persist via PlotProfileStore.
  3. Test their pattern against recent DATA lines from the active tab,
     with a tiered visual result (green / amber / red).
  4. Scaffold a regex from a sample line by clicking the values they
     want to plot — the "Capture from sample" assistant.

Design notes:
  - The dialog is a passive data structure: it owns no engine and
    triggers no I/O. It receives a ``recent_lines_provider`` callable
    that the host (DeviceTab / MainWindow) wires to the live engine.
  - The ``configChanged`` signal is the dialog's ONLY output — the
    host listens, applies the new ``PlotConfig`` to its engine, and
    closes the loop.
  - Apply is enabled whenever the configuration is structurally
    valid:
      * Auto mode → always valid
      * Manual mode → green (matches recent lines) OR amber (compiles
        and has named groups but didn't match — firmware happens to
        be quiet right now)
    Apply is disabled only when the pattern is structurally broken
    (red) — there's no path where applying it could work.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QStackedLayout,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.plot_config import (
    AUTO_PROFILE_NAME,
    PROFILE_NAME_MAX,
    PROFILE_PATTERN_MAX,
    PlotConfig,
    PlotProfileStore,
)
from core.plot_parsers import RegexParser, RegexParserError

logger = logging.getLogger(__name__)


# ── Visual constants ─────────────────────────────────────────────────────────

# Tiered status colours. Theme-neutral semantics — green/amber/red read
# the same in both Studio Light and Midnight Dark.
_STATUS_GREEN = "#2E7D32"
_STATUS_AMBER = "#E65100"
_STATUS_RED = "#C62828"
_STATUS_NEUTRAL = "#616161"

#: Visual width caps to keep the dialog reading like the rest of WireTrace.
_DIALOG_MIN_W = 640
_DIALOG_MIN_H = 580


# ── Dialog ───────────────────────────────────────────────────────────────────

class PlotConfigDialog(QDialog):
    """Modal dialog for configuring a tab's plot mode and profile.

    Args:
        store: PlotProfileStore — owns persisted profiles.
        recent_lines_provider: callable returning the live engine's
            recent DATA lines (most recent last). May return [].
        current_config: the PlotConfig currently in effect on the
            host engine (so the dialog opens preselected to it).
        parent: parent QWidget for modality.

    Signals:
        configChanged(PlotConfig): emitted when the user clicks Apply
            with a valid configuration. The host should apply this to
            the engine and close the loop.
    """

    configChanged = Signal(object)  # PlotConfig

    def __init__(
        self,
        store: PlotProfileStore,
        recent_lines_provider: Callable[[], list[str]],
        current_config: PlotConfig | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Configure Plot")
        self.setMinimumSize(_DIALOG_MIN_W, _DIALOG_MIN_H)
        self.setModal(True)

        self._store = store
        self._recent_lines_provider = recent_lines_provider
        self._editing_unsaved = False  # form has changes vs selected profile

        self._setup_ui()
        self._populate_profiles()
        self._select_initial(current_config)
        self._refresh_test_status()

    # ── Setup ────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(12)

        # ── Mode toggle ────────────────────────────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(16)
        mode_label = QLabel("Mode:")
        mode_label.setMinimumWidth(60)
        self._mode_auto_rb = QRadioButton("Auto-detect")
        self._mode_auto_rb.setToolTip(
            "WireTrace detects JSON / key:value / delimited formats automatically.",
        )
        self._mode_manual_rb = QRadioButton("Manual (regex)")
        self._mode_manual_rb.setToolTip(
            "Declare your own regex with named groups for full control.",
        )
        self._mode_group = QButtonGroup(self)
        self._mode_group.addButton(self._mode_auto_rb)
        self._mode_group.addButton(self._mode_manual_rb)
        self._mode_auto_rb.setChecked(True)
        self._mode_auto_rb.toggled.connect(self._on_mode_toggled)
        mode_row.addWidget(mode_label)
        mode_row.addWidget(self._mode_auto_rb)
        mode_row.addWidget(self._mode_manual_rb)
        mode_row.addStretch()
        root.addLayout(mode_row)

        # ── Profile bar ────────────────────────────────────────────
        profile_row = QHBoxLayout()
        profile_row.setSpacing(8)
        profile_label = QLabel("Profile:")
        profile_label.setMinimumWidth(60)

        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(220)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_selected)

        self._save_as_btn = QPushButton("Save As\u2026")
        self._save_as_btn.setObjectName("clearBtn")
        self._save_as_btn.setFixedHeight(26)
        self._save_as_btn.clicked.connect(self._on_save_as)

        self._rename_btn = QPushButton("Rename")
        self._rename_btn.setObjectName("clearBtn")
        self._rename_btn.setFixedHeight(26)
        self._rename_btn.clicked.connect(self._on_rename)

        self._delete_btn = QPushButton("Delete")
        self._delete_btn.setObjectName("clearBtn")
        self._delete_btn.setFixedHeight(26)
        self._delete_btn.clicked.connect(self._on_delete)

        profile_row.addWidget(profile_label)
        profile_row.addWidget(self._profile_combo, 1)
        profile_row.addWidget(self._save_as_btn)
        profile_row.addWidget(self._rename_btn)
        profile_row.addWidget(self._delete_btn)
        root.addLayout(profile_row)

        # Visual separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(sep)

        # ── Manual section (stacked: hidden in auto mode) ─────────
        # We use a stacked layout rather than show/hide so the dialog
        # stays the same size in both modes — no flicker.
        self._manual_panel = QWidget()
        manual_layout = QVBoxLayout(self._manual_panel)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.setSpacing(8)

        # Pattern editor
        pattern_label = QLabel("Pattern:")
        pattern_label.setMinimumHeight(20)
        self._pattern_edit = QPlainTextEdit()
        self._pattern_edit.setObjectName("plotConfigPatternEdit")
        self._pattern_edit.setPlaceholderText(
            r"e.g.  RPM:\s*(?P<RPM>\d+),\s*Voltage:\s*(?P<Voltage>[\d.]+)",
        )
        self._pattern_edit.setMaximumHeight(64)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._pattern_edit.setFont(mono)
        self._pattern_edit.textChanged.connect(self._on_pattern_changed)

        pattern_hint = QLabel(
            "Each <code>(?P&lt;name&gt;\u2026)</code> named group becomes a "
            "trace named <i>name</i>.",
        )
        pattern_hint.setProperty("secondary", True)
        pattern_hint.setTextFormat(Qt.TextFormat.RichText)
        pattern_hint.setWordWrap(True)

        manual_layout.addWidget(pattern_label)
        manual_layout.addWidget(self._pattern_edit)
        manual_layout.addWidget(pattern_hint)

        # Recent lines panel
        recent_header = QHBoxLayout()
        recent_label = QLabel("Recent lines from this session:")
        self._refresh_lines_btn = QPushButton("Refresh")
        self._refresh_lines_btn.setObjectName("clearBtn")
        self._refresh_lines_btn.setFixedHeight(22)
        self._refresh_lines_btn.clicked.connect(self._refresh_recent_lines)
        recent_header.addWidget(recent_label)
        recent_header.addStretch()
        recent_header.addWidget(self._refresh_lines_btn)
        manual_layout.addLayout(recent_header)

        self._recent_list = QListWidget()
        self._recent_list.setObjectName("plotConfigRecentList")
        self._recent_list.setFont(mono)
        self._recent_list.setSelectionMode(
            QListWidget.SelectionMode.SingleSelection,
        )
        self._recent_list.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        manual_layout.addWidget(self._recent_list, 1)

        # Capture + Test row
        capture_row = QHBoxLayout()
        self._capture_btn = QPushButton("Capture from sample\u2026")
        self._capture_btn.setObjectName("clearBtn")
        self._capture_btn.setFixedHeight(26)
        self._capture_btn.setToolTip(
            "Pick a sample line, then name each numeric value to capture; "
            "the dialog scaffolds the regex for you.",
        )
        self._capture_btn.clicked.connect(self._on_capture_assistant)

        self._test_btn = QPushButton("Test pattern")
        self._test_btn.setObjectName("clearBtn")
        self._test_btn.setFixedHeight(26)
        self._test_btn.clicked.connect(self._refresh_test_status)

        capture_row.addWidget(self._capture_btn)
        capture_row.addStretch()
        capture_row.addWidget(self._test_btn)
        manual_layout.addLayout(capture_row)

        # Test status line — coloured tier indicator
        self._status_label = QLabel("")
        self._status_label.setObjectName("plotConfigStatus")
        self._status_label.setTextFormat(Qt.TextFormat.RichText)
        self._status_label.setWordWrap(True)
        self._status_label.setMinimumHeight(36)
        manual_layout.addWidget(self._status_label)

        # ── Auto-mode placeholder (shown when mode = auto) ────────
        self._auto_panel = QWidget()
        auto_layout = QVBoxLayout(self._auto_panel)
        auto_layout.setContentsMargins(0, 0, 0, 0)
        self._auto_blurb = QLabel(
            "<b>Auto-detect</b> scans incoming serial data and identifies "
            "JSON, key:value, or delimited formats automatically.<br><br>"
            "If your firmware uses a custom format, switch to "
            "<b>Manual</b> and declare a regex.",
        )
        self._auto_blurb.setProperty("secondary", True)
        self._auto_blurb.setWordWrap(True)
        self._auto_blurb.setTextFormat(Qt.TextFormat.RichText)
        self._auto_blurb.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
        )
        auto_layout.addWidget(self._auto_blurb)
        auto_layout.addStretch()

        # Stack: index 0 = auto, 1 = manual
        self._stack = QStackedLayout()
        self._stack.addWidget(self._auto_panel)
        self._stack.addWidget(self._manual_panel)
        stack_holder = QWidget()
        stack_holder.setLayout(self._stack)
        root.addWidget(stack_holder, 1)

        # ── Footer ─────────────────────────────────────────────────
        footer = QHBoxLayout()
        footer.setSpacing(8)
        footer.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("clearBtn")
        cancel_btn.setFixedHeight(26)
        cancel_btn.clicked.connect(self.reject)
        self._apply_btn = QPushButton("Apply")
        self._apply_btn.setFixedHeight(26)
        self._apply_btn.setMinimumWidth(96)
        self._apply_btn.setDefault(True)
        self._apply_btn.clicked.connect(self._on_apply)
        footer.addWidget(cancel_btn)
        footer.addWidget(self._apply_btn)
        root.addLayout(footer)

    # ── Initial state ────────────────────────────────────────────────────

    def _populate_profiles(self) -> None:
        self._profile_combo.blockSignals(True)
        try:
            self._profile_combo.clear()
            for p in self._store.all_profiles():
                self._profile_combo.addItem(p.name, p)
        finally:
            self._profile_combo.blockSignals(False)

    def _select_initial(self, current: PlotConfig | None) -> None:
        target = current.name if current else self._store.default_name
        idx = self._profile_combo.findText(target)
        if idx < 0:
            idx = 0
        self._profile_combo.setCurrentIndex(idx)
        self._on_profile_selected(idx)

    # ── Mode + profile selection ─────────────────────────────────────────

    def _on_mode_toggled(self, _checked: bool) -> None:
        is_manual = self._mode_manual_rb.isChecked()
        self._stack.setCurrentIndex(1 if is_manual else 0)
        self._editing_unsaved = True
        self._update_profile_buttons()
        self._refresh_test_status()
        self._update_apply_state()

    def _on_profile_selected(self, idx: int) -> None:
        if idx < 0:
            return
        profile = self._profile_combo.itemData(idx)
        if not isinstance(profile, PlotConfig):
            return

        # Load the selected profile into the form
        self._mode_group.blockSignals(True)
        try:
            if profile.mode == "manual":
                self._mode_manual_rb.setChecked(True)
                self._stack.setCurrentIndex(1)
            else:
                self._mode_auto_rb.setChecked(True)
                self._stack.setCurrentIndex(0)
        finally:
            self._mode_group.blockSignals(False)

        self._pattern_edit.blockSignals(True)
        try:
            self._pattern_edit.setPlainText(profile.pattern)
        finally:
            self._pattern_edit.blockSignals(False)

        self._refresh_recent_lines()
        self._editing_unsaved = False
        self._update_profile_buttons()
        self._refresh_test_status()
        self._update_apply_state()

    def _on_pattern_changed(self) -> None:
        self._editing_unsaved = True
        # Don't auto-test on every keystroke — too noisy. The user can
        # click Test or just click Apply (Apply implicitly tests).
        # But we DO update Apply state in case the pattern just became
        # valid/invalid.
        self._update_apply_state()

    def _update_profile_buttons(self) -> None:
        current = self._current_profile_or_none()
        is_builtin = current is not None and current.is_builtin
        self._rename_btn.setEnabled(not is_builtin and current is not None)
        self._delete_btn.setEnabled(not is_builtin and current is not None)

    def _current_profile_or_none(self) -> PlotConfig | None:
        data = self._profile_combo.currentData()
        return data if isinstance(data, PlotConfig) else None

    # ── Recent lines ─────────────────────────────────────────────────────

    def _refresh_recent_lines(self) -> None:
        self._recent_list.clear()
        try:
            lines = self._recent_lines_provider() or []
        except Exception:
            logger.exception("recent_lines_provider raised")
            lines = []
        if not lines:
            empty = QListWidgetItem(
                "(no recent lines yet \u2014 connect a device "
                "and wait for data)",
            )
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._recent_list.addItem(empty)
            return
        # Most recent on top
        for line in reversed(lines):
            self._recent_list.addItem(line)

    # ── Test pattern → tiered status ─────────────────────────────────────

    def _build_parser_or_none(self) -> tuple[RegexParser | None, str]:
        """Try to construct a RegexParser from the current pattern.

        Returns (parser, error_message). On success, error_message is
        empty. On failure, parser is None and error_message describes
        the issue (compile error, no named groups, empty pattern).
        """
        if self._mode_auto_rb.isChecked():
            return None, ""
        pattern = self._pattern_edit.toPlainText().strip()
        if not pattern:
            return None, "Pattern is empty."
        if len(pattern) > PROFILE_PATTERN_MAX:
            return None, f"Pattern exceeds {PROFILE_PATTERN_MAX} characters."
        try:
            return RegexParser(pattern), ""
        except RegexParserError as exc:
            return None, str(exc)

    def _refresh_test_status(self) -> None:
        """Compute and render the tiered Test result.

        Status tiers, in order of severity:
          - Neutral (auto mode): no test needed.
          - Red: pattern is structurally invalid (compile error,
            no named groups, empty). Apply disabled.
          - Amber: pattern is valid but didn't match any recent line.
            Apply enabled — engineer may know the firmware will emit
            matching data later.
          - Green: pattern matched ≥1 line and extracted numerics.
            Apply enabled.
        """
        if self._mode_auto_rb.isChecked():
            self._status_label.setText("")
            self._update_apply_state()
            return

        parser, err = self._build_parser_or_none()
        if parser is None:
            # RED — structurally broken
            self._status_label.setText(
                f"<span style='color:{_STATUS_RED};'>"
                f"\u2717 {self._html_escape(err)}</span>",
            )
            self._update_apply_state()
            return

        sample = []
        try:
            sample = self._recent_lines_provider() or []
        except Exception:
            logger.exception("recent_lines_provider raised in test")

        result = parser.test(sample)
        matched = result["matched"]
        total = result["total"]
        cols = result["columns"]
        preview = result["preview"]

        if total == 0:
            # Pattern compiles but no sample to test against
            self._status_label.setText(
                f"<span style='color:{_STATUS_NEUTRAL};'>"
                f"\u2022 Pattern is valid. No recent lines to test against; "
                f"connect a device to verify.</span>",
            )
            self._update_apply_state()
            return

        if matched == 0:
            # AMBER — valid pattern, no current matches
            self._status_label.setText(
                f"<span style='color:{_STATUS_AMBER};'>"
                f"\u26A0 Pattern is valid but didn't match any of the "
                f"last {total} lines. It will be applied to new data."
                f"</span>",
            )
            self._update_apply_state()
            return

        # GREEN
        first = preview[0] if preview else None
        first_str = ""
        if first is not None:
            _line, values = first
            extracted = ", ".join(f"{k}={v:g}" for k, v in values.items())
            first_str = (
                f"<br><span style='color:{_STATUS_NEUTRAL};'>"
                f"First match: <code>{self._html_escape(extracted)}</code>"
                f"</span>"
            )
        cols_str = ", ".join(cols)
        self._status_label.setText(
            f"<span style='color:{_STATUS_GREEN};'>"
            f"\u2713 Matched {matched}/{total} lines &middot; "
            f"columns: <b>{self._html_escape(cols_str)}</b>"
            f"</span>{first_str}",
        )
        self._update_apply_state()

    def _update_apply_state(self) -> None:
        """Apply is enabled iff the current configuration is structurally
        valid. Tier-amber configurations remain enabled — the user
        knows their format and the firmware may simply be quiet."""
        if self._mode_auto_rb.isChecked():
            self._apply_btn.setEnabled(True)
            return
        parser, _err = self._build_parser_or_none()
        self._apply_btn.setEnabled(parser is not None)

    @staticmethod
    def _html_escape(s: str) -> str:
        return (
            s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
        )

    # ── Profile actions ──────────────────────────────────────────────────

    def _on_save_as(self) -> None:
        """Save the current form as a new named profile.

        For manual mode, the pattern must be structurally valid
        (otherwise we'd be persisting a broken profile). For auto
        mode, any valid name works.
        """
        if self._mode_manual_rb.isChecked():
            parser, err = self._build_parser_or_none()
            if parser is None:
                QMessageBox.warning(
                    self,
                    "Cannot save profile",
                    f"The pattern is not valid:\n\n{err}\n\n"
                    "Fix the pattern before saving.",
                )
                return

        suggested = self._suggest_profile_name()
        name, ok = QInputDialog.getText(
            self, "Save Profile As",
            "Profile name:", text=suggested,
        )
        if not ok:
            return
        name = name.strip()
        if not PlotProfileStore.is_valid_profile_name(name):
            QMessageBox.warning(
                self, "Invalid name",
                f"Profile name must be 1\u2013{PROFILE_NAME_MAX} characters "
                "and contain no control characters.",
            )
            return
        if name == AUTO_PROFILE_NAME:
            QMessageBox.warning(
                self, "Reserved name",
                f"{AUTO_PROFILE_NAME!r} is the built-in profile name "
                "and cannot be reused. Pick a different name.",
            )
            return
        existing = self._store.get(name)
        if existing is not None:
            reply = QMessageBox.question(
                self, "Replace profile?",
                f"A profile named {name!r} already exists. Replace it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        try:
            if self._mode_auto_rb.isChecked():
                profile = PlotConfig.auto(name)
            else:
                pattern = self._pattern_edit.toPlainText().strip()
                profile = PlotConfig.manual(name, pattern)
            self._store.upsert(profile)
        except ValueError as exc:
            QMessageBox.warning(self, "Cannot save profile", str(exc))
            return

        self._populate_profiles()
        idx = self._profile_combo.findText(name)
        if idx >= 0:
            self._profile_combo.setCurrentIndex(idx)
        self._editing_unsaved = False

    def _on_rename(self) -> None:
        current = self._current_profile_or_none()
        if current is None or current.is_builtin:
            return
        new_name, ok = QInputDialog.getText(
            self, "Rename Profile",
            "New name:", text=current.name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if new_name == current.name:
            return
        if not self._store.rename(current.name, new_name):
            QMessageBox.warning(
                self, "Cannot rename",
                "The new name is invalid or already in use.",
            )
            return
        self._populate_profiles()
        idx = self._profile_combo.findText(new_name)
        if idx >= 0:
            self._profile_combo.setCurrentIndex(idx)

    def _on_delete(self) -> None:
        current = self._current_profile_or_none()
        if current is None or current.is_builtin:
            return
        reply = QMessageBox.question(
            self, "Delete profile?",
            f"Delete profile {current.name!r}?\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        if not self._store.delete(current.name):
            return
        self._populate_profiles()
        # Fall back to Auto-detect after delete
        idx = self._profile_combo.findText(AUTO_PROFILE_NAME)
        if idx >= 0:
            self._profile_combo.setCurrentIndex(idx)

    def _suggest_profile_name(self) -> str:
        """Suggest a unique default name for Save As."""
        base = "My Profile"
        if self._store.get(base) is None:
            return base
        i = 2
        while self._store.get(f"{base} {i}") is not None:
            i += 1
        return f"{base} {i}"

    # ── Capture assistant ────────────────────────────────────────────────

    def _on_capture_assistant(self) -> None:
        """Scaffold a regex from a sample line.

        Flow:
          1. User selects a sample line in the recent-lines list.
          2. Dialog opens showing that line; user double-clicks the
             numeric tokens they want to capture and names each.
          3. Dialog generates an escaped pattern with named groups and
             writes it into the pattern field.
        """
        item = self._recent_list.currentItem()
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsSelectable):
            QMessageBox.information(
                self, "Capture from sample",
                "First select a line in the recent-lines list, "
                "then click Capture from sample.",
            )
            return
        line = item.text()
        sub = _CaptureAssistantDialog(line, self)
        if sub.exec() == QDialog.DialogCode.Accepted:
            pattern = sub.generated_pattern()
            if pattern:
                self._pattern_edit.setPlainText(pattern)
                self._refresh_test_status()

    # ── Apply ────────────────────────────────────────────────────────────

    def _on_apply(self) -> None:
        if self._mode_auto_rb.isChecked():
            cfg = PlotConfig.auto()  # canonical Auto-detect
        else:
            parser, err = self._build_parser_or_none()
            if parser is None:
                # Defensive — Apply should be disabled in this branch
                QMessageBox.warning(self, "Cannot apply", err)
                return
            pattern = self._pattern_edit.toPlainText().strip()
            current = self._current_profile_or_none()
            # If the user is on a manual profile and hasn't modified
            # the pattern, keep the original profile name. Otherwise
            # use a "(unsaved)" sentinel — the engine doesn't care
            # about names but the host might.
            if (current is not None
                    and current.mode == "manual"
                    and current.pattern == pattern
                    and not self._editing_unsaved):
                cfg = current
            else:
                # Anonymous in-flight config; the user can Save As if
                # they want it persisted.
                cfg = PlotConfig.manual("(unsaved)", pattern)

        self.configChanged.emit(cfg)
        self.accept()


# ── Capture-from-sample sub-dialog ───────────────────────────────────────────

class _CaptureAssistantDialog(QDialog):
    """Visual regex scaffolder.

    Shows the sample line with numeric tokens highlighted as clickable
    chips. The user clicks each chip they want to capture and names
    it. On Accept, generates a regex of the form::

        prefix1(?P<name1>\\d+)between(?P<name2>[\\d.]+)suffix

    Surrounding non-numeric text is regex-escaped verbatim so the
    pattern matches the same shape of line.
    """

    def __init__(self, line: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Capture from sample")
        self.setMinimumSize(560, 360)
        self.setModal(True)

        self._line = line
        self._tokens = self._tokenize(line)
        # Per-token state: name (str) for selected, None for unselected
        self._selected_names: dict[int, str] = {}

        self._setup_ui()
        self._refresh_preview()

    @staticmethod
    def _tokenize(line: str) -> list[tuple[str, bool]]:
        """Split into (text, is_numeric) tokens.

        Numeric tokens are runs of digits, optional decimal point,
        optional sign, and optional scientific exponent. Everything
        else is preserved as literal interstitial text.
        """
        import re as _re
        # Numeric: optional sign, digits, optional decimal, optional exponent
        pattern = _re.compile(
            r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?",
        )
        out: list[tuple[str, bool]] = []
        last = 0
        for m in pattern.finditer(line):
            if m.start() > last:
                out.append((line[last:m.start()], False))
            out.append((line[m.start():m.end()], True))
            last = m.end()
        if last < len(line):
            out.append((line[last:], False))
        return out

    def _setup_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 12)
        root.setSpacing(10)

        intro = QLabel(
            "<b>Click a numeric value</b> to capture it, then give it a name. "
            "The pattern is built from your selections.",
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        # Source line, chip-styled
        # Background and border live in the theme QSS files
        # (midnight_dark.qss + studio_light.qss) keyed on the
        # objectName below. Qt's rich-text rendering does not
        # reliably honour palette(base) for background, so we
        # avoid palette() here and let the theme drive it.
        source_box = QLabel(self._render_source_html())
        source_box.setObjectName("captureSourceBox")
        source_box.setTextFormat(Qt.TextFormat.RichText)
        source_box.setWordWrap(True)
        source_box.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse,
        )
        source_box.linkActivated.connect(self._on_token_clicked)
        self._source_box = source_box
        root.addWidget(source_box)

        captured_label = QLabel("Captured values:")
        root.addWidget(captured_label)
        self._captured_view = QListWidget()
        self._captured_view.setMaximumHeight(120)
        root.addWidget(self._captured_view)

        preview_label = QLabel("Generated pattern:")
        root.addWidget(preview_label)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self._pattern_preview = QTextEdit()
        self._pattern_preview.setReadOnly(True)
        self._pattern_preview.setFont(mono)
        self._pattern_preview.setMaximumHeight(60)
        root.addWidget(self._pattern_preview)

        footer = QHBoxLayout()
        footer.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("clearBtn")
        cancel_btn.setFixedHeight(26)
        cancel_btn.clicked.connect(self.reject)
        self._use_btn = QPushButton("Use this pattern")
        self._use_btn.setFixedHeight(26)
        self._use_btn.setMinimumWidth(140)
        self._use_btn.setEnabled(False)
        self._use_btn.clicked.connect(self.accept)
        footer.addWidget(cancel_btn)
        footer.addWidget(self._use_btn)
        root.addLayout(footer)

    def _render_source_html(self) -> str:
        """Render the source line with numeric tokens as clickable links.

        Color contract:
            * Interstitial text (the literal parts between numeric tokens
              — labels like "RPM:", commas, spaces) has no inline color
              and inherits the QLabel's `color` property, which is set
              by the theme QSS rule on `QLabel#captureSourceBox`. That
              keeps it readable on both light and dark backgrounds.
            * Numeric chips have explicit accent colors (blue for
              selected, yellow for unselected) that are intentionally
              theme-independent — they are deliberate highlights and
              must stand out on both backgrounds. Black text on yellow
              and white text on blue are both AA-contrast safe.
        """
        parts: list[str] = []
        for i, (text, is_num) in enumerate(self._tokens):
            esc = (text.replace("&", "&amp;").replace("<", "&lt;")
                   .replace(">", "&gt;"))
            if is_num:
                if i in self._selected_names:
                    name = self._selected_names[i]
                    # Selected: blue chip with white text — deliberate
                    # theme-independent accent.
                    parts.append(
                        f"<a href='token:{i}' style='"
                        f"background:#1976D2;color:#fff;text-decoration:none;"
                        f"padding:2px 6px;border-radius:3px;margin:0 1px;'>"
                        f"{esc} \u27A4 {self._html_escape(name)}</a>",
                    )
                else:
                    # Unselected: yellow chip with black text — deliberate
                    # theme-independent accent (highlight on both themes).
                    parts.append(
                        f"<a href='token:{i}' style='"
                        f"background:#FFF59D;color:#000;text-decoration:none;"
                        f"padding:2px 6px;border-radius:3px;margin:0 1px;'>"
                        f"{esc}</a>",
                    )
            else:
                # Interstitial text — no inline color, inherits from
                # QLabel#captureSourceBox (theme-driven by QSS).
                parts.append(esc)
        return "<div style='font-family: monospace;'>" + "".join(parts) + "</div>"

    @staticmethod
    def _html_escape(s: str) -> str:
        return (
            s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
        )

    def _on_token_clicked(self, link: str) -> None:
        if not link.startswith("token:"):
            return
        try:
            idx = int(link.split(":", 1)[1])
        except ValueError:
            return
        if idx not in {i for i, (_t, is_num) in enumerate(self._tokens) if is_num}:
            return

        if idx in self._selected_names:
            # Toggle off
            del self._selected_names[idx]
        else:
            suggested = self._suggest_name(idx)
            name, ok = QInputDialog.getText(
                self, "Name this value",
                "Trace name (used as the (?P<name>\u2026) capture):",
                text=suggested,
            )
            if not ok:
                return
            name = name.strip()
            if not name:
                return
            if not name.isidentifier():
                QMessageBox.warning(
                    self, "Invalid name",
                    "Names must be valid Python identifiers "
                    "(letters, digits, underscores; cannot start with a digit).",
                )
                return
            if name in self._selected_names.values():
                QMessageBox.warning(
                    self, "Duplicate name",
                    f"The name {name!r} is already used. Pick a unique name.",
                )
                return
            self._selected_names[idx] = name

        self._source_box.setText(self._render_source_html())
        self._refresh_preview()

    def _suggest_name(self, idx: int) -> str:
        """Suggest a name based on the preceding non-numeric text.

        For "RPM: 1461" → suggest "RPM". Falls back to "value" if no
        prefix is identifier-like.
        """
        if idx == 0:
            return "value"
        prev = self._tokens[idx - 1][0]
        # Take the trailing identifier-ish chunk
        import re as _re
        m = _re.search(r"([A-Za-z_][A-Za-z0-9_]*)\W*$", prev)
        if m:
            candidate = m.group(1)
            # Avoid duplicates by appending a digit
            if candidate in self._selected_names.values():
                i = 2
                while f"{candidate}{i}" in self._selected_names.values():
                    i += 1
                return f"{candidate}{i}"
            return candidate
        return "value"

    def _refresh_preview(self) -> None:
        self._captured_view.clear()
        if not self._selected_names:
            self._pattern_preview.setPlainText("")
            self._use_btn.setEnabled(False)
            return

        # Captured values list
        for idx, name in sorted(self._selected_names.items()):
            value = self._tokens[idx][0]
            self._captured_view.addItem(f"{name} = {value}")

        # Generate pattern
        import re as _re
        parts: list[str] = []
        for i, (text, is_num) in enumerate(self._tokens):
            if is_num and i in self._selected_names:
                name = self._selected_names[i]
                # Numeric class — generous: signed, decimal, scientific
                parts.append(rf"(?P<{name}>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")
            else:
                # Literal text — escape special regex chars, but keep
                # whitespace flexible (replace runs of \s with \s+)
                escaped = _re.escape(text)
                # Make whitespace flexible so minor formatting changes
                # don't break the pattern
                escaped = _re.sub(r"(?:\\\s)+", r"\\s+", escaped)
                parts.append(escaped)

        pattern = "".join(parts)
        self._pattern_preview.setPlainText(pattern)
        self._use_btn.setEnabled(True)

    def generated_pattern(self) -> str:
        return self._pattern_preview.toPlainText()
