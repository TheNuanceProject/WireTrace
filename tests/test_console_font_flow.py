# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests that the console's font size has one code path.

The console's constructor sets its size through ``set_font_size``, the
same method every later change uses (the View menu, the shortcuts,
Preferences). If the constructor set the font another way, the size at
launch and the size after the first change could differ.

The drawing itself cannot run against the Qt stand-ins, so the test
holds the constructor to that call.
"""

from __future__ import annotations


class TestConsoleFontInitFlow:
    """The constructor calls set_font_size with DEFAULT_FONT_SIZE, so one
    code path owns the font at launch and on every user action."""

    def test_init_calls_set_font_size_with_default(self):
        """The source of the constructor is inspected rather than run:
        the widget is too large to run against the Qt stand-ins."""
        import inspect

        from ui.widgets.console_view import ConsoleView

        source = inspect.getsource(ConsoleView.__init__)
        assert "set_font_size(DEFAULT_FONT_SIZE)" in source, (
            "ConsoleView.__init__ does not call "
            "set_font_size(DEFAULT_FONT_SIZE). The size at launch would "
            "then be set by another path than later changes, and the "
            "console could change size on the first action."
        )

    def test_default_font_size_constant_is_sane(self):
        """If DEFAULT_FONT_SIZE drifts to something silly, both the
        launch state and the post-action state would be silly together
       : same code path, but a bad value. This is a guard against
        the constant drifting."""
        from app.constants import DEFAULT_FONT_SIZE, FONT_SIZE_MAX, FONT_SIZE_MIN
        assert FONT_SIZE_MIN <= DEFAULT_FONT_SIZE <= FONT_SIZE_MAX
        # Comfortable monospace range for a serial console
        assert 9 <= DEFAULT_FONT_SIZE <= 16

    def test_set_font_size_sets_logical_pixels(self):
        """The size is in logical pixels, as every size of the design
        language is: it follows the display's scaling and is the same
        on every operating system, where points are not (a point is a
        pixel on macOS and a third more on Windows)."""
        from ui.widgets.console_view import ConsoleView

        # Build a tiny stand-in that has just the surface set_font_size
        # touches. Record which font mutator was called.
        recorded = {"point_size": None, "pixel_size": None}

        class FakeFont:
            def setPointSize(self, n):
                recorded["point_size"] = n

            def setPixelSize(self, n):
                recorded["pixel_size"] = n

        class FakeView:
            def __init__(self):
                self._font = FakeFont()
                self.set_font_called_with = None

            def font(self):
                return self._font

            def setFont(self, f):
                self.set_font_called_with = f

            def _apply_font(self):
                self.geometry_recomputed = True

        view = FakeView()
        # Call the unbound method directly with our fake instance
        ConsoleView.set_font_size(view, 14)

        assert recorded["pixel_size"] == 14
        assert recorded["point_size"] is None
        assert view.set_font_called_with is not None, \
            "set_font_size must apply the modified font via setFont"
        assert view.geometry_recomputed, \
            "set_font_size must recompute the row geometry for the new font"

    def test_no_stylesheet_rule_sets_the_console_text_size(self):
        """A text size in the stylesheet that reached the console would
        override the size the user chose."""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        template = (root / "ui" / "themes" / "wiretrace.qss.template").read_text(encoding="utf-8")
        template = re.sub(r"/\*.*?\*/", "", template, flags=re.S)
        reaching_the_console = {"*", "QWidget", "QAbstractScrollArea", "QAbstractScrollArea#consoleView"}
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", template):
            if "font-size" in body:
                names = {part.strip() for part in selector.split(",")}
                assert not names & reaching_the_console, selector
