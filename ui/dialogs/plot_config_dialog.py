# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace Configure plot dialog.

User-facing surface for the manual plotting configuration system.
Lets the user:

  1. Choose auto mode (the default: let WireTrace detect the format)
     or manual mode (declare a regex with named groups).
  2. Save / load / rename / delete named profiles for reuse across
     sessions and devices. Profiles persist via PlotProfileStore.
  3. Test their pattern against recent lines from the active tab,
     with a tiered visual result (green / amber / red).
  4. Scaffold a regex from a sample line by clicking the values they
     want to plot: the "Capture from sample" assistant.

Design notes:
  - The dialog is a passive data structure: it owns no engine and
    triggers no I/O. It receives a ``recent_lines_provider`` callable
    that the host (DeviceTab) wires to the live engine.
  - The ``configChanged`` signal is the dialog's only output: the
    host listens, applies the new ``PlotConfig`` to its engine, and
    closes the loop.
  - Apply is enabled whenever the configuration is structurally
    valid:
      * Auto mode: always valid
      * Manual mode: green (matches recent lines) or amber (compiles
        and has named groups but did not match, because the firmware
        happens to be quiet)
    Apply is disabled only when the pattern is structurally broken
    (red): there's no path where applying it could work.
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QRadioButton,
    QSizePolicy,
    QStackedLayout,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.constants import PLOT_REGEX_TIMEOUT_SECONDS
from app.plot_config import (
    AUTO_PROFILE_NAME,
    PROFILE_NAME_MAX,
    PROFILE_PATTERN_MAX,
    PlotConfig,
    PlotProfileStore,
)
from core.plot_parsers import RegexParser, RegexParserError
from ui.dialogs.common import action_button, button_row, dialog_layout, heading, note, rule, tighten
from ui.dialogs.messages import ask_text, confirm, tell
from ui.themes import tokens
from ui.themes.fonts import monospace_font
from ui.widgets.dot_grid import DotGridDialog
from ui.widgets.least_size import keep_least_size
from ui.widgets.styling import emphasis

logger = logging.getLogger(__name__)


_DEFAULT_PROFILE_NAME = "My Profile"
_UNSAVED_PROFILE_NAME = "(unsaved)"

# ── Dialog ───────────────────────────────────────────────────────────────

