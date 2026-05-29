# SPDX-License-Identifier: MIT
"""WireTrace plot configuration — value objects and profile storage.

A ``PlotConfig`` describes one plotting setup: either auto-detect, or
manual mode with a regex pattern. Configurations are saved as named
profiles inside the existing ``preferences.ini`` under the ``[Plot]``
section, JSON-encoded to support arbitrary profile names without INI
escaping headaches.

Schema in preferences.ini::

    [Plot]
    default_profile = Auto-detect
    profiles_json = [{"name": "Auto-detect", "mode": "auto"},
                     {"name": "Motor v3",    "mode": "manual",
                      "pattern": "RPM:\\s*(?P<RPM>\\d+)"}]

This module:
  - Owns the canonical PlotConfig dataclass
  - Knows how to serialise/deserialise profile lists to/from the
    config string-blob
  - Always exposes the built-in "Auto-detect" profile, even on a fresh
    install or a corrupted config

This module does NOT touch UI, the plot engine, or disk directly —
all persistence flows through the injected ``ConfigManager``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import ConfigManager

logger = logging.getLogger(__name__)


# ── Canonical built-in profile name ─────────────────────────────────────────
#
# Every install ships with this profile. It cannot be deleted or renamed
# by the user — selecting it is how you say "use auto-detect."
AUTO_PROFILE_NAME = "Auto-detect"

# Maximum profile name length. Long enough for descriptive names like
# "Motor controller v3 (debug build)" but short enough to display
# cleanly in the dropdown.
PROFILE_NAME_MAX = 64

# Maximum pattern length. Real regexes are far smaller; this is a
# defensive cap against accidentally pasting an entire log file.
PROFILE_PATTERN_MAX = 4096


# ── Data classes ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PlotConfig:
    """Describes one plot configuration.

    ``mode`` is ``"auto"`` for auto-detect or ``"manual"`` for
    user-declared regex. ``pattern`` is required iff mode is manual.
    """

    name: str
    mode: str  # "auto" | "manual"
    pattern: str = ""

    def __post_init__(self) -> None:
        if self.mode not in ("auto", "manual"):
            raise ValueError(
                f"PlotConfig.mode must be 'auto' or 'manual', got {self.mode!r}",
            )
        if self.mode == "manual" and not self.pattern.strip():
            raise ValueError("Manual-mode PlotConfig requires a non-empty pattern.")
        if not self.name.strip():
            raise ValueError("PlotConfig.name cannot be empty.")
        if len(self.name) > PROFILE_NAME_MAX:
            raise ValueError(
                f"PlotConfig.name exceeds {PROFILE_NAME_MAX} characters.",
            )
        if len(self.pattern) > PROFILE_PATTERN_MAX:
            raise ValueError(
                f"PlotConfig.pattern exceeds {PROFILE_PATTERN_MAX} characters.",
            )

    @property
    def is_builtin(self) -> bool:
        """True for the canonical Auto-detect profile.

        Built-in profiles are protected from rename and delete in the UI.
        """
        return self.name == AUTO_PROFILE_NAME

    def to_dict(self) -> dict[str, str]:
        out: dict[str, str] = {"name": self.name, "mode": self.mode}
        if self.mode == "manual":
            out["pattern"] = self.pattern
        return out

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PlotConfig:
        name = str(data.get("name", "")).strip()
        mode = str(data.get("mode", "auto")).strip().lower()
        pattern = str(data.get("pattern", ""))
        return cls(name=name, mode=mode, pattern=pattern)

    @classmethod
    def auto(cls, name: str = AUTO_PROFILE_NAME) -> PlotConfig:
        """Construct an auto-mode profile."""
        return cls(name=name, mode="auto", pattern="")

    @classmethod
    def manual(cls, name: str, pattern: str) -> PlotConfig:
        """Construct a manual-mode profile."""
        return cls(name=name, mode="manual", pattern=pattern)


# ── Profile store ───────────────────────────────────────────────────────────

@dataclass
class _ProfileStoreState:
    """Internal — the loaded state of the profile store."""

    profiles: list[PlotConfig] = field(default_factory=list)
    default_name: str = AUTO_PROFILE_NAME


class PlotProfileStore:
    """Read/write plot profiles to ``preferences.ini`` via ConfigManager.

    Always exposes at least the built-in Auto-detect profile. Corrupt
    or missing config falls back to defaults silently — the user
    should never see a parse error from a stray byte in their INI.

    All mutations (add / rename / delete / set_default) are written
    through to disk on each call so a crash mid-session doesn't lose
    the user's most recent edit.
    """

    SECTION = "Plot"
    KEY_PROFILES = "profiles_json"
    KEY_DEFAULT = "default_profile"

    def __init__(self, config: ConfigManager) -> None:
        self._config = config
        self._state = _ProfileStoreState()
        self._load()

    # ── Read API ─────────────────────────────────────────────────────────

    def all_profiles(self) -> list[PlotConfig]:
        """Return all profiles in display order (Auto-detect always first)."""
        return list(self._state.profiles)

    def names(self) -> list[str]:
        """Return profile names in display order."""
        return [p.name for p in self._state.profiles]

    def get(self, name: str) -> PlotConfig | None:
        """Return the named profile, or None if not found."""
        for p in self._state.profiles:
            if p.name == name:
                return p
        return None

    @property
    def default_name(self) -> str:
        """Name of the default profile to apply on new tabs.

        Always returns a name that exists in the store. If the saved
        default no longer exists, falls back to AUTO_PROFILE_NAME.
        """
        for p in self._state.profiles:
            if p.name == self._state.default_name:
                return self._state.default_name
        return AUTO_PROFILE_NAME

    def default_profile(self) -> PlotConfig:
        """Return the default profile (always non-None)."""
        p = self.get(self.default_name)
        return p if p is not None else PlotConfig.auto()

    # ── Write API ────────────────────────────────────────────────────────

    def upsert(self, profile: PlotConfig) -> None:
        """Add or update a profile in place.

        The built-in Auto-detect profile cannot be replaced with a
        manual config (would break the contract), but its presence
        in the store is preserved by this method either way.
        """
        if profile.name == AUTO_PROFILE_NAME and profile.mode != "auto":
            raise ValueError(
                f"The built-in {AUTO_PROFILE_NAME!r} profile must remain in auto mode.",
            )

        for i, existing in enumerate(self._state.profiles):
            if existing.name == profile.name:
                self._state.profiles[i] = profile
                self._save()
                return
        self._state.profiles.append(profile)
        self._save()

    def delete(self, name: str) -> bool:
        """Delete a profile. Returns True if it existed and was removed.

        The built-in Auto-detect profile cannot be deleted; attempting
        to do so is a no-op that returns False.
        """
        if name == AUTO_PROFILE_NAME:
            return False
        for i, existing in enumerate(self._state.profiles):
            if existing.name == name:
                del self._state.profiles[i]
                # If the deleted one was the default, fall back.
                if self._state.default_name == name:
                    self._state.default_name = AUTO_PROFILE_NAME
                self._save()
                return True
        return False

    def rename(self, old_name: str, new_name: str) -> bool:
        """Rename a profile. Returns True on success.

        Built-in profiles cannot be renamed. New name must be unique
        and within length limits.
        """
        if old_name == AUTO_PROFILE_NAME:
            return False
        new_name = new_name.strip()
        if not new_name or len(new_name) > PROFILE_NAME_MAX:
            return False
        if any(p.name == new_name for p in self._state.profiles):
            return False
        for i, existing in enumerate(self._state.profiles):
            if existing.name == old_name:
                self._state.profiles[i] = PlotConfig(
                    name=new_name,
                    mode=existing.mode,
                    pattern=existing.pattern,
                )
                if self._state.default_name == old_name:
                    self._state.default_name = new_name
                self._save()
                return True
        return False

    def set_default(self, name: str) -> bool:
        """Mark a profile as the default. Returns True on success."""
        if not any(p.name == name for p in self._state.profiles):
            return False
        self._state.default_name = name
        self._save()
        return True

    # ── Validation helpers ───────────────────────────────────────────────

    @staticmethod
    def is_valid_profile_name(name: str) -> bool:
        """A profile name is valid if non-empty, fits the length cap,
        and contains no characters that would corrupt the JSON store
        round-trip. We allow most printables; we exclude only control
        characters."""
        if not name or not name.strip():
            return False
        if len(name) > PROFILE_NAME_MAX:
            return False
        return not re.search(r"[\x00-\x1f\x7f]", name)

    # ── Persistence ──────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load profiles + default from preferences.ini.

        Falls back to defaults silently on parse error or missing keys.
        Always ensures the built-in Auto-detect profile is present.
        """
        # Default profile name
        default_name = self._config.get(
            self.SECTION, self.KEY_DEFAULT, fallback=AUTO_PROFILE_NAME,
        ) or AUTO_PROFILE_NAME

        # Profiles JSON blob
        raw = self._config.get(self.SECTION, self.KEY_PROFILES, fallback="") or ""

        profiles: list[PlotConfig] = []
        if raw.strip():
            try:
                data = json.loads(raw)
                if isinstance(data, list):
                    for entry in data:
                        if not isinstance(entry, dict):
                            continue
                        try:
                            profiles.append(PlotConfig.from_dict(entry))
                        except (ValueError, TypeError) as exc:
                            logger.warning(
                                "Skipping malformed plot profile %r: %s",
                                entry, exc,
                            )
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Plot profile JSON failed to parse; using defaults: %s", exc,
                )

        # Always ensure Auto-detect exists, in first position
        if not any(p.name == AUTO_PROFILE_NAME for p in profiles):
            profiles.insert(0, PlotConfig.auto())
        else:
            # Move it to the front for stable ordering
            auto = next(p for p in profiles if p.name == AUTO_PROFILE_NAME)
            profiles = [auto] + [p for p in profiles if p.name != AUTO_PROFILE_NAME]
            # And make sure it's still in auto mode (defensive)
            if auto.mode != "auto":
                profiles[0] = PlotConfig.auto()

        self._state.profiles = profiles
        self._state.default_name = default_name

    def _save(self) -> None:
        """Persist current state to preferences.ini.

        Calls ``ConfigManager.save()`` so the change reaches disk
        immediately. INI section/keys are created on first write.
        """
        payload = json.dumps(
            [p.to_dict() for p in self._state.profiles],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self._config.set(self.SECTION, self.KEY_PROFILES, payload)
        self._config.set(self.SECTION, self.KEY_DEFAULT, self._state.default_name)
        try:
            self._config.save()
        except Exception:
            # Persistence failures shouldn't crash the dialog; log and
            # let the in-memory state diverge from disk until next save.
            logger.exception("Failed to persist plot profiles")
