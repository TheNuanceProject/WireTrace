# SPDX-License-Identifier: MIT
"""Tests for ``app.plot_config`` — PlotConfig + PlotProfileStore.

Covers:
  - PlotConfig dataclass validation
  - Profile store load/save round-trip via a fake ConfigManager
  - Auto-detect profile is always present and protected
  - Corruption recovery (malformed JSON, missing keys, bad entries)
  - Default fallback when the saved default no longer exists
  - Rename / delete / set_default semantics
"""

from __future__ import annotations

import pytest

from app.plot_config import (
    AUTO_PROFILE_NAME,
    PROFILE_NAME_MAX,
    PROFILE_PATTERN_MAX,
    PlotConfig,
    PlotProfileStore,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fake ConfigManager — INI-shaped key-value store, no disk I/O
# ─────────────────────────────────────────────────────────────────────────────

class FakeConfig:
    """Stand-in for ConfigManager. Keeps an in-memory section/key store."""

    def __init__(self, initial: dict[str, dict[str, str]] | None = None) -> None:
        self._data: dict[str, dict[str, str]] = {}
        if initial:
            for section, kvs in initial.items():
                self._data[section] = dict(kvs)
        self.save_count = 0

    def get(self, section: str, key: str, fallback: str | None = None) -> str:
        return self._data.get(section, {}).get(key, fallback or "")

    def set(self, section: str, key: str, value) -> None:
        self._data.setdefault(section, {})[key] = str(value)

    def save(self) -> bool:
        self.save_count += 1
        return True


# ─────────────────────────────────────────────────────────────────────────────
# PlotConfig validation
# ─────────────────────────────────────────────────────────────────────────────

class TestPlotConfigValidation:
    def test_auto_minimal(self):
        c = PlotConfig.auto()
        assert c.name == AUTO_PROFILE_NAME
        assert c.mode == "auto"
        assert c.pattern == ""

    def test_auto_custom_name(self):
        c = PlotConfig.auto("My Auto")
        assert c.name == "My Auto"
        assert c.mode == "auto"

    def test_manual_requires_pattern(self):
        with pytest.raises(ValueError, match="non-empty pattern"):
            PlotConfig(name="bad", mode="manual", pattern="")
        with pytest.raises(ValueError, match="non-empty pattern"):
            PlotConfig(name="bad", mode="manual", pattern="   ")

    def test_manual_with_pattern(self):
        c = PlotConfig.manual("Motor", r"RPM:\s*(?P<RPM>\d+)")
        assert c.mode == "manual"
        assert c.pattern == r"RPM:\s*(?P<RPM>\d+)"

    def test_invalid_mode(self):
        with pytest.raises(ValueError, match="must be 'auto' or 'manual'"):
            PlotConfig(name="bad", mode="silly")

    def test_empty_name(self):
        with pytest.raises(ValueError, match="name cannot be empty"):
            PlotConfig(name="", mode="auto")
        with pytest.raises(ValueError, match="name cannot be empty"):
            PlotConfig(name="   ", mode="auto")

    def test_name_length_cap(self):
        with pytest.raises(ValueError, match="exceeds"):
            PlotConfig(name="x" * (PROFILE_NAME_MAX + 1), mode="auto")

    def test_pattern_length_cap(self):
        with pytest.raises(ValueError, match="exceeds"):
            PlotConfig(
                name="huge", mode="manual",
                pattern="a" * (PROFILE_PATTERN_MAX + 1),
            )

    def test_is_builtin(self):
        assert PlotConfig.auto().is_builtin
        assert not PlotConfig.auto("Not Auto-detect").is_builtin
        assert not PlotConfig.manual("Custom", r"(?P<x>\d+)").is_builtin

    def test_round_trip_dict(self):
        c1 = PlotConfig.manual("Motor v3", r"RPM:\s*(?P<RPM>\d+)")
        d = c1.to_dict()
        c2 = PlotConfig.from_dict(d)
        assert c1 == c2

    def test_to_dict_omits_pattern_for_auto(self):
        d = PlotConfig.auto().to_dict()
        assert "pattern" not in d
        assert d == {"name": AUTO_PROFILE_NAME, "mode": "auto"}


# ─────────────────────────────────────────────────────────────────────────────
# PlotProfileStore — initial state
# ─────────────────────────────────────────────────────────────────────────────

class TestProfileStoreInitial:
    def test_empty_config_yields_auto_only(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        assert store.names() == [AUTO_PROFILE_NAME]
        assert store.default_name == AUTO_PROFILE_NAME

    def test_loads_existing_profiles(self):
        cfg = FakeConfig({
            "Plot": {
                "profiles_json": '[{"name":"Auto-detect","mode":"auto"},'
                                 '{"name":"Motor","mode":"manual",'
                                 '"pattern":"RPM:(?P<r>\\\\d+)"}]',
                "default_profile": "Motor",
            },
        })
        store = PlotProfileStore(cfg)
        assert store.names() == [AUTO_PROFILE_NAME, "Motor"]
        assert store.default_name == "Motor"
        motor = store.get("Motor")
        assert motor is not None
        assert motor.mode == "manual"

    def test_auto_inserted_if_missing(self):
        """Even a corrupted save that lost Auto-detect must come back
        with it present, in the first position."""
        cfg = FakeConfig({
            "Plot": {
                "profiles_json": '[{"name":"Motor","mode":"manual",'
                                 '"pattern":"RPM:(?P<r>\\\\d+)"}]',
            },
        })
        store = PlotProfileStore(cfg)
        names = store.names()
        assert names[0] == AUTO_PROFILE_NAME
        assert "Motor" in names

    def test_auto_forced_to_auto_mode(self):
        """If an attacker (or bug) wrote Auto-detect as manual, the
        store quietly restores its auto contract."""
        cfg = FakeConfig({
            "Plot": {
                "profiles_json": '[{"name":"Auto-detect","mode":"manual",'
                                 '"pattern":"hax"}]',
            },
        })
        store = PlotProfileStore(cfg)
        auto = store.get(AUTO_PROFILE_NAME)
        assert auto is not None
        assert auto.mode == "auto"
        assert auto.pattern == ""

    def test_malformed_json_falls_back_silently(self):
        cfg = FakeConfig({
            "Plot": {"profiles_json": "{not valid json"},
        })
        store = PlotProfileStore(cfg)
        assert store.names() == [AUTO_PROFILE_NAME]

    def test_skip_malformed_entries(self):
        """Mixed valid and invalid entries: the bad ones are dropped,
        the good ones survive."""
        cfg = FakeConfig({
            "Plot": {
                "profiles_json": '['
                '{"name":"Good","mode":"manual","pattern":"(?P<x>\\\\d+)"},'
                '{"name":"","mode":"auto"},'  # empty name
                '{"name":"BadMode","mode":"silly"},'
                '"not even a dict",'
                '{"name":"OK","mode":"auto"}'
                ']',
            },
        })
        store = PlotProfileStore(cfg)
        names = store.names()
        assert AUTO_PROFILE_NAME in names
        assert "Good" in names
        assert "OK" in names
        assert "BadMode" not in names
        assert "" not in names

    def test_default_falls_back_to_auto_when_missing(self):
        cfg = FakeConfig({
            "Plot": {
                "profiles_json": '[{"name":"Auto-detect","mode":"auto"}]',
                "default_profile": "Vanished Profile",
            },
        })
        store = PlotProfileStore(cfg)
        assert store.default_name == AUTO_PROFILE_NAME
        assert store.default_profile().is_builtin


# ─────────────────────────────────────────────────────────────────────────────
# PlotProfileStore — write API
# ─────────────────────────────────────────────────────────────────────────────

class TestProfileStoreWrite:
    def test_upsert_new(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor", r"(?P<x>\d+)"))
        assert "Motor" in store.names()
        # Must have persisted
        assert cfg.save_count >= 1

    def test_upsert_update(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor", r"(?P<x>\d+)"))
        store.upsert(PlotConfig.manual("Motor", r"(?P<y>\d+)"))
        m = store.get("Motor")
        assert m is not None
        assert m.pattern == r"(?P<y>\d+)"
        # Still only one Motor
        assert sum(1 for n in store.names() if n == "Motor") == 1

    def test_upsert_auto_with_manual_mode_rejected(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        with pytest.raises(ValueError, match="must remain in auto mode"):
            store.upsert(PlotConfig(
                name=AUTO_PROFILE_NAME, mode="manual", pattern="x",
            ))

    def test_delete_user_profile(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor", r"(?P<x>\d+)"))
        assert store.delete("Motor") is True
        assert "Motor" not in store.names()

    def test_delete_nonexistent_returns_false(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        assert store.delete("Ghost") is False

    def test_delete_auto_protected(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        assert store.delete(AUTO_PROFILE_NAME) is False
        assert AUTO_PROFILE_NAME in store.names()

    def test_delete_default_falls_back_to_auto(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor", r"(?P<x>\d+)"))
        store.set_default("Motor")
        assert store.default_name == "Motor"
        store.delete("Motor")
        assert store.default_name == AUTO_PROFILE_NAME

    def test_rename_user_profile(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor", r"(?P<x>\d+)"))
        assert store.rename("Motor", "Motor v2") is True
        assert "Motor v2" in store.names()
        assert "Motor" not in store.names()

    def test_rename_preserves_default(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor", r"(?P<x>\d+)"))
        store.set_default("Motor")
        store.rename("Motor", "Motor v2")
        assert store.default_name == "Motor v2"

    def test_rename_collision_rejected(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("A", r"(?P<x>\d+)"))
        store.upsert(PlotConfig.manual("B", r"(?P<x>\d+)"))
        assert store.rename("A", "B") is False
        assert store.get("A") is not None
        assert store.get("B") is not None

    def test_rename_auto_protected(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        assert store.rename(AUTO_PROFILE_NAME, "Auto2") is False

    def test_rename_to_empty_rejected(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor", r"(?P<x>\d+)"))
        assert store.rename("Motor", "") is False
        assert store.rename("Motor", "   ") is False

    def test_set_default_existing(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        store.upsert(PlotConfig.manual("Motor", r"(?P<x>\d+)"))
        assert store.set_default("Motor") is True
        assert store.default_name == "Motor"

    def test_set_default_nonexistent_rejected(self):
        cfg = FakeConfig()
        store = PlotProfileStore(cfg)
        assert store.set_default("Ghost") is False
        assert store.default_name == AUTO_PROFILE_NAME

    def test_round_trip_via_disk(self):
        """Save state, construct a NEW store from the same config —
        all profiles and the default survive."""
        cfg = FakeConfig()
        store1 = PlotProfileStore(cfg)
        store1.upsert(PlotConfig.manual("Motor", r"(?P<r>\d+)"))
        store1.upsert(PlotConfig.manual("BMS", r"(?P<v>[\d.]+)"))
        store1.set_default("BMS")

        store2 = PlotProfileStore(cfg)
        assert store2.names() == [AUTO_PROFILE_NAME, "Motor", "BMS"]
        assert store2.default_name == "BMS"
        assert store2.get("Motor").pattern == r"(?P<r>\d+)"
        assert store2.get("BMS").pattern == r"(?P<v>[\d.]+)"

    def test_special_chars_in_profile_name_round_trip(self):
        """Names with dots, spaces, parens, unicode — JSON encoding
        handles all without escaping issues."""
        cfg = FakeConfig()
        store1 = PlotProfileStore(cfg)
        weird = "Motor v3.2 (debug build) — éôü"
        store1.upsert(PlotConfig.manual(weird, r"(?P<x>\d+)"))

        store2 = PlotProfileStore(cfg)
        assert weird in store2.names()


# ─────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestNameValidation:
    def test_valid_simple(self):
        assert PlotProfileStore.is_valid_profile_name("Motor")
        assert PlotProfileStore.is_valid_profile_name("Motor v3")
        assert PlotProfileStore.is_valid_profile_name("Motor v3.2 (debug)")

    def test_empty_invalid(self):
        assert not PlotProfileStore.is_valid_profile_name("")
        assert not PlotProfileStore.is_valid_profile_name("   ")

    def test_too_long_invalid(self):
        assert not PlotProfileStore.is_valid_profile_name("x" * (PROFILE_NAME_MAX + 1))

    def test_control_chars_invalid(self):
        assert not PlotProfileStore.is_valid_profile_name("Bad\x00Name")
        assert not PlotProfileStore.is_valid_profile_name("Bad\nName")
        assert not PlotProfileStore.is_valid_profile_name("Bad\tName")
