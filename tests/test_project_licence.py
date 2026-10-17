# SPDX-License-Identifier: GPL-3.0-or-later
"""The licence WireTrace is distributed under is stated in several
places. These tests hold them to one another."""

from __future__ import annotations

import hashlib
from pathlib import Path

from tools.add_spdx_headers import SPDX_LINE
from version import APP_DEVELOPER, APP_LICENSE, APP_LICENSE_URL

ROOT = Path(__file__).resolve().parent.parent

#: SHA-256 of the text of the GNU General Public License version 3 as
#: the Free Software Foundation publishes it.
_GPL_3_SHA256 = "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986"


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_the_licence_file_is_the_published_text_unchanged() -> None:
    # Compared with the line endings of the published text, whatever
    # this checkout uses.
    text = (ROOT / "LICENSE").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(text).hexdigest() == _GPL_3_SHA256


def test_every_source_file_names_the_licence_the_program_states() -> None:
    assert f"# SPDX-License-Identifier: {APP_LICENSE}" == SPDX_LINE
    assert APP_LICENSE == "GPL-3.0-or-later"
    assert "gnu.org/licenses/gpl-3.0" in APP_LICENSE_URL


def test_the_notice_carries_the_attribution_and_the_licence_notice() -> None:
    notice = _read("NOTICE")
    assert APP_DEVELOPER in notice
    assert "either version 3 of the License, or (at your\noption) any later version" in notice
    assert "Versions 1.0.0 to 1.2.0 were released under the MIT License" in notice


def test_the_documents_name_the_same_licence() -> None:
    for name in ("README.md", "CONTRIBUTING.md"):
        text = _read(name)
        assert "GNU General Public License, version 3 or later" in text, name
        assert "[MIT]" not in text and "MIT License](./LICENSE)" not in text, name


def test_the_code_of_conduct_says_whom_to_write_to() -> None:
    text = _read("CODE_OF_CONDUCT.md")
    assert "INSERT CONTACT METHOD" not in text
    assert "@thenuanceproject.com" in text
