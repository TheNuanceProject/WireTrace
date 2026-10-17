# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace version and identity: single source of truth for all builds.

This module is the canonical source for all version and identity information.
It is read by:
  - The application at runtime (window title, about dialog, update checks)
  - The build script to stamp version into installers
  - build/build.py, which passes it to Nuitka, Inno Setup, the .desktop
    file, the AppImage and the .deb
  - The release tools in tools/

No other module defines version or identity constants.
"""

# ── Application Identity ─────────────────────────────────────────────────────

APP_NAME = "WireTrace"
APP_VERSION = "2.0.0"
APP_DESCRIPTION = "Professional Serial Data Monitor"
APP_AUTHOR = "The Nuance Project"
APP_DEVELOPER = "Mohamad Shahin Ambalatha Kandy"
APP_WEBSITE = "https://thenuanceproject.com"
# The one address for everything about WireTrace: the Linux package's
# maintainer, and questions and requests about privacy. The same address
# as the website's privacy policy (siteConfig.author.email).
APP_MAINTAINER_EMAIL = "shahin@thenuanceproject.com"

# ── Updates ──────────────────────────────────────────────────────────────
# The project website says which version is the latest. The installers
# themselves are downloaded from GitHub Releases.

UPDATE_BASE_URL = "https://thenuanceproject.com/updates"

# The signed manifest that versions from 2.0 read. See updater/manifest.py.
UPDATE_MANIFEST_URL = f"{UPDATE_BASE_URL}/wiretrace/manifest.json"

# Where usage statistics are sent, when the user shares them, and where
# the request to delete them is sent when the user stops. See
# updater/usage.py.
USAGE_REPORT_URL = f"{UPDATE_BASE_URL}/wiretrace/usage"
USAGE_DELETE_URL = f"{UPDATE_BASE_URL}/wiretrace/usage/delete"

# The unsigned manifest that 1.x versions read. This version never reads
# it; the release tools write it so that 1.x users are told about 2.0.
UPDATE_LEGACY_JSON_FILE = "wiretrace-update.json"

# Public keys an update manifest may be signed with: Ed25519, 32 bytes
# each, in base64. Made by tools/release_keys.py.
UPDATE_PUBLIC_KEYS: tuple[str, ...] = (
    "pxwAaNI6sn5Iv4NzBNT+7SheovtKXMqPUZhwmS1lXrY=",
    "5QCvKeBlABrCrN1wIfq5QVTCVFquUVGLBfmhVpnEE8U=",
)

# The oldest version that can update straight to this one. Raise it for
# a release that an older version cannot install over.
UPDATE_MIN_VERSION = "1.0.0"

# Names of the files of a release. The update check accepts no other.
WINDOWS_INSTALLER_NAME = "{name}-Setup-v{version}.exe"
LINUX_APPIMAGE_NAME = "{name}-v{version}-x86_64.AppImage"
LINUX_DEB_NAME = "wiretrace_{version}_amd64.deb"

# The licence the application is distributed under, as an SPDX
# identifier, with where its text is published. Versions up to 1.2.0
# were released under the MIT License.
APP_LICENSE = "GPL-3.0-or-later"
APP_LICENSE_URL = "https://www.gnu.org/licenses/gpl-3.0.html"

# Where the work can be supported: the About dialog's Support button
# opens it. Empty, the dialog shows no Support button.
APP_SUPPORT_URL = "https://github.com/sponsors/shahin-hashim"

# Where the source lives, and where a problem can be reported
APP_REPOSITORY_URL = "https://github.com/TheNuanceProject/WireTrace"
APP_ISSUES_URL = f"{APP_REPOSITORY_URL}/issues"

# Where installers are downloaded from, and the page of one release
RELEASES_BASE_URL = f"{APP_REPOSITORY_URL}/releases/download"
RELEASE_PAGE_URL = f"{APP_REPOSITORY_URL}/releases/tag/v{{version}}"

# What the update check sends, and why, is described here
APP_PRIVACY_URL = f"{APP_WEBSITE}/privacy/"

# ── Derived Constants (used by UI and build scripts) ─────────────────────────

WINDOW_TITLE = f"{APP_NAME} v{APP_VERSION}"
COPYRIGHT = f"© 2026 {APP_AUTHOR}"
