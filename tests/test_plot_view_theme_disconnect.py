# SPDX-License-Identifier: MIT
"""Regression tests for B7 — PlotView leaks via undisconnected theme_changed.

The bug: ``PlotView.__init__`` connected ``theme_manager.theme_changed``
to ``self._on_theme_changed``, but ``PlotView.shutdown`` only stopped the
redraw timer — it never disconnected. Because the long-lived
``theme_manager`` holds the connection, it retained a reference to the
closed PlotView: memory grew by one PlotView per tab close, and a theme
change after a close could invoke the slot on a partially-destroyed
widget (Qt warning at best, crash at worst).

The fix disconnects ``theme_changed`` in ``shutdown`` before stopping the
timer, guarded by ``contextlib.suppress(RuntimeError, TypeError)`` and the
same defensive ``getattr`` the connect path uses.

``PlotView`` pulls in pyqtgraph, which is not importable under the
headless test stubs, so — like the other plot_view tests — we avoid a
full widget construction. Here we inject a stub ``pyqtgraph`` module so
the real class imports, then call the genuine ``shutdown`` method against
a minimal fake ``self``. This exercises the production code (not a copy)
while staying independent of Qt and pyqtgraph internals.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

# plot_view does ``import pyqtgraph as pg`` at module load. Provide a stub
# so the import succeeds under the headless stubs; shutdown never touches
# pyqtgraph, so an empty module is sufficient.
if "pyqtgraph" not in sys.modules:  # pragma: no cover - import shim
    sys.modules["pyqtgraph"] = types.ModuleType("pyqtgraph")

from ui.widgets.plot_view import PlotView


class _FakeSignal:
    """Records disconnect calls; can be configured to raise."""

    def __init__(self, raises: type[BaseException] | None = None) -> None:
        self.disconnect_calls: list[object] = []
        self._raises = raises

    def disconnect(self, slot: object) -> None:
        self.disconnect_calls.append(slot)
        if self._raises is not None:
            raise self._raises("simulated disconnect failure")


class _FakeTimer:
    def __init__(self) -> None:
        self.stop_calls = 0

    def stop(self) -> None:
        self.stop_calls += 1


def _make_fake_self(theme_manager: object, order: list[str]):
    """Minimal fake exposing what shutdown touches, recording call order."""
    timer = _FakeTimer()

    def stop() -> None:
        order.append("timer.stop")
        timer.stop()

    return SimpleNamespace(
        _theme_manager=theme_manager,
        _on_theme_changed=lambda *_: None,
        _timer=SimpleNamespace(stop=stop),
    )


class TestShutdownDisconnects:
    def test_disconnects_theme_changed_then_stops_timer(self):
        order: list[str] = []
        signal = _FakeSignal()

        def recording_disconnect(slot):
            order.append("disconnect")
            signal.disconnect(slot)

        tm = SimpleNamespace(theme_changed=SimpleNamespace(disconnect=recording_disconnect))
        fake = _make_fake_self(tm, order)

        PlotView.shutdown(fake)

        # Disconnect happens before the timer is stopped.
        assert order == ["disconnect", "timer.stop"]
        assert signal.disconnect_calls == [fake._on_theme_changed]

    def test_disconnect_passes_the_bound_slot(self):
        signal = _FakeSignal()
        tm = SimpleNamespace(theme_changed=signal)
        order: list[str] = []
        fake = _make_fake_self(tm, order)

        PlotView.shutdown(fake)

        assert signal.disconnect_calls == [fake._on_theme_changed]


class TestShutdownTimerAlwaysStops:
    def test_timer_stops_when_theme_manager_has_no_signal(self):
        order: list[str] = []
        # theme_manager without a theme_changed attribute (defensive path).
        tm = SimpleNamespace()
        fake = _make_fake_self(tm, order)

        PlotView.shutdown(fake)

        assert order == ["timer.stop"]

    def test_timer_stops_when_disconnect_raises_runtimeerror(self):
        order: list[str] = []
        signal = _FakeSignal(raises=RuntimeError)

        def recording_disconnect(slot):
            order.append("disconnect")
            signal.disconnect(slot)

        tm = SimpleNamespace(theme_changed=SimpleNamespace(disconnect=recording_disconnect))
        fake = _make_fake_self(tm, order)

        # RuntimeError (already disconnected / C++ object gone) is
        # suppressed; the timer still stops.
        PlotView.shutdown(fake)

        assert order == ["disconnect", "timer.stop"]

    def test_timer_stops_when_disconnect_raises_typeerror(self):
        order: list[str] = []
        signal = _FakeSignal(raises=TypeError)

        def recording_disconnect(slot):
            order.append("disconnect")
            signal.disconnect(slot)

        tm = SimpleNamespace(theme_changed=SimpleNamespace(disconnect=recording_disconnect))
        fake = _make_fake_self(tm, order)

        PlotView.shutdown(fake)

        assert order == ["disconnect", "timer.stop"]
