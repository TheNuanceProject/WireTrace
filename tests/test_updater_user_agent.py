# SPDX-License-Identifier: MIT
"""Tests for the update-request User-Agent and install-channel detection.

The update check sends a User-Agent identifying the app and platform:
version, OS, architecture, and install channel. These tests pin the
fully-derived format and the channel logic so it cannot silently
regress to the bare ``WireTrace/<version>`` string.
"""

from __future__ import annotations

import pytest

import updater.update_manager as um
from updater.update_manager import _install_channel, _user_agent
from version import APP_NAME, APP_VERSION


class TestUserAgent:
    def test_format_and_fields(self):
        ua = _user_agent()
        # 'WireTrace/<version> (<os>; <arch>; <channel>)'
        assert ua.startswith(f"{APP_NAME}/{APP_VERSION} (")
        assert ua.endswith(")")
        inside = ua[ua.index("(") + 1: -1]
        parts = [p.strip() for p in inside.split(";")]
        assert len(parts) == 3  # os, arch, channel
        assert all(parts), "no empty UA fields"

    def test_carries_version_os_channel(self, monkeypatch):
        monkeypatch.setattr(um, "get_current_platform", lambda: "linux")
        monkeypatch.setenv("APPIMAGE", "/home/u/WireTrace.AppImage")
        ua = _user_agent()
        assert APP_VERSION in ua
        assert "linux" in ua
        assert "appimage" in ua

    def test_no_bare_legacy_user_agent(self):
        # Regression guard: the old UA was the bare 'WireTrace/<version>'
        # with no platform info. The enriched UA must carry the parenthesised
        # fields, so this is never the whole string again.
        assert _user_agent() != f"{APP_NAME}/{APP_VERSION}"
        assert "(" in _user_agent()


class TestInstallChannel:
    def test_appimage_env_wins(self, monkeypatch):
        monkeypatch.setenv("APPIMAGE", "/somewhere/WireTrace.AppImage")
        assert _install_channel() == "appimage"

    def test_source_when_not_compiled(self, monkeypatch):
        monkeypatch.delenv("APPIMAGE", raising=False)
        # Ensure the Nuitka marker is absent (it is, under the interpreter).
        monkeypatch.delitem(um.__dict__, "__compiled__", raising=False)
        assert _install_channel() == "source"

    @pytest.mark.parametrize(
        ("plat", "expected"),
        [("windows", "exe"), ("linux", "deb"), ("macos", "app")],
    )
    def test_compiled_channel_per_os(self, monkeypatch, plat, expected):
        monkeypatch.delenv("APPIMAGE", raising=False)
        # Simulate a Nuitka standalone build: the module carries __compiled__.
        monkeypatch.setitem(um.__dict__, "__compiled__", True)
        monkeypatch.setattr(um, "get_current_platform", lambda: plat)
        assert _install_channel() == expected
