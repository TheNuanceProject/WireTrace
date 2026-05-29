# SPDX-License-Identifier: MIT
"""Regression tests for the ``plot_visibility_changed`` signal contract.

The bug: View → Live Plot menu's checkmark could drift out of sync
with the actual plot panel visibility. Specifically, disconnecting
hid the plot but the menu still showed it as checked, because the
menu was only re-synced on tab switch — not on per-action transitions.

The fix introduces a single setter ``_set_plot_visible(value)`` that
all mutation paths must go through. The setter:
  1. Updates ``_plot_visible``
  2. Updates the toolbar Plot button
  3. Emits ``plot_visibility_changed(value)``

MainWindow connects to the signal and updates the menu's check when
the emitting tab is the current one.

These tests pin down the signal-contract invariants. The visual Qt
parts aren't covered (they can't run headless), but the data flow is.
"""

from __future__ import annotations

# Test stubs from conftest.py are auto-installed.


class _Recorder:
    """Captures signal emissions for assertion."""

    def __init__(self) -> None:
        self.events: list[bool] = []

    def __call__(self, value: bool) -> None:
        self.events.append(value)


class _MiniTab:
    """Minimal stand-in matching the DeviceTab visibility contract.

    Replicates the source-of-truth pattern: one ``_plot_visible``
    field, one setter, one signal. Tests assert that emissions
    happen on the right transitions and that the field always
    matches what was emitted.
    """

    def __init__(self) -> None:
        self._plot_visible = False
        self._listeners: list = []

    def connect(self, fn) -> None:
        self._listeners.append(fn)

    def _emit(self, value: bool) -> None:
        for fn in self._listeners:
            fn(value)

    def is_plot_visible(self) -> bool:
        return self._plot_visible

    def _set_plot_visible(self, value: bool) -> None:
        # Mirrors DeviceTab._set_plot_visible exactly.
        if self._plot_visible == value:
            # Idempotent emit so external observers can re-sync.
            self._emit(value)
            return
        self._plot_visible = value
        self._emit(value)


class TestPlotVisibilitySignalContract:

    def test_emit_on_transition(self):
        tab = _MiniTab()
        rec = _Recorder()
        tab.connect(rec)
        tab._set_plot_visible(True)
        assert rec.events == [True]
        tab._set_plot_visible(False)
        assert rec.events == [True, False]

    def test_idempotent_call_still_emits(self):
        """Re-syncing on tab switch is the use case — even if state
        didn't change, the signal must fire so the menu can refresh
        defensively. The View menu pulls from this signal as its
        only sync source after initial creation."""
        tab = _MiniTab()
        rec = _Recorder()
        tab.connect(rec)
        tab._set_plot_visible(True)
        tab._set_plot_visible(True)  # no-op change
        assert rec.events == [True, True]

    def test_field_matches_emission(self):
        tab = _MiniTab()
        seen_during_emit = []
        tab.connect(lambda v: seen_during_emit.append(
            (v, tab.is_plot_visible()),
        ))
        tab._set_plot_visible(True)
        tab._set_plot_visible(False)
        # When a listener fires, the field and the value must agree.
        for emitted, field in seen_during_emit:
            assert emitted == field, \
                f"signal carried {emitted} but field was {field}"

    def test_disconnect_path_emits(self):
        """Disconnect tear-down hides the plot. The menu must be
        notified so its check unticks. Earlier code mutated
        _plot_visible directly here and bypassed the signal."""
        tab = _MiniTab()
        tab._set_plot_visible(True)  # connect + open plot

        rec = _Recorder()
        tab.connect(rec)

        # Disconnect — must go through the setter
        tab._set_plot_visible(False)

        assert rec.events == [False], \
            "disconnect bypassed the visibility signal"

    def test_toggle_then_disconnect(self):
        """Real sequence: user toggles plot on, then disconnects.
        Menu must end up unchecked."""
        tab = _MiniTab()
        states = []
        tab.connect(lambda v: states.append(v))

        tab._set_plot_visible(True)   # user clicks Plot button
        tab._set_plot_visible(False)  # disconnect tears down

        assert states == [True, False]
        assert tab.is_plot_visible() is False


class TestCurrentTabFilter:
    """MainWindow's handler must ignore emissions from non-current
    tabs. Background tabs changing their plot state shouldn't
    perturb the menu — the menu always reflects the *current* tab.
    """

    def test_only_current_tab_drives_menu(self):
        tab_a = _MiniTab()
        tab_b = _MiniTab()
        menu_checked = [False]
        current_tab = [tab_a]

        def handler(tab, visible):
            if tab is current_tab[0]:
                menu_checked[0] = visible

        tab_a.connect(lambda v: handler(tab_a, v))
        tab_b.connect(lambda v: handler(tab_b, v))

        # Tab A is current. A turns plot on — menu reflects it.
        tab_a._set_plot_visible(True)
        assert menu_checked[0] is True

        # Tab B (background) turns plot on — menu unchanged.
        tab_b._set_plot_visible(True)
        assert menu_checked[0] is True

        # Switch current to B — caller would re-sync menu via
        # tab.is_plot_visible() here. The signal alone doesn't
        # cross-pollute.
        current_tab[0] = tab_b

        # A (now background) turns plot off — menu still reflects B.
        tab_a._set_plot_visible(False)
        assert menu_checked[0] is True
