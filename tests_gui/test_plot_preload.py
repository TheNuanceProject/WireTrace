# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for ui.features.plot_preload: the plot loaded before its first use."""

from __future__ import annotations

import sys

import ui.features.plot_preload as plot_preload
from tests_gui.support import wait_until

_STAND_IN = "tests_gui.fake_plot_module"


def test_the_plot_is_loaded_once_on_a_thread_of_its_own(qapp, monkeypatch):
    monkeypatch.setattr(plot_preload, "PLOT_MODULE", _STAND_IN)
    monkeypatch.setattr(plot_preload, "_thread", None)
    monkeypatch.delitem(sys.modules, _STAND_IN, raising=False)

    plot_preload.preload_plot()
    first = plot_preload._thread
    plot_preload.preload_plot()

    assert plot_preload._thread is first
    assert not plot_preload.plot_loaded()
    wait_until(qapp, plot_preload.plot_loaded)
    assert sys.modules[_STAND_IN].LOADED_ON == "wiretrace-plot-preload"


def test_a_plot_already_loaded_starts_no_thread(qapp, monkeypatch):
    monkeypatch.setattr(plot_preload, "_thread", None)
    import ui.widgets.plot_view  # noqa: F401

    plot_preload.preload_plot()

    assert plot_preload._thread is None
    assert plot_preload.plot_loaded()
