# SPDX-License-Identifier: MIT
"""Tests for the updater's SemVer version parsing and comparison.

Covers the logic that decides whether to prompt a user for an update.
A bug here either misses a real update or spuriously offers an old
or invalid one — both bad.
"""

from __future__ import annotations

import pytest

from updater.update_manager import _require_https, is_newer, parse_version


class TestParseVersion:
    """SemVer parsing handles valid, v-prefixed, and malformed input."""

    @pytest.mark.parametrize(
        ("input_str", "expected"),
        [
            ("1.0.0", (1, 0, 0)),
            ("0.0.1", (0, 0, 1)),
            ("10.20.30", (10, 20, 30)),
        ],
    )
    def test_basic_parsing(self, input_str, expected):
        assert parse_version(input_str) == expected

    def test_strips_leading_v_lowercase(self):
        assert parse_version("v1.2.3") == (1, 2, 3)

    def test_strips_leading_v_uppercase(self):
        assert parse_version("V1.2.3") == (1, 2, 3)

    def test_strips_surrounding_whitespace(self):
        assert parse_version("  1.2.3  ") == (1, 2, 3)

    def test_malformed_returns_zeros(self):
        assert parse_version("not-a-version") == (0, 0, 0)

    def test_empty_string_returns_zeros(self):
        assert parse_version("") == (0, 0, 0)

    def test_prerelease_suffix_not_supported(self):
        # Explicit statement: SemVer pre-release suffixes (e.g., 1.0.0-beta)
        # are not parsed. They return (0, 0, 0), which is treated as
        # "unknown version" by is_newer — never newer than a real release.
        assert parse_version("1.0.0-beta") == (0, 0, 0)


class TestIsNewer:
    """The comparison used to decide whether to prompt for update."""

    def test_newer_patch(self):
        assert is_newer("1.0.1", "1.0.0") is True

    def test_newer_minor(self):
        assert is_newer("1.1.0", "1.0.9") is True

    def test_newer_major(self):
        assert is_newer("2.0.0", "1.9.9") is True

    def test_same_version(self):
        assert is_newer("1.0.0", "1.0.0") is False

    def test_older_is_not_newer(self):
        assert is_newer("1.0.0", "1.0.1") is False

    def test_v_prefix_handled(self):
        assert is_newer("v1.0.1", "v1.0.0") is True

    def test_mixed_prefix_handled(self):
        assert is_newer("v1.0.1", "1.0.0") is True


class TestRequireHttps:
    """Regression guard for audit finding C1 — HTTPS-only scheme guard."""

    def test_accepts_https(self):
        _require_https("https://example.com/file.exe")  # no exception

    def test_rejects_http(self):
        with pytest.raises(ValueError, match="non-HTTPS"):
            _require_https("http://example.com/evil.exe")

    def test_rejects_file_scheme(self):
        with pytest.raises(ValueError):
            _require_https("file:///etc/passwd")

    def test_rejects_ftp(self):
        with pytest.raises(ValueError):
            _require_https("ftp://example.com/file.exe")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            _require_https("")

    def test_rejects_non_string(self):
        with pytest.raises(ValueError):
            _require_https(None)  # type: ignore[arg-type]
