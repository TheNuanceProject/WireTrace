# SPDX-License-Identifier: MIT
"""WireTrace centralized configuration manager.

Responsibilities:
  - Read/write preferences.ini
  - Provide sane defaults for all settings
  - Graceful fallback to defaults on parse error or missing file
  - Validate values on load
  - Ensure config directory exists before writing

This module does NOT touch: UI, serial, or logging.
"""

import configparser
import logging
import os
from typing import Any

from app.constants import (
    DEFAULT_BAUD_RATE,
    DEFAULT_FONT_FAMILY,
    DEFAULT_FONT_SIZE,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    DEFAULT_WINDOW_X,
    DEFAULT_WINDOW_Y,
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    GUI_UPDATE_INTERVAL_MS,
    LOG_BUFFER_MAX_ENTRIES,
    LOG_FLUSH_INTERVAL_MS,
    MAX_CONSOLE_LINES,
    PREFERENCES_FILENAME,
    ThemeID,
    get_config_dir,
    get_default_log_dir,
)

logger = logging.getLogger(__name__)

# ── Default Configuration ────────────────────────────────────────────────────

_DEFAULTS = {
    "General": {
        "theme": ThemeID.MIDNIGHT_DARK.value,
        "language": "en",
        "check_updates_on_startup": "true",
        "update_check_interval_hours": "24",
        "default_log_directory": "",  # Resolved at runtime via get_default_log_dir()
    },
    "Display": {
        "font_family": DEFAULT_FONT_FAMILY,
        "font_size": str(DEFAULT_FONT_SIZE),
        "color_mode": "true",
        "line_spacing": "true",
        "auto_scroll": "true",
        "default_display_mode": "text",
        "timestamp_mode": "absolute",
    },
    "Serial": {
        "default_baud_rate": str(DEFAULT_BAUD_RATE),
    },
    "Export": {
        "default_format": "txt",
    },
    "Performance": {
        "gui_update_interval_ms": str(GUI_UPDATE_INTERVAL_MS),
        "log_buffer_max_entries": str(LOG_BUFFER_MAX_ENTRIES),
        "log_flush_interval_ms": str(LOG_FLUSH_INTERVAL_MS),
        "max_console_lines": str(MAX_CONSOLE_LINES),
    },
    "Colors": {
        "critical": "#B71C1C",
        "error": "#C62828",
        "warning": "#E65100",
        "info": "#1565C0",
        "debug": "#757575",
        "command": "#0D47A1",
        "data": "#212121",
        "timestamp": "#9E9E9E",
    },
    "WindowState": {
        "width": str(DEFAULT_WINDOW_WIDTH),
        "height": str(DEFAULT_WINDOW_HEIGHT),
        "x": str(DEFAULT_WINDOW_X),
        "y": str(DEFAULT_WINDOW_Y),
        "maximized": "false",
    },
}


