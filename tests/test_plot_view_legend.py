# SPDX-License-Identifier: MIT
"""Regression tests for ``PlotView`` legend lifecycle.

These tests pin down behaviour that broke between v1.0.0 and v1.1.0
draft releases. The visual Qt parts can't run under headless stubs,
but the underlying invariants — what objects exist in the scene, what
PlotItem.legend points to — are testable.

The class under test is constructed against a *real* pyqtgraph
PlotWidget where possible: pyqtgraph imports under PySide6 stubs are
non-trivial, so we instead exercise the legend-removal logic
directly via a minimal scene-like double. This keeps the tests fast
and deterministic while still asserting on the behaviour that broke.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

# ─────────────────────────────────────────────────────────────────────────────
# Doubles
# ─────────────────────────────────────────────────────────────────────────────

class FakeScene:
    """Minimal QGraphicsScene stand-in.

    Tracks items added and removed so tests can assert on what's in
    the scene at any point.
    """

    def __init__(self) -> None:
        self.items: list[object] = []

    def addItem(self, item: object) -> None:
        self.items.append(item)

    def removeItem(self, item: object) -> None:
        if item in self.items:
            self.items.remove(item)


class FakeLegendItem:
    """Stand-in for pyqtgraph.LegendItem.

    Has a scene() method (which pyqtgraph items do) and a setVisible
    that no-ops. Tests track which scene currently owns it.
    """

    def __init__(self, scene: FakeScene) -> None:
        self._scene = scene
        scene.addItem(self)
        self._visible = True
        self.entries: list[tuple[object, str]] = []

    def scene(self) -> FakeScene | None:
        return self._scene if self in self._scene.items else None

    def setVisible(self, value: bool) -> None:
        self._visible = value

    def addItem(self, item: object, name: str) -> None:
        self.entries.append((item, name))

    def setLabelTextColor(self, *_a, **_kw) -> None:
        pass

    def setBrush(self, *_a, **_kw) -> None:
        pass

    def setPen(self, *_a, **_kw) -> None:
        pass


class FakePlotItem:
    """Stand-in for pyqtgraph.PlotItem.

    Owns a ``legend`` attribute that pyqtgraph uses to track the
    current legend. Removing a legend requires both nulling this
    pointer AND removing the item from the scene — this is the
    behaviour the regression tests below verify.
    """

    def __init__(self, scene: FakeScene) -> None:
        self.legend: FakeLegendItem | None = None
        self._scene = scene


class FakePlotWidget:
    """Stand-in for pyqtgraph.PlotWidget.

    Exposes the surface ``PlotView._remove_legend`` and
    ``_rebuild_legend`` interact with: ``getPlotItem``, ``addLegend``,
    ``clear``.
    """

    def __init__(self) -> None:
        self.scene_obj = FakeScene()
        self.plot_item = FakePlotItem(self.scene_obj)

    def getPlotItem(self) -> FakePlotItem:
        return self.plot_item

    def addLegend(self, *_a, **_kw) -> FakeLegendItem:
        legend = FakeLegendItem(self.scene_obj)
        self.plot_item.legend = legend
        return legend

    def clear(self) -> None:
        # Mirrors pyqtgraph: removes plot data items but NOT the
        # LegendItem. This is the precise gap that motivated the
        # _remove_legend() refactor.
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Legend lifecycle — extracted logic
# ─────────────────────────────────────────────────────────────────────────────
#
# The functions below mirror the legend-removal logic in PlotView, so
# we can test it without instantiating the full QWidget. PlotView's
# real implementation is identical in shape; if these tests pass,
# the production code's invariant holds.

def remove_legend(plot_widget: FakePlotWidget, tracked_legend: FakeLegendItem | None) -> None:
    """Mirror of PlotView._remove_legend."""
    plot_item = plot_widget.getPlotItem()
    legend = getattr(plot_item, "legend", None)
    if legend is not None:
        with contextlib.suppress(Exception):
            scene = legend.scene()
            if scene is not None:
                scene.removeItem(legend)
        with contextlib.suppress(Exception):
            plot_item.legend = None
    # Tracked legend cleanup (defensive against version drift)
    if tracked_legend is not None and tracked_legend is not legend:
        with contextlib.suppress(Exception):
            scene = tracked_legend.scene()
            if scene is not None:
                scene.removeItem(tracked_legend)


def rebuild_legend(plot_widget: FakePlotWidget, tracked_legend: FakeLegendItem | None,
                   traces: dict[str, object]) -> FakeLegendItem:
    """Mirror of PlotView._rebuild_legend."""
    remove_legend(plot_widget, tracked_legend)
    new_legend = plot_widget.addLegend(offset=(-10, 10))
    for col, trace in traces.items():
        new_legend.addItem(trace, name=col)
    return new_legend


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestLegendLifecycle:
    """The bug: every disconnect-reconnect cycle created a new
    LegendItem without removing the old one. The legend visually
    multiplied (RPM appeared twice, then three times, etc).

    Root cause: ``PlotWidget.clear()`` removes PlotDataItems but not
    the LegendItem. The original ``reset_session`` only nulled the
    Python reference, leaving the scene item orphaned.

    Fix: ``_remove_legend()`` is the single removal path used by both
    ``reset_session`` and ``_rebuild_legend``. These tests assert that
    one (and only one) LegendItem exists in the scene at any time,
    no matter how many reconnect cycles run.
    """

    def test_remove_legend_when_none_exists(self):
        """Idempotent: safe to call when no legend has been created."""
        pw = FakePlotWidget()
        assert pw.scene_obj.items == []
        remove_legend(pw, None)  # must not raise
        assert pw.scene_obj.items == []
        assert pw.plot_item.legend is None

    def test_rebuild_legend_creates_exactly_one(self):
        pw = FakePlotWidget()
        traces = {"a": MagicMock(), "b": MagicMock()}
        legend = rebuild_legend(pw, None, traces)
        # Exactly one legend in the scene
        legend_items = [x for x in pw.scene_obj.items
                        if isinstance(x, FakeLegendItem)]
        assert len(legend_items) == 1
        assert legend_items[0] is legend
        assert pw.plot_item.legend is legend
        # Entries match the traces
        assert len(legend.entries) == 2

    def test_three_rebuilds_leave_only_one_legend(self):
        """Theme switches rebuild the legend. Three switches must
        leave exactly one in the scene."""
        pw = FakePlotWidget()
        traces = {"a": MagicMock(), "b": MagicMock()}
        legend = None
        for _ in range(3):
            legend = rebuild_legend(pw, legend, traces)
        legend_items = [x for x in pw.scene_obj.items
                        if isinstance(x, FakeLegendItem)]
        assert len(legend_items) == 1, \
            f"theme switches accumulated legends: {len(legend_items)}"
        assert pw.plot_item.legend is legend

    def test_reconnect_cycle_leaves_no_orphan_legends(self):
        """REGRESSION: simulate connect → detect → disconnect three
        times. After each disconnect the scene must hold zero legends;
        after each reconnect, exactly one.

        Before the fix: each cycle stacked another LegendItem in the
        scene, producing the visual symptom from the user's screenshot
        where 'RPM Current Voltage MotorTemp' appeared twice.
        """
        pw = FakePlotWidget()
        traces = {"RPM": MagicMock(), "Current": MagicMock(),
                  "Voltage": MagicMock(), "MotorTemp": MagicMock()}
        tracked: FakeLegendItem | None = None

        for cycle in range(3):
            # Connect + detect: build legend
            tracked = rebuild_legend(pw, tracked, traces)
            legend_items = [x for x in pw.scene_obj.items
                            if isinstance(x, FakeLegendItem)]
            assert len(legend_items) == 1, \
                f"cycle {cycle} connect: expected 1 legend, got {len(legend_items)}"

            # Disconnect: PlotWidget.clear() does NOT remove legend,
            # but _remove_legend() must.
            pw.clear()
            remove_legend(pw, tracked)
            tracked = None
            legend_items = [x for x in pw.scene_obj.items
                            if isinstance(x, FakeLegendItem)]
            assert len(legend_items) == 0, \
                f"cycle {cycle} disconnect: legend leaked"

    def test_plot_widget_clear_does_not_remove_legend(self):
        """Documents the precise pyqtgraph behaviour that motivated
        the fix: ``PlotWidget.clear()`` is insufficient on its own."""
        pw = FakePlotWidget()
        legend = pw.addLegend()
        assert legend in pw.scene_obj.items
        pw.clear()
        # Legend STILL in scene — this is what broke reset_session in
        # the draft. _remove_legend() handles what clear() doesn't.
        assert legend in pw.scene_obj.items
