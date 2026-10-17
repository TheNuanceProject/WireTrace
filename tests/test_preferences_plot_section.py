# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests that the default plot profile chosen in Preferences is saved
and read back.

What is held to:
  1. Saving calls ``PlotProfileStore.set_default(name)``
  2. Loading reads ``store.default_name`` and selects it
  3. Save, close, reopen: the list shows the saved value
  4. The store's persistence through ConfigManager works

The dialog itself cannot run against the Qt stand-ins, so the state
transitions are tested at the data layer.
"""

from __future__ import annotations

from app.plot_config import AUTO_PROFILE_NAME, PlotConfig, PlotProfileStore


class FakeConfig:
    """Mirror of the FakeConfig in test_plot_config.py: fresh
    instance per test so each test is hermetic."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, str]] = {}

    def get(self, section: str, key: str, fallback: str | None = None) -> str:
        return self._data.get(section, {}).get(key, fallback or "")

    def set(self, section: str, key: str, value) -> None:
        self._data.setdefault(section, {})[key] = str(value)

    def save(self) -> bool:
        return True


class _MiniCombo:
    """Stand-in for QComboBox. Tracks items as (text, data) tuples
    and the current index. Mirrors the methods Preferences uses."""

    def __init__(self) -> None:
        self._items: list[tuple[str, object]] = []
        self._current = -1

    def clear(self) -> None:
        self._items = []
        self._current = -1

    def addItem(self, text: str, data: object = None) -> None:
        self._items.append((text, data if data is not None else text))
        if self._current < 0:
            self._current = 0

    def findText(self, text: str) -> int:
        for i, (t, _) in enumerate(self._items):
            if t == text:
                return i
        return -1

    def setCurrentIndex(self, i: int) -> None:
        if 0 <= i < len(self._items):
            self._current = i

    def currentText(self) -> str:
        if 0 <= self._current < len(self._items):
            return self._items[self._current][0]
        return ""

    def currentData(self) -> object:
        if 0 <= self._current < len(self._items):
            return self._items[self._current][1]
        return None

    def blockSignals(self, _: bool) -> None:
        pass


def _populate(combo: _MiniCombo, store: PlotProfileStore) -> None:
    """Mirror of PreferencesDialog._load_plot_profiles."""
    combo.blockSignals(True)
    try:
        combo.clear()
        for name in store.names():
            combo.addItem(name, name)
    finally:
        combo.blockSignals(False)


def _load_values(combo: _MiniCombo, store: PlotProfileStore) -> None:
    """Mirror of the Plot block in PreferencesDialog._load_values."""
    default_name = store.default_name
    pi = combo.findText(default_name)
    if pi >= 0:
        combo.setCurrentIndex(pi)


def _apply(combo: _MiniCombo, store: PlotProfileStore) -> None:
    """Mirror of the Plot block in PreferencesDialog._apply."""
    selected = combo.currentData()
    if selected:
        store.set_default(str(selected))


class TestPlotPreferencesRoundTrip:
    """Save, close and reopen: the value saved is the value shown."""

    def test_initial_default_is_auto_detect(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        combo = _MiniCombo()
        _populate(combo, store)
        _load_values(combo, store)
        assert combo.currentText() == AUTO_PROFILE_NAME

    def test_save_persists_selection(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor v3", r"RPM:\s*(?P<r>\d+)"))

        combo = _MiniCombo()
        _populate(combo, store)
        _load_values(combo, store)

        # User picks Motor v3 in the dropdown
        idx = combo.findText("Motor v3")
        assert idx >= 0
        combo.setCurrentIndex(idx)
        assert combo.currentText() == "Motor v3"

        # User clicks Save
        _apply(combo, store)

        # Store's default is now Motor v3
        assert store.default_name == "Motor v3"

    def test_save_then_reopen_shows_saved_default(self):
        """Reopening Preferences after Save shows the profile that was
        chosen, not Auto-detect: _load_values reads it from the store."""
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor v3", r"RPM:\s*(?P<r>\d+)"))

        # First dialog session: user selects Motor v3 and saves
        combo1 = _MiniCombo()
        _populate(combo1, store)
        _load_values(combo1, store)
        combo1.setCurrentIndex(combo1.findText("Motor v3"))
        _apply(combo1, store)

        # Second dialog session: simulates reopen. A NEW store is
        # constructed from the same config, then a NEW combo is
        # populated and _load_values runs.
        store2 = PlotProfileStore(cfg)
        combo2 = _MiniCombo()
        _populate(combo2, store2)
        _load_values(combo2, store2)

        assert combo2.currentText() == "Motor v3", \
            "default profile didn't survive Preferences close+reopen"

    def test_default_applies_to_new_tabs(self):
        """The whole point of the default profile: new tabs should
        boot with whatever the user set. PlotProfileStore.default_profile()
        is what DeviceTab.__init__ reads."""
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor v3", r"RPM:\s*(?P<r>\d+)"))

        combo = _MiniCombo()
        _populate(combo, store)
        combo.setCurrentIndex(combo.findText("Motor v3"))
        _apply(combo, store)

        # Simulate app restart: new ConfigManager-backed store
        store2 = PlotProfileStore(cfg)
        # DeviceTab.__init__ does: self._plot_profile_store.default_profile()
        applied = store2.default_profile()
        assert applied.name == "Motor v3"
        assert applied.mode == "manual"
        assert applied.pattern == r"RPM:\s*(?P<r>\d+)"

    def test_default_deletion_falls_back_gracefully(self):
        """If the previously-default profile was deleted, the dialog
        must show Auto-detect on reopen, not crash or show a phantom."""
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor v3", r"RPM:\s*(?P<r>\d+)"))
        store.set_default("Motor v3")
        assert store.default_name == "Motor v3"

        # Delete the profile (e.g. via the Configure plot dialog)
        store.delete("Motor v3")
        # Now default falls back to Auto-detect: store contract
        assert store.default_name == AUTO_PROFILE_NAME

        # Preferences reopen: combo should select Auto-detect
        combo = _MiniCombo()
        _populate(combo, store)
        _load_values(combo, store)
        assert combo.currentText() == AUTO_PROFILE_NAME

    def test_empty_selection_does_not_overwrite(self):
        """Defensive: if the combo somehow has no currentData (empty
        store), _apply must not blow away the existing default."""
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor v3", r"RPM:\s*(?P<r>\d+)"))
        store.set_default("Motor v3")

        combo = _MiniCombo()
        # Don't populate: combo is empty, currentData() returns None
        _apply(combo, store)

        assert store.default_name == "Motor v3"
