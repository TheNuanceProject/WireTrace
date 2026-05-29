# SPDX-License-Identifier: MIT
"""Regression tests for the console font-size flow.

The bug: on first connect the console rendered at one size (small),
and any later Preferences-save or Ctrl+0 made it jump to a larger
size. Root cause: ConsoleView.__init__ set the font via
``QFont(family, pt_size)`` which is overridden by the global QSS
rule ``QWidget { font-size: 12px }`` for inheriting widgets, while
later actions called ``set_font_size`` which uses
``QFont.setPointSize`` — a path the QSS rule does NOT override.

The fix: ``__init__`` calls ``set_font_size(DEFAULT_FONT_SIZE)`` so
the initial render and every later size change share one code path.
No QSS-vs-code-set ambiguity, no jump.

The QFont/QPainter rendering chain can't run under headless stubs,
but the call-path invariant is what we actually need to lock down:
**ConsoleView.__init__ must route through ``set_font_size`` so launch
and later actions cannot diverge.**
"""

from __future__ import annotations


class TestConsoleFontInitFlow:
    """Pin the call-path contract: __init__ must call set_font_size
    with DEFAULT_FONT_SIZE so the same code path owns the font on
    launch and on every user action."""

    def test_init_calls_set_font_size_with_default(self):
        """REGRESSION: if anyone removes the set_font_size call from
        __init__, the font-jump bug returns.

        We inspect the source of __init__ directly rather than running
        it. This is robust against headless-stub limitations (the real
        QPlainTextEdit surface is too large to stub usefully) and
        catches the exact regression: ``set_font_size(DEFAULT_FONT_SIZE)``
        must appear in the constructor.
        """
        import inspect

        from ui.widgets.console_view import ConsoleView

        source = inspect.getsource(ConsoleView.__init__)
        assert "set_font_size(DEFAULT_FONT_SIZE)" in source, (
            "ConsoleView.__init__ no longer calls "
            "set_font_size(DEFAULT_FONT_SIZE). The initial font will "
            "then be governed by the global QSS rule "
            "`QWidget { font-size: 12px }`, while later actions "
            "(Ctrl+0, Ctrl++, Preferences save) use setPointSize. "
            "This is the exact bug from the v1.1.0 verification: "
            "small on launch, jumps bigger on first action. "
            "Restore the set_font_size(DEFAULT_FONT_SIZE) call."
        )

    def test_default_font_size_constant_is_sane(self):
        """If DEFAULT_FONT_SIZE drifts to something silly, both the
        launch state and the post-action state would be silly together
        — same code path, but a bad value. This is a guard against
        the constant drifting."""
        from app.constants import DEFAULT_FONT_SIZE, FONT_SIZE_MAX, FONT_SIZE_MIN
        assert FONT_SIZE_MIN <= DEFAULT_FONT_SIZE <= FONT_SIZE_MAX
        # Comfortable monospace range for a serial console
        assert 9 <= DEFAULT_FONT_SIZE <= 16

    def test_set_font_size_uses_set_point_size(self):
        """The whole point of the fix is that set_font_size routes
        through QFont.setPointSize, which beats the global QSS px
        rule. Confirm that's still the implementation."""
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

        view = FakeView()
        # Call the unbound method directly with our fake instance
        ConsoleView.set_font_size(view, 14)

        assert recorded["point_size"] == 14, \
            "set_font_size must use setPointSize (pt), not setPixelSize " \
            "(px). The pt path beats the global QSS px rule; the px " \
            "path would lose to it on widget construction."
        assert recorded["pixel_size"] is None, \
            "set_font_size accidentally called setPixelSize — that would " \
            "leave the widget vulnerable to QSS px rules on the next " \
            "stylesheet refresh."
        assert view.set_font_called_with is not None, \
            "set_font_size must apply the modified font via setFont"