class PlotConfigDialog(DotGridDialog):
    """Modal dialog for configuring a tab's plot mode and profile.

    Args:
        store: The PlotProfileStore that owns the persisted profiles.
        recent_lines_provider: callable returning the live engine's
            recent lines (most recent last). May return [].
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
        self.setWindowTitle("Configure plot")
        keep_least_size(self, tokens.METRICS.dialog_medium_width, tokens.METRICS.dialog_tall_height)
        self.setModal(True)

        self._store = store
        self._recent_lines_provider = recent_lines_provider
        self._editing_unsaved = False  # form has changes vs selected profile
        # True when the most recent Test timed out (catastrophic
        # backtracking). Gates Apply off until the pattern is changed or
        # re-tested clean. Reset on every pattern edit and mode toggle.
        self._last_test_timed_out = False

        self._setup_ui()
        self._populate_profiles()
        self._select_initial(current_config)
        self._refresh_test_status()

    # ── Setup ────────────────────────────────────────────────────────────

    def _setup_ui(self) -> None:
        metrics = tokens.METRICS
        root = dialog_layout(self)

        # ── Mode toggle ────────────────────────────────────────────
        mode_row = QHBoxLayout()
        mode_row.setSpacing(metrics.space_4)
        mode_label = heading("Mode")
        mode_label.setFixedWidth(metrics.short_label_width)
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
        profile_row.setSpacing(metrics.space_4)
        profile_label = heading("Profile")
        profile_label.setFixedWidth(metrics.short_label_width)

        self._profile_combo = QComboBox()
        self._profile_combo.setMinimumWidth(metrics.form_field_width)
        self._profile_combo.currentIndexChanged.connect(self._on_profile_selected)

        self._save_as_btn = action_button("Save as...")
        self._save_as_btn.clicked.connect(self._on_save_as)

        self._rename_btn = action_button("Rename")
        self._rename_btn.clicked.connect(self._on_rename)

        self._delete_btn = action_button("Delete")
        self._delete_btn.clicked.connect(self._on_delete)

        profile_actions = QHBoxLayout()
        tighten(profile_actions)
        profile_actions.addWidget(self._profile_combo, 1)
        profile_actions.addWidget(self._save_as_btn)
        profile_actions.addWidget(self._rename_btn)
        profile_actions.addWidget(self._delete_btn)
        profile_row.addWidget(profile_label)
        profile_row.addLayout(profile_actions, 1)
        root.addLayout(profile_row)

        root.addWidget(rule())

        # ── Manual section (stacked: hidden in auto mode) ─────────
        # We use a stacked layout rather than show/hide so the dialog
        # stays the same size in both modes: no flicker.
        self._manual_panel = QWidget()
        manual_layout = QVBoxLayout(self._manual_panel)
        tighten(manual_layout)

        # Pattern editor
        pattern_label = heading("Pattern")
        self._pattern_edit = QPlainTextEdit()
        self._pattern_edit.setObjectName("plotConfigPatternEdit")
        self._pattern_edit.setPlaceholderText(
            r"e.g.  RPM:\s*(?P<RPM>\d+),\s*Voltage:\s*(?P<Voltage>[\d.]+)",
        )
        self._pattern_edit.setMaximumHeight(metrics.notes_height)
        mono = monospace_font(tokens.TYPOGRAPHY.console_family)
        self._pattern_edit.setFont(mono)
        self._pattern_edit.textChanged.connect(self._on_pattern_changed)

        pattern_hint = note(
            "Each <code>(?P&lt;name&gt;...)</code> named group becomes a "
            "trace named <i>name</i>.",
        )
        pattern_hint.setTextFormat(Qt.TextFormat.RichText)

        manual_layout.addWidget(pattern_label)
        manual_layout.addWidget(self._pattern_edit)
        manual_layout.addWidget(pattern_hint)

        # Recent lines panel
        self._refresh_lines_btn = action_button("Refresh")
        self._refresh_lines_btn.clicked.connect(self._refresh_recent_lines)
        manual_layout.addLayout(button_row(
            self._refresh_lines_btn, leading=heading("Recent lines from this session"),
        ))

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
        self._capture_btn = action_button("Capture from sample...")
        self._capture_btn.setToolTip(
            "Pick a sample line, then name each numeric value to capture; "
            "the dialog scaffolds the regex for you.",
        )
        self._capture_btn.clicked.connect(self._on_capture_assistant)

        self._test_btn = action_button("Test pattern")
        self._test_btn.clicked.connect(self._refresh_test_status)
        manual_layout.addLayout(button_row(self._test_btn, leading=self._capture_btn))

        # Test status line: coloured tier indicator
        self._status_label = QLabel("")
        self._status_label.setObjectName("plotConfigStatus")
        self._status_label.setTextFormat(Qt.TextFormat.RichText)
        self._status_label.setWordWrap(True)
        self._status_label.setMinimumHeight(metrics.header_height)
        manual_layout.addWidget(self._status_label)

        # ── Auto-mode placeholder (shown when mode = auto) ────────
        self._auto_panel = QWidget()
        auto_layout = QVBoxLayout(self._auto_panel)
        tighten(auto_layout)
        self._auto_blurb = note(
            f"{emphasis('Auto-detect')} scans incoming serial data and identifies "
            "JSON, key:value, or delimited formats automatically.<br><br>"
            "If your firmware uses a custom format, switch to "
            f"{emphasis('Manual')} and declare a regex.",
        )
        self._auto_blurb.setTextFormat(Qt.TextFormat.RichText)
        self._auto_blurb.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
        )
        auto_layout.addWidget(self._auto_blurb)
        auto_layout.addStretch()

        # Stack: index 0 = auto, 1 = manual
        self._stack = QStackedLayout()
        self._stack.setContentsMargins(0, 0, 0, 0)
        self._stack.addWidget(self._auto_panel)
        self._stack.addWidget(self._manual_panel)
        stack_holder = QWidget()
        stack_holder.setLayout(self._stack)
        root.addWidget(stack_holder, 1)

        # ── Footer ─────────────────────────────────────────────────
        cancel_btn = action_button("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self._apply_btn = action_button("Apply", primary=True)
        self._apply_btn.setDefault(True)
        self._apply_btn.clicked.connect(self._on_apply)
        root.addLayout(button_row(cancel_btn, self._apply_btn))

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
        # The pattern changed, so any prior timeout verdict is stale.
        self._last_test_timed_out = False
        # Don't auto-test on every keystroke: too noisy. The user can
        # click Test or just click Apply (Apply implicitly tests).
        # The Apply state is still updated, in case the pattern just became
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
                "No recent lines yet. Connect a device and wait for data.",
            )
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._recent_list.addItem(empty)
            return
        # Most recent on top
        for line in reversed(lines):
            self._recent_list.addItem(line)

    # ── Test pattern and its status ─────────────────────────────────────

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
            Apply enabled: engineer may know the firmware will emit
            matching data later.
          - Green: pattern matched at least one line and extracted numerics.
            Apply enabled.
        """
        if self._mode_auto_rb.isChecked():
            self._status_label.setText("")
            self._update_apply_state()
            return

        parser, err = self._build_parser_or_none()
        if parser is None:
            # Red: structurally broken
            self._status_label.setText(
                f"<span style='color:{tokens.palette().error};'>"
                f"{html.escape(err)}</span>",
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
        timed_out = result["timed_out"]

        # Recompute the timeout verdict on every test.
        self._last_test_timed_out = False

        if timed_out:
            # Red: catastrophic backtracking. Apply is disabled until the
            # pattern is changed; running it on live data would freeze the
            # parsing thread line after line.
            self._last_test_timed_out = True
            self._status_label.setText(
                f"<span style='color:{tokens.palette().error};'>"
                f"Pattern timed out after "
                f"{PLOT_REGEX_TIMEOUT_SECONDS:g} s on {timed_out} of {total} "
                f"lines. It may backtrack without end. "
                f"Try simplifying it.</span>",
            )
            self._update_apply_state()
            return

        if total == 0:
            # Pattern compiles but no sample to test against
            self._status_label.setText(
                f"<span style='color:{tokens.palette().text_secondary};'>"
                f"Pattern is valid. No recent lines to test against; "
                f"connect a device to verify.</span>",
            )
            self._update_apply_state()
            return

        if matched == 0:
            # Amber: valid pattern, no current matches
            self._status_label.setText(
                f"<span style='color:{tokens.palette().warning};'>"
                f"Pattern is valid but didn't match any of the "
                f"last {total} lines. It will be applied to new data."
                f"</span>",
            )
            self._update_apply_state()
            return

        # Green
        first = preview[0] if preview else None
        first_str = ""
        if first is not None:
            _line, values = first
            extracted = ", ".join(f"{k}={v:g}" for k, v in values.items())
            first_str = (
                f"<br><span style='color:{tokens.palette().text_secondary};'>"
                f"First match: <code>{html.escape(extracted)}</code>"
                f"</span>"
            )
        cols_str = ", ".join(cols)
        self._status_label.setText(
            f"<span style='color:{tokens.palette().success};'>"
            f"Matched {matched} of {total} lines &middot; "
            f"columns: {emphasis(html.escape(cols_str))}"
            f"</span>{first_str}",
        )
        self._update_apply_state()

    def _update_apply_state(self) -> None:
        """Apply is enabled iff the current configuration is structurally
        valid. Tier-amber configurations remain enabled: the user
        knows their format and the firmware may simply be quiet."""
        if self._mode_auto_rb.isChecked():
            self._apply_btn.setEnabled(True)
            return
        parser, _err = self._build_parser_or_none()
        self._apply_btn.setEnabled(parser is not None and not self._last_test_timed_out)

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
                tell(
                    self,
                    "Cannot save profile",
                    f"The pattern is not valid:\n\n{err}\n\n"
                    "Fix the pattern before saving.",
                )
                return

        suggested = self._suggest_profile_name()
        name = ask_text(self, "Save profile as", "Profile name", suggested)
        if name is None:
            return
        name = name.strip()
        if not PlotProfileStore.is_valid_profile_name(name):
            tell(
                self, "Invalid name",
                f"Profile name must be 1 to {PROFILE_NAME_MAX} characters "
                "and contain no control characters.",
            )
            return
        if name == AUTO_PROFILE_NAME:
            tell(
                self, "Reserved name",
                f"{AUTO_PROFILE_NAME!r} is the built-in profile name "
                "and cannot be reused. Pick a different name.",
            )
            return
        if self._store.get(name) is not None and not confirm(
            self, "Replace profile",
            f"A profile named {name!r} already exists. Replace it?",
            proceed="Replace",
        ):
            return

        try:
            if self._mode_auto_rb.isChecked():
                profile = PlotConfig.auto(name)
            else:
                pattern = self._pattern_edit.toPlainText().strip()
                profile = PlotConfig.manual(name, pattern)
            self._store.upsert(profile)
        except ValueError as exc:
            tell(self, "Cannot save profile", str(exc))
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
        new_name = ask_text(self, "Rename profile", "New name", current.name)
        if new_name is None:
            return
        new_name = new_name.strip()
        if new_name == current.name:
            return
        if not self._store.rename(current.name, new_name):
            tell(
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
        if not confirm(
            self, "Delete profile",
            f"Delete profile {current.name!r}?",
            proceed="Delete",
            detail="This cannot be undone.",
        ):
            return
        if not self._store.delete(current.name):
            return
        self._populate_profiles()
        # Fall back to Auto-detect after delete
        idx = self._profile_combo.findText(AUTO_PROFILE_NAME)
        if idx >= 0:
            self._profile_combo.setCurrentIndex(idx)

    def _suggest_profile_name(self) -> str:
        """Suggest a unique default name for Save as."""
        base = _DEFAULT_PROFILE_NAME
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
          2. Dialog opens showing that line; user clicks the
             numeric tokens they want to capture and names each.
          3. Dialog generates an escaped pattern with named groups and
             writes it into the pattern field.
        """
        item = self._recent_list.currentItem()
        if item is None or not (item.flags() & Qt.ItemFlag.ItemIsSelectable):
            tell(
                self, "Capture from sample",
                "First select a line in the recent-lines list, "
                "then click Capture from sample.",
                problem=False,
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
                # Defensive: Apply should be disabled in this branch
                tell(self, "Cannot apply", err)
                return
            pattern = self._pattern_edit.toPlainText().strip()
            current = self._current_profile_or_none()
            # If the user is on a manual profile and hasn't modified
            # the pattern, keep the original profile name. Otherwise
            # use the name "(unsaved)": the engine ignores names, and
            # the host shows it in its message.
            if (current is not None
                    and current.mode == "manual"
                    and current.pattern == pattern
                    and not self._editing_unsaved):
                cfg = current
            else:
                # Anonymous in-flight config; the user can Save as if
                # they want it persisted.
                cfg = PlotConfig.manual(_UNSAVED_PROFILE_NAME, pattern)

        self.configChanged.emit(cfg)
        self.accept()


# ── Capture-from-sample sub-dialog ───────────────────────────────────────────

class _CaptureAssistantDialog(DotGridDialog):
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
        keep_least_size(
            self, tokens.METRICS.dialog_medium_width, tokens.METRICS.dialog_short_height,
        )
        self.setModal(True)

        self._line = line
        self._tokens = self._tokenize(line)
        # The name given to each selected token, by token index.
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
        # Numeric: optional sign, digits, optional decimal, optional exponent
        pattern = re.compile(
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
        metrics = tokens.METRICS
        root = dialog_layout(self)

        intro = QLabel(
            f"{emphasis('Click a numeric value')} to capture it, then give it a name. "
            "The pattern is built from your selections.",
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        root.addWidget(intro)

        # Source line, chip-styled
        # Background and border come from the stylesheet template,
        # keyed on the objectName below. Qt's rich-text rendering does not
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

        root.addWidget(heading("Captured values"))
        self._captured_view = QListWidget()
        self._captured_view.setMaximumHeight(metrics.sample_height)
        root.addWidget(self._captured_view)

        root.addWidget(heading("Generated pattern"))
        mono = monospace_font(tokens.TYPOGRAPHY.console_family)
        self._pattern_preview = QTextEdit()
        self._pattern_preview.setReadOnly(True)
        self._pattern_preview.setFont(mono)
        self._pattern_preview.setMaximumHeight(metrics.notes_height)
        root.addWidget(self._pattern_preview)

        cancel_btn = action_button("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self._use_btn = action_button("Use this pattern", primary=True)
        self._use_btn.setEnabled(False)
        self._use_btn.clicked.connect(self.accept)
        root.addLayout(button_row(cancel_btn, self._use_btn))

    def _render_source_html(self) -> str:
        """Render the source line with numeric tokens as clickable links.

        Colours come from the active theme. Text between the numeric
        tokens carries no colour of its own and inherits the label's.
        A selected value is drawn like a primary button; an unselected
        one is highlighted like a search hit.
        """
        palette = tokens.palette()
        metrics = tokens.METRICS
        chip = (
            f"text-decoration:none;padding:{metrics.space_half}px {metrics.space_2}px;"
            f"border-radius:{metrics.radius_sm}px;margin:0 {metrics.border}px;"
        )
        selected = f"background:{palette.text};color:{palette.paper};{chip}"
        unselected = f"background:{palette.search_hit};color:{palette.text};{chip}"
        parts: list[str] = []
        for i, (text, is_num) in enumerate(self._tokens):
            esc = html.escape(text)
            if is_num:
                if i in self._selected_names:
                    name = self._selected_names[i]
                    parts.append(
                        f"<a href='token:{i}' style='{selected}'>"
                        f"{esc} as {html.escape(name)}</a>",
                    )
                else:
                    parts.append(
                        f"<a href='token:{i}' style='{unselected}'>{esc}</a>",
                    )
            else:
                # Text between values: inherits the label's colour.
                parts.append(esc)
        family = tokens.TYPOGRAPHY.console_family
        return f"<div style='font-family: \"{family}\", monospace;'>" + "".join(parts) + "</div>"

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
            name = ask_text(
                self, "Name this value",
                "Trace name, used as the (?P<name>...) capture",
                suggested,
            )
            if name is None:
                return
            name = name.strip()
            if not name:
                return
            if not name.isidentifier():
                tell(
                    self, "Invalid name",
                    "Names must be valid Python identifiers "
                    "(letters, digits, underscores; cannot start with a digit).",
                )
                return
            if name in self._selected_names.values():
                tell(
                    self, "Duplicate name",
                    f"The name {name!r} is already used. Pick a unique name.",
                )
                return
            self._selected_names[idx] = name

        self._source_box.setText(self._render_source_html())
        self._refresh_preview()

    def _suggest_name(self, idx: int) -> str:
        """Suggest a name based on the preceding non-numeric text.

        For "RPM: 1461" the suggestion is "RPM". Falls back to "value" if no
        prefix is identifier-like.
        """
        if idx == 0:
            return "value"
        prev = self._tokens[idx - 1][0]
        # Take the trailing identifier-ish chunk
        m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\W*$", prev)
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
        parts: list[str] = []
        for i, (text, is_num) in enumerate(self._tokens):
            if is_num and i in self._selected_names:
                name = self._selected_names[i]
                # A generous numeric class: signed, decimal, scientific.
                parts.append(rf"(?P<{name}>-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")
            else:
                # Literal text is escaped; runs of whitespace become \s+
                # so small formatting changes do not break the pattern.
                escaped = re.escape(text)
                escaped = re.sub(r"(?:\\\s)+", r"\\s+", escaped)
                parts.append(escaped)

        pattern = "".join(parts)
        self._pattern_preview.setPlainText(pattern)
        self._use_btn.setEnabled(True)

    def generated_pattern(self) -> str:
        return self._pattern_preview.toPlainText()