class ConfigManager:
    """Reads and writes application preferences to an INI file.

    Thread-safety: This class is intended to be used from the main thread only.
    Configuration is loaded once at startup and saved on changes or shutdown.
    """

    def __init__(self) -> None:
        self._config = configparser.ConfigParser()
        self._config_dir = get_config_dir()
        self._config_path = os.path.join(self._config_dir, PREFERENCES_FILENAME)
        self._apply_defaults()
        self._load()

    # ── Public API ───────────────────────────────────────────────────────

    def get(self, section: str, key: str, fallback: str | None = None) -> str:
        """Get a string value from configuration.

        Args:
            section: INI section name (e.g., "General", "Display").
            key: Key within the section.
            fallback: Value to return if key is missing. If None, uses
                      the built-in default.

        Returns:
            The configuration value as a string.
        """
        if fallback is not None:
            return self._config.get(section, key, fallback=fallback)
        return self._config.get(section, key, fallback=self._get_default(section, key))

    def get_int(self, section: str, key: str, fallback: int | None = None) -> int:
        """Get an integer value from configuration with validation.

        Returns the fallback (or built-in default) if the stored value
        is not a valid integer.
        """
        raw = self.get(section, key)
        try:
            return int(raw)
        except (ValueError, TypeError):
            if fallback is not None:
                return fallback
            default = self._get_default(section, key)
            try:
                return int(default)
            except (ValueError, TypeError):
                return 0

    def get_bool(self, section: str, key: str, fallback: bool | None = None) -> bool:
        """Get a boolean value from configuration.

        Accepts: true/false, yes/no, 1/0, on/off (case-insensitive).
        Returns the fallback (or built-in default) on invalid input.
        """
        raw = self.get(section, key)
        if raw.lower() in ("true", "yes", "1", "on"):
            return True
        if raw.lower() in ("false", "no", "0", "off"):
            return False
        if fallback is not None:
            return fallback
        default = self._get_default(section, key)
        return default.lower() in ("true", "yes", "1", "on")

    def set(self, section: str, key: str, value: Any) -> None:
        """Set a configuration value.

        Args:
            section: INI section name.
            key: Key within the section.
            value: Value to store (converted to string).
        """
        if not self._config.has_section(section):
            self._config.add_section(section)
        self._config.set(section, key, str(value))

    def load(self) -> None:
        """Reload configuration from disk."""
        self._load()

    def save(self) -> bool:
        """Write current configuration to disk.

        Returns:
            True if save succeeded, False otherwise.
        """
        try:
            os.makedirs(self._config_dir, exist_ok=True)
            with open(self._config_path, "w", encoding="utf-8") as f:
                self._config.write(f)
            logger.info("Configuration saved to %s", self._config_path)
            return True
        except OSError as e:
            logger.error("Failed to save configuration: %s", e)
            return False

    def reset_to_defaults(self) -> None:
        """Reset all settings to their default values."""
        self._config = configparser.ConfigParser()
        self._apply_defaults()
        logger.info("Configuration reset to defaults")

    @property
    def config_path(self) -> str:
        """Return the full path to the preferences file."""
        return self._config_path

    # ── Convenience Properties ───────────────────────────────────────────

    @property
    def theme(self) -> str:
        return self.get("General", "theme")

    @theme.setter
    def theme(self, value: str) -> None:
        self.set("General", "theme", value)

    @property
    def font_family(self) -> str:
        return self.get("Display", "font_family")

    @font_family.setter
    def font_family(self, value: str) -> None:
        self.set("Display", "font_family", value)

    @property
    def font_size(self) -> int:
        size = self.get_int("Display", "font_size")
        return max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, size))

    @font_size.setter
    def font_size(self, value: int) -> None:
        clamped = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, value))
        self.set("Display", "font_size", clamped)

    @property
    def default_baud_rate(self) -> int:
        return self.get_int("Serial", "default_baud_rate", fallback=DEFAULT_BAUD_RATE)

    @default_baud_rate.setter
    def default_baud_rate(self, value: int) -> None:
        self.set("Serial", "default_baud_rate", value)

    @property
    def color_mode(self) -> bool:
        return self.get_bool("Display", "color_mode", fallback=True)

    @color_mode.setter
    def color_mode(self, value: bool) -> None:
        self.set("Display", "color_mode", str(value).lower())

    @property
    def auto_scroll(self) -> bool:
        return self.get_bool("Display", "auto_scroll", fallback=True)

    @auto_scroll.setter
    def auto_scroll(self, value: bool) -> None:
        self.set("Display", "auto_scroll", str(value).lower())

    @property
    def line_spacing(self) -> bool:
        return self.get_bool("Display", "line_spacing", fallback=True)

    @line_spacing.setter
    def line_spacing(self, value: bool) -> None:
        self.set("Display", "line_spacing", str(value).lower())

    @property
    def timestamp_mode(self) -> str:
        return self.get("Display", "timestamp_mode")

    @timestamp_mode.setter
    def timestamp_mode(self, value: str) -> None:
        self.set("Display", "timestamp_mode", value)

    @property
    def display_mode(self) -> str:
        return self.get("Display", "default_display_mode")

    @display_mode.setter
    def display_mode(self, value: str) -> None:
        self.set("Display", "default_display_mode", value)

    @property
    def default_log_directory(self) -> str:
        stored = self.get("General", "default_log_directory")
        if stored:
            return stored
        return get_default_log_dir()

    @default_log_directory.setter
    def default_log_directory(self, value: str) -> None:
        self.set("General", "default_log_directory", value)

    @property
    def default_export_format(self) -> str:
        return self.get("Export", "default_format")

    @default_export_format.setter
    def default_export_format(self, value: str) -> None:
        self.set("Export", "default_format", value)

    @property
    def check_updates_on_startup(self) -> bool:
        return self.get_bool("General", "check_updates_on_startup", fallback=True)

    @check_updates_on_startup.setter
    def check_updates_on_startup(self, value: bool) -> None:
        self.set("General", "check_updates_on_startup", str(value).lower())

    @property
    def update_check_interval_hours(self) -> int:
        return self.get_int("General", "update_check_interval_hours", fallback=24)

    @update_check_interval_hours.setter
    def update_check_interval_hours(self, value: int) -> None:
        self.set("General", "update_check_interval_hours", value)

    @property
    def gui_update_interval_ms(self) -> int:
        return self.get_int("Performance", "gui_update_interval_ms",
                            fallback=GUI_UPDATE_INTERVAL_MS)

    @property
    def log_buffer_max_entries(self) -> int:
        return self.get_int("Performance", "log_buffer_max_entries",
                            fallback=LOG_BUFFER_MAX_ENTRIES)

    @property
    def log_flush_interval_ms(self) -> int:
        return self.get_int("Performance", "log_flush_interval_ms",
                            fallback=LOG_FLUSH_INTERVAL_MS)

    @property
    def max_console_lines(self) -> int:
        return self.get_int("Performance", "max_console_lines",
                            fallback=MAX_CONSOLE_LINES)

    @property
    def window_width(self) -> int:
        return self.get_int("WindowState", "width", fallback=DEFAULT_WINDOW_WIDTH)

    @window_width.setter
    def window_width(self, value: int) -> None:
        self.set("WindowState", "width", value)

    @property
    def window_height(self) -> int:
        return self.get_int("WindowState", "height", fallback=DEFAULT_WINDOW_HEIGHT)

    @window_height.setter
    def window_height(self, value: int) -> None:
        self.set("WindowState", "height", value)

    @property
    def window_x(self) -> int:
        return self.get_int("WindowState", "x", fallback=DEFAULT_WINDOW_X)

    @window_x.setter
    def window_x(self, value: int) -> None:
        self.set("WindowState", "x", value)

    @property
    def window_y(self) -> int:
        return self.get_int("WindowState", "y", fallback=DEFAULT_WINDOW_Y)

    @window_y.setter
    def window_y(self, value: int) -> None:
        self.set("WindowState", "y", value)

    @property
    def maximized(self) -> bool:
        return self.get_bool("WindowState", "maximized", fallback=False)

    @maximized.setter
    def maximized(self, value: bool) -> None:
        self.set("WindowState", "maximized", str(value).lower())

    def get_tag_color(self, tag: str) -> str:
        """Get the configured color for a severity tag.

        Args:
            tag: Tag name (lowercase in config: critical, error, etc.)

        Returns:
            Hex color string (e.g., "#B71C1C").
        """
        return self.get("Colors", tag.lower())

    def set_tag_color(self, tag: str, color: str) -> None:
        """Set the color for a severity tag.

        Args:
            tag: Tag name (lowercase in config).
            color: Hex color string (e.g., "#B71C1C").
        """
        self.set("Colors", tag.lower(), color)

    # ── Internal ─────────────────────────────────────────────────────────

    def _apply_defaults(self) -> None:
        """Populate the config parser with all default values."""
        for section, keys in _DEFAULTS.items():
            if not self._config.has_section(section):
                self._config.add_section(section)
            for key, value in keys.items():
                if not self._config.has_option(section, key):
                    self._config.set(section, key, value)

    def _load(self) -> None:
        """Load preferences from disk. Falls back to defaults on any error."""
        if not os.path.isfile(self._config_path):
            logger.info("No preferences file found at %s — using defaults",
                        self._config_path)
            return

        try:
            self._config.read(self._config_path, encoding="utf-8")
            # Re-apply defaults to fill any missing keys from older config files
            self._apply_defaults()
            logger.info("Preferences loaded from %s", self._config_path)
        except (configparser.Error, OSError, UnicodeDecodeError) as e:
            logger.warning("Failed to parse preferences file: %s — resetting to defaults", e)
            self._config = configparser.ConfigParser()
            self._apply_defaults()

    def _get_default(self, section: str, key: str) -> str:
        """Return the built-in default for a given section/key pair."""
        try:
            return _DEFAULTS[section][key]
        except KeyError:
            return ""
