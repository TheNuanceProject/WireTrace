# SPDX-License-Identifier: GPL-3.0-or-later
"""The names of the pages of Preferences, in the order of its list.

Kept apart from the dialog so that what refers to a page by name (the
tour) does not load the dialog to do so.
"""

from __future__ import annotations

APPEARANCE = "Appearance"
CONSOLE = "Console"
CONNECTION = "Connection"
LOGGING = "Logging"
FEATURES = "Features"
PLOT = "Plot"
ALERTS = "Alerts"
HIGHLIGHTS = "Highlights"
UPDATES_AND_PRIVACY = "Updates and privacy"
ADVANCED = "Advanced"

#: The pages, in the order of the list. Advanced comes after a divider.
PAGES: tuple[str, ...] = (
    APPEARANCE, CONSOLE, CONNECTION, LOGGING, FEATURES, PLOT, ALERTS, HIGHLIGHTS,
    UPDATES_AND_PRIVACY, ADVANCED,
)
PAGE_AFTER_DIVIDER = ADVANCED
