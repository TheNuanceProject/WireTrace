# SPDX-License-Identifier: MIT
"""WireTrace version and identity — single source of truth for all builds.

This module is the canonical source for all version and identity information.
It is read by:
  - The application at runtime (window title, about dialog, update checks)
  - The build script to stamp version into installers
  - Inno Setup, Info.plist, .desktop files, AppImage YAML

No other module defines version or identity constants.
"""

# ── Application Identity ─────────────────────────────────────────────────────

APP_NAME = "WireTrace"
APP_VERSION = "1.0.0"
APP_DISPLAY_NAME = "WireTrace"
APP_DESCRIPTION = "Professional Serial Data Monitor"
APP_AUTHOR = "The Nuance Project"
APP_DEVELOPER = "Mohamad Shahin Ambalatha Kandy"
APP_WEBSITE = "https://thenuanceproject.com"
APP_ID = "com.thenuanceproject.wiretrace"

# ── Update Server ────────────────────────────────────────────────────────────
# Update check JSON is hosted on the project website (static file).
# Actual binary downloads are served from GitHub Releases (CDN-backed).

UPDATE_BASE_URL = "https://thenuanceproject.com/updates"
UPDATE_JSON_FILE = "wiretrace-update.json"

# GitHub Releases base URL — used by build script to generate download links
RELEASES_BASE_URL = "https://github.com/TheNuanceProject/WireTrace/releases/download"

# ── Derived Constants (used by UI and build scripts) ─────────────────────────

WINDOW_TITLE = f"{APP_NAME} v{APP_VERSION}"
COPYRIGHT = f"© 2026 {APP_AUTHOR}"
UPDATE_JSON_URL = f"{UPDATE_BASE_URL}/{UPDATE_JSON_FILE}"
