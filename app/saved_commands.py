# SPDX-License-Identifier: GPL-3.0-or-later
"""Commands the user has saved to send again.

One list for the whole application, kept in ``preferences.ini`` under
``[Commands]`` as a JSON list of texts. The first few have a keyboard
shortcut each. The list never leaves the machine.

This module does not touch the UI or the disk directly: persistence flows
through the injected ``ConfigManager``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

from app.constants import SAVED_COMMAND_MAX_LENGTH, SAVED_COMMANDS_MAX

if TYPE_CHECKING:
    from app.config import ConfigManager

logger = logging.getLogger(__name__)

SECTION = "Commands"
KEY = "saved_json"


def clean(commands: Iterable[object]) -> list[str]:
    """The commands worth keeping: text, trimmed, each once, within the limits."""
    kept: list[str] = []
    for command in commands:
        if not isinstance(command, str):
            continue
        text = command.strip()[:SAVED_COMMAND_MAX_LENGTH]
        if text and text not in kept:
            kept.append(text)
    return kept[:SAVED_COMMANDS_MAX]


class SavedCommands:
    """Reads and changes the saved commands."""

    def __init__(self, config: ConfigManager) -> None:
        self._config = config

    def all(self) -> list[str]:
        """The saved commands, in the order the user gave them."""
        raw = self._config.get(SECTION, KEY, fallback="")
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Saved commands are unreadable and are ignored: %s", exc)
            return []
        return clean(data) if isinstance(data, list) else []

    def can_add(self, command: str) -> bool:
        """True if ``command`` is not empty, not saved yet, and there is room."""
        text = command.strip()
        saved = self.all()
        return bool(text) and text not in saved and len(saved) < SAVED_COMMANDS_MAX

    def add(self, command: str) -> bool:
        """Save ``command`` at the end of the list. False if it was not added."""
        if not self.can_add(command):
            return False
        self.replace([*self.all(), command])
        return True

    def replace(self, commands: Iterable[str]) -> None:
        """Make ``commands`` the whole list."""
        self._config.set(SECTION, KEY, json.dumps(clean(commands), ensure_ascii=False))
