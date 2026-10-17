# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace centralized configuration manager.

Responsibilities:
  - Read/write preferences.ini
  - Provide sane defaults for all settings
  - Graceful fallback to defaults on parse error or missing file
  - Validate values on load
  - Ensure config directory exists before writing

This module does not touch the UI, the serial port or log files.
"""

import configparser
import io
import logging
import os
from datetime import datetime
from typing import Any

from app._atomic import atomic_write_text
from app.constants import (
    DEFAULT_BAUD_RATE,
    DEFAULT_FONT_FAMILY,
    DEFAULT_FONT_SIZE,
    DEFAULT_LOG_LINE_ENDING,
    DEFAULT_WINDOW_HEIGHT,
    DEFAULT_WINDOW_WIDTH,
    DEFAULT_WINDOW_X,
    DEFAULT_WINDOW_Y,
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    GUI_UPDATE_INTERVAL_MS,
    GUI_UPDATE_INTERVAL_MS_MAX,
    GUI_UPDATE_INTERVAL_MS_MIN,
    LOG_BUFFER_MAX_ENTRIES,
    LOG_BUFFER_MAX_ENTRIES_MAX,
    LOG_BUFFER_MAX_ENTRIES_MIN,
    LOG_FLUSH_INTERVAL_MS,
    LOG_FLUSH_INTERVAL_MS_MAX,
    LOG_FLUSH_INTERVAL_MS_MIN,
    LOG_LINE_ENDINGS,
    LOG_SPLIT_DEFAULT_MB,
    LOG_SPLIT_MAX_MB,
    MAX_CONSOLE_LINES,
    MAX_CONSOLE_LINES_MAX,
    MAX_CONSOLE_LINES_MIN,
    PREFERENCES_FILENAME,
    SETTINGS_VERSION,
    SETTINGS_VERSION_UNVERSIONED,
    SHARED_FILE_LOCK_SUFFIX,
    SHARED_FILE_LOCK_TIMEOUT_S,
    THEME_FOLLOW_SYSTEM,
    WINDOW_POSITION_MAX,
    WINDOW_POSITION_MIN,
    WINDOW_SIZE_MAX,
    WINDOW_SIZE_MIN,
    ExportFormat,
    LineEnding,
    get_config_dir,
    get_default_log_dir,
)
from app.file_lock import held_lock

logger = logging.getLogger(__name__)

# ── Default Configuration ────────────────────────────────────────────────────

_BYTES_PER_MB = 1024 * 1024


def _new_parser() -> configparser.ConfigParser:
    """Create the parser used for preferences.

    Interpolation is off: values are stored exactly as given, so a
    ``%`` in a plot pattern or a directory name is an ordinary character.
    """
    return configparser.ConfigParser(interpolation=None)


_DEFAULTS = {
    "General": {
        "settings_version": str(SETTINGS_VERSION),
        "theme": THEME_FOLLOW_SYSTEM,
        "check_updates_on_startup": "true",
        "default_log_directory": "",  # Resolved at runtime via get_default_log_dir()
    },
    "Display": {
        "font_family": DEFAULT_FONT_FAMILY,
        "font_size": str(DEFAULT_FONT_SIZE),
        "color_mode": "true",
        "auto_scroll": "true",
        "timestamp_mode": "absolute",
        "hide_blank_lines": "false",
    },
    "Serial": {
        "default_baud_rate": str(DEFAULT_BAUD_RATE),
    },
    "Export": {
        "default_format": "txt",
        "last_format": "txt",
        "last_directory": "",
    },
    "Logging": {
        "split_size_mb": str(LOG_SPLIT_DEFAULT_MB),
        "line_ending": DEFAULT_LOG_LINE_ENDING.name,
    },
    "Performance": {
        "gui_update_interval_ms": str(GUI_UPDATE_INTERVAL_MS),
        "log_buffer_max_entries": str(LOG_BUFFER_MAX_ENTRIES),
        "log_flush_interval_ms": str(LOG_FLUSH_INTERVAL_MS),
        "max_console_lines": str(MAX_CONSOLE_LINES),
    },
    "Plot": {
        # Persisted by app.plot_config.PlotProfileStore. Default values
        # ensure a fresh install boots with the built-in Auto-detect
        # profile selected.
        "default_profile": "Auto-detect",
        "profiles_json": '[{"name":"Auto-detect","mode":"auto"}]',
    },
    "Updates": {
        # Whether the choice made in the Windows installer about update
        # checks has been looked at. See WireTraceApp._take_over_installer_choice.
        "installer_choice_read": "false",
        # What the user answered in the update window, and the day the
        # server named at the previous check. See updater/update_manager.py.
        "skipped_version": "",
        "snoozed_until": "",
        "last_check_day": "",
        # When the newest update manifest accepted was signed. One signed
        # earlier is refused. See updater/update_manager.py.
        "newest_manifest_signed_at": "",
    },
    "Privacy": {
        # Whether usage statistics are shared (off unless the user
        # switches them on), and how often the question about them has
        # been shown and whether it was answered. See updater/usage.py.
        "usage_statistics": "false",
        "usage_question_shown": "0",
        "usage_question_answered": "false",
    },
    "Commands": {
        # Persisted by app.saved_commands.SavedCommands.
        "saved_json": "[]",
    },
    "Ports": {
        # Persisted by app.port_memory.PortMemory: what each port was
        # last used with.
        "remembered_json": "{}",
    },
    "WindowState": {
        "width": str(DEFAULT_WINDOW_WIDTH),
        "height": str(DEFAULT_WINDOW_HEIGHT),
        "x": str(DEFAULT_WINDOW_X),
        "y": str(DEFAULT_WINDOW_Y),
        "maximized": "false",
    },
}


#: Values that are the user's own data, not settings. Restoring the
#: defaults leaves them as they are.
_KEPT_ON_RESET: tuple[tuple[str, str], ...] = (
    ("Plot", "profiles_json"),
    ("Ports", "remembered_json"),
    ("Commands", "saved_json"),
    ("Export", "last_directory"),
    # The user's answer about update checks is a choice, not a default.
    ("General", "check_updates_on_startup"),
    *(("Updates", key) for key in _DEFAULTS["Updates"]),
    # So are the answers about usage statistics.
    *(("Privacy", key) for key in _DEFAULTS["Privacy"]),
    *(("WindowState", key) for key in _DEFAULTS["WindowState"]),
)


class ConfigManager:
    """Reads and writes application preferences to an INI file.

    Thread-safety: This class is intended to be used from the main thread only.
    Configuration is loaded at startup and saved on changes or shutdown.

    Several WireTrace windows may be open at once, each with its own
    ConfigManager. A save therefore writes only the settings this one
    changed since it last read or wrote the file, onto what the file
    holds now, while holding the file's lock. A setting changed in two
    windows keeps the value saved last.
    """

    def __init__(self) -> None:
        self._config = _new_parser()
        self._config_dir = get_config_dir()
        self._config_path = os.path.join(self._config_dir, PREFERENCES_FILENAME)
        self._lock_path = self._config_path + SHARED_FILE_LOCK_SUFFIX
        self._first_run = not os.path.isfile(self._config_path)
        #: The settings changed here and not yet written.
        self._changed: set[tuple[str, str]] = set()
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
        text = str(value)
        if self._config.get(section, key, fallback=None) != text:
            self._changed.add((section, key))
        self._config.set(section, key, text)

    @property
    def first_run(self) -> bool:
        """Whether this user had no preferences file when the application
        started: nothing has been chosen by them yet."""
        return self._first_run

    def load(self) -> None:
        """Reload configuration from disk. Changes not saved are dropped."""
        self._changed.clear()
        self._load()

    def save(self) -> bool:
        """Write the settings changed here to disk, atomically.

        While the file's lock is held, the file is read again, the
        settings changed here are set on what it holds, and the result is
        written via ``atomic_write_text``, so an interrupted save (crash,
        power loss, disk full) can never leave ``preferences.ini`` empty
        or truncated, and a setting saved meanwhile by another WireTrace
        window is kept. Afterwards this manager holds what was written.

        Returns:
            True if save succeeded, False otherwise.
        """
        try:
            os.makedirs(self._config_dir, exist_ok=True)
            with held_lock(self._lock_path, SHARED_FILE_LOCK_TIMEOUT_S):
                merged = self._merged_with_file()
                buffer = io.StringIO()
                merged.write(buffer)
                atomic_write_text(self._config_path, buffer.getvalue(), encoding="utf-8")
            self._config = merged
            self._changed.clear()
            logger.info("Configuration saved to %s", self._config_path)
            return True
        except OSError as e:
            # LockTimeout is an OSError: another window held the file too long.
            logger.error("Failed to save configuration: %s", e)
            return False

    def _merged_with_file(self) -> configparser.ConfigParser:
        """What the file holds now, with the settings changed here set on
        it. When the file is missing or cannot be read, the settings
        held here, whole."""
        on_disk = _new_parser()
        try:
            if not on_disk.read(self._config_path, encoding="utf-8"):
                return self._config
        except (configparser.Error, OSError, UnicodeDecodeError) as e:
            logger.warning("Preferences file unreadable (%s); writing the settings held here", e)
            return self._config
        for section, key in self._changed:
            if self._config.has_option(section, key):
                if not on_disk.has_section(section):
                    on_disk.add_section(section)
                on_disk.set(section, key, self._config.get(section, key))
        for section, keys in _DEFAULTS.items():
            if not on_disk.has_section(section):
                on_disk.add_section(section)
            for key, value in keys.items():
                if not on_disk.has_option(section, key):
                    on_disk.set(section, key, value)
        return on_disk

    def reset_to_defaults(self) -> None:
        """Return every setting to its default.

        What the user has built up is not a setting and is kept: saved
        plot profiles, saved commands, what each port was last used with,
        the folder of the last export, the update check setting and what
        is remembered about updates, the answers about usage statistics,
        and the place of the window.
        """
        kept = {
            (section, key): self._config.get(section, key)
            for section, key in _KEPT_ON_RESET
            if self._config.has_option(section, key)
        }
        self._config = _new_parser()
        self._apply_defaults()
        for (section, key), value in kept.items():
            self._config.set(section, key, value)
        # Restoring the defaults is a choice about every setting, and about
        # nothing that is kept: what is kept was not touched, so another
        # window's newer value of it in the file must stand, unless it was
        # changed in this window and not yet saved.
        kept_keys = set(_KEPT_ON_RESET)
        self._changed = {
            (section, key)
            for section in self._config.sections()
            for key in self._config.options(section)
            if (section, key) not in kept_keys
        } | (self._changed & kept_keys)
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
        """The console's text size, in logical pixels."""
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
    def timestamp_mode(self) -> str:
        return self.get("Display", "timestamp_mode")

    @timestamp_mode.setter
    def timestamp_mode(self, value: str) -> None:
        self.set("Display", "timestamp_mode", value)

    @property
    def default_log_directory(self) -> str:
        stored = self.get("General", "default_log_directory")
        if stored:
            return stored
        return get_default_log_dir()

    @default_log_directory.setter
    def default_log_directory(self, value: str) -> None:
        # The built-in folder is stored as "no choice", so it follows
        # the system if the documents folder is ever moved.
        self.set(
            "General", "default_log_directory",
            "" if value == get_default_log_dir() else value,
        )

    def _get_format(self, key: str) -> ExportFormat:
        stored = self.get("Export", key).upper()
        return ExportFormat.__members__.get(stored, ExportFormat.TXT)

    @property
    def default_log_format(self) -> ExportFormat:
        """The files a new log is offered with: text, or text and CSV.

        A log always has its text file, so a stored "csv" means both.
        """
        stored = self._get_format("default_format")
        return ExportFormat.TXT if stored is ExportFormat.TXT else ExportFormat.BOTH

    @default_log_format.setter
    def default_log_format(self, value: ExportFormat) -> None:
        self.set("Export", "default_format", value.name.lower())

    @property
    def export_format(self) -> ExportFormat:
        """The files chosen for the last export."""
        return self._get_format("last_format")

    @export_format.setter
    def export_format(self, value: ExportFormat) -> None:
        self.set("Export", "last_format", value.name.lower())

    @property
    def export_directory(self) -> str:
        """The folder of the last export, or the log folder before any."""
        return self.get("Export", "last_directory") or self.default_log_directory

    @export_directory.setter
    def export_directory(self, value: str) -> None:
        self.set("Export", "last_directory", value)

    @property
    def check_updates_on_startup(self) -> bool:
        return self.get_bool("General", "check_updates_on_startup", fallback=True)

    @check_updates_on_startup.setter
    def check_updates_on_startup(self, value: bool) -> None:
        self.set("General", "check_updates_on_startup", str(value).lower())

    @property
    def usage_statistics(self) -> bool:
        """Whether usage statistics are shared. Off unless switched on."""
        return self.get_bool("Privacy", "usage_statistics", fallback=False)

    @usage_statistics.setter
    def usage_statistics(self, value: bool) -> None:
        self.set("Privacy", "usage_statistics", str(value).lower())

    @property
    def usage_question_shown(self) -> int:
        """How many times the question about usage statistics was shown."""
        return max(0, self.get_int("Privacy", "usage_question_shown", fallback=0))

    @usage_question_shown.setter
    def usage_question_shown(self, value: int) -> None:
        self.set("Privacy", "usage_question_shown", max(0, value))

    @property
    def usage_question_answered(self) -> bool:
        """Whether the user answered the question about usage statistics."""
        return self.get_bool("Privacy", "usage_question_answered", fallback=False)

    @usage_question_answered.setter
    def usage_question_answered(self, value: bool) -> None:
        self.set("Privacy", "usage_question_answered", str(value).lower())

    @property
    def installer_choice_read(self) -> bool:
        """Whether the choice made in the Windows installer has been taken over."""
        return self.get_bool("Updates", "installer_choice_read", fallback=False)

    @installer_choice_read.setter
    def installer_choice_read(self, value: bool) -> None:
        self.set("Updates", "installer_choice_read", str(value).lower())

    @property
    def skipped_update_version(self) -> str:
        """The version the user asked not to be offered again, or empty."""
        return self.get("Updates", "skipped_version", fallback="")

    @skipped_update_version.setter
    def skipped_update_version(self, value: str) -> None:
        self.set("Updates", "skipped_version", value)

    @property
    def update_snoozed_until(self) -> datetime | None:
        """Until when no update is to be announced, in UTC, or None."""
        stored = self.get("Updates", "snoozed_until", fallback="")
        try:
            moment = datetime.fromisoformat(stored)
        except ValueError:
            return None
        return moment if moment.tzinfo is not None else None

    @update_snoozed_until.setter
    def update_snoozed_until(self, value: datetime | None) -> None:
        self.set("Updates", "snoozed_until", "" if value is None else value.isoformat())

    @property
    def update_last_check_day(self) -> str:
        """The day the server named at the previous update check, or empty."""
        return self.get("Updates", "last_check_day", fallback="")

    @update_last_check_day.setter
    def update_last_check_day(self, value: str) -> None:
        self.set("Updates", "last_check_day", value)

    @property
    def update_newest_manifest_signed_at(self) -> str:
        """When the newest update manifest accepted was signed, as the
        manifest writes it, or empty before the first."""
        return self.get("Updates", "newest_manifest_signed_at", fallback="")

    @update_newest_manifest_signed_at.setter
    def update_newest_manifest_signed_at(self, value: str) -> None:
        self.set("Updates", "newest_manifest_signed_at", value)

    @property
    def gui_update_interval_ms(self) -> int:
        return self._get_clamped(
            "Performance", "gui_update_interval_ms",
            GUI_UPDATE_INTERVAL_MS_MIN, GUI_UPDATE_INTERVAL_MS_MAX,
        )

    @gui_update_interval_ms.setter
    def gui_update_interval_ms(self, value: int) -> None:
        self.set("Performance", "gui_update_interval_ms", max(GUI_UPDATE_INTERVAL_MS_MIN, min(GUI_UPDATE_INTERVAL_MS_MAX, value)))

    @property
    def log_buffer_max_entries(self) -> int:
        return self._get_clamped(
            "Performance", "log_buffer_max_entries",
            LOG_BUFFER_MAX_ENTRIES_MIN, LOG_BUFFER_MAX_ENTRIES_MAX,
        )

    @log_buffer_max_entries.setter
    def log_buffer_max_entries(self, value: int) -> None:
        self.set("Performance", "log_buffer_max_entries", max(LOG_BUFFER_MAX_ENTRIES_MIN, min(LOG_BUFFER_MAX_ENTRIES_MAX, value)))

    @property
    def log_flush_interval_ms(self) -> int:
        return self._get_clamped(
            "Performance", "log_flush_interval_ms",
            LOG_FLUSH_INTERVAL_MS_MIN, LOG_FLUSH_INTERVAL_MS_MAX,
        )

    @log_flush_interval_ms.setter
    def log_flush_interval_ms(self, value: int) -> None:
        self.set("Performance", "log_flush_interval_ms", max(LOG_FLUSH_INTERVAL_MS_MIN, min(LOG_FLUSH_INTERVAL_MS_MAX, value)))

    @property
    def max_console_lines(self) -> int:
        return self._get_clamped(
            "Performance", "max_console_lines",
            MAX_CONSOLE_LINES_MIN, MAX_CONSOLE_LINES_MAX,
        )

    @max_console_lines.setter
    def max_console_lines(self, value: int) -> None:
        self.set("Performance", "max_console_lines", max(MAX_CONSOLE_LINES_MIN, min(MAX_CONSOLE_LINES_MAX, value)))

    @property
    def log_split_size_mb(self) -> int:
        """Size at which a log starts a new part, in MB (0 = never)."""
        return self._get_clamped("Logging", "split_size_mb", 0, LOG_SPLIT_MAX_MB)

    @log_split_size_mb.setter
    def log_split_size_mb(self, value: int) -> None:
        self.set("Logging", "split_size_mb", max(0, min(LOG_SPLIT_MAX_MB, value)))

    @property
    def log_line_ending(self) -> LineEnding:
        """What ends each line of a log or export file."""
        stored = LineEnding.__members__.get(self.get("Logging", "line_ending"))
        return stored if stored in LOG_LINE_ENDINGS else DEFAULT_LOG_LINE_ENDING

    @log_line_ending.setter
    def log_line_ending(self, value: LineEnding) -> None:
        if value not in LOG_LINE_ENDINGS:
            raise ValueError(f"not a line ending for files: {value!r}")
        self.set("Logging", "line_ending", value.name)

    @property
    def log_split_bytes(self) -> int:
        """Size at which a log starts a new part, in bytes (0 = never)."""
        return self.log_split_size_mb * _BYTES_PER_MB

    @property
    def hide_blank_lines(self) -> bool:
        return self.get_bool("Display", "hide_blank_lines", fallback=False)

    @hide_blank_lines.setter
    def hide_blank_lines(self, value: bool) -> None:
        self.set("Display", "hide_blank_lines", str(value).lower())

    @property
    def window_width(self) -> int:
        return self._get_clamped("WindowState", "width", WINDOW_SIZE_MIN, WINDOW_SIZE_MAX)

    @window_width.setter
    def window_width(self, value: int) -> None:
        self.set("WindowState", "width", value)

    @property
    def window_height(self) -> int:
        return self._get_clamped("WindowState", "height", WINDOW_SIZE_MIN, WINDOW_SIZE_MAX)

    @window_height.setter
    def window_height(self, value: int) -> None:
        self.set("WindowState", "height", value)

    @property
    def window_x(self) -> int:
        return self._get_clamped("WindowState", "x", WINDOW_POSITION_MIN, WINDOW_POSITION_MAX)

    @window_x.setter
    def window_x(self, value: int) -> None:
        self.set("WindowState", "x", value)

    @property
    def window_y(self) -> int:
        return self._get_clamped("WindowState", "y", WINDOW_POSITION_MIN, WINDOW_POSITION_MAX)

    @window_y.setter
    def window_y(self, value: int) -> None:
        self.set("WindowState", "y", value)

    @property
    def maximized(self) -> bool:
        return self.get_bool("WindowState", "maximized", fallback=False)

    @maximized.setter
    def maximized(self, value: bool) -> None:
        self.set("WindowState", "maximized", str(value).lower())

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
            logger.info("No preferences file found at %s. Using defaults",
                        self._config_path)
            return

        try:
            # The file is read on its own, without the built-in defaults
            # underneath, because a missing version is how a 1.x file is
            # recognised.
            stored = _new_parser()
            stored.read(self._config_path, encoding="utf-8")
            self._config = stored
            self._migrate()
            # Re-apply defaults to fill any missing keys from older config files
            self._apply_defaults()
            logger.info("Preferences loaded from %s", self._config_path)
        except (configparser.Error, OSError, UnicodeDecodeError) as e:
            logger.warning("Failed to parse preferences file (%s). Resetting to defaults", e)
            self._config = _new_parser()
            self._apply_defaults()

    def _stored_settings_version(self) -> int:
        """Version of the file that was read. A 1.x file carries none."""
        raw = self._config.get("General", "settings_version", fallback="")
        try:
            return int(raw)
        except ValueError:
            return SETTINGS_VERSION_UNVERSIONED

    def _migrate(self) -> None:
        """Apply, once, the defaults a newer release changed.

        WireTrace 1.x wrote every default into the file, so a saved value
        cannot be told apart from a choice. Moving to 2.0 therefore sets
        the theme and the console font and its size to the 2.0 defaults
        for everyone; every other setting is kept. (1.x stored the size in
        points, 2.0 in pixels, so the old number would be wrong anyway.) The new version is saved with the
        file, so this happens on the first start only and a later choice
        made by the user is never overwritten.
        """
        stored = self._stored_settings_version()
        if stored >= SETTINGS_VERSION:
            return

        for section in ("General", "Display"):
            if not self._config.has_section(section):
                self._config.add_section(section)

        # 1 -> 2: the theme follows the system; the console uses the
        # font shipped with the application, at the design's size.
        self.set("General", "theme", THEME_FOLLOW_SYSTEM)
        self.set("Display", "font_family", DEFAULT_FONT_FAMILY)
        self.set("Display", "font_size", DEFAULT_FONT_SIZE)

        self.set("General", "settings_version", str(SETTINGS_VERSION))
        logger.info(
            "Preferences brought up to date: version %d to %d",
            stored, SETTINGS_VERSION,
        )

    def _get_clamped(self, section: str, key: str, minimum: int, maximum: int) -> int:
        """Return an integer setting, kept within its allowed range.

        A value edited by hand in the file can be anything; a setting
        outside its range would otherwise reach the code that relies on it.
        """
        return max(minimum, min(maximum, self.get_int(section, key)))

    def _get_default(self, section: str, key: str) -> str:
        """Return the built-in default for a given section/key pair."""
        try:
            return _DEFAULTS[section][key]
        except KeyError:
            return ""
