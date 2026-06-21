# SPDX-License-Identifier: MIT
"""Regression tests for B8 — reader/log-engine signals leak on shutdown.

The bug: ``DeviceTab._on_connected`` connects four signals — the relay
``serial_manager.data_received → reader.enqueue_data`` plus three reader
outbound signals (``line_received``, ``rate_updated``, ``error_occurred``)
— and the log engine's ``error_occurred``. ``ordered_shutdown`` only
disconnected the relay. The remaining connection records kept the dead
``SerialReader`` and ``LogEngine`` instances alive, so a session that
disconnects and reconnects many times (the hardware bring-up workflow)
leaked their buffers.

The fix disconnects the three reader signals (after ``stop()``/``wait()``
so the final flush still reaches its slots) and the log engine's error
signal, each guarded with ``contextlib.suppress(RuntimeError, TypeError)``
before the reference is dropped.

``DeviceTab`` pulls in the full widget stack, which the headless test
stubs only partially cover, so — like the other UI lifecycle tests in
this suite — we don't construct the widget. Instead we make the real
class importable under the stubs (a stub ``pyqtgraph`` plus a catch-all
on the stub Qt modules) and call the genuine ``ordered_shutdown`` against
a minimal fake ``self``. With real PySide6 present, no shim is applied and
the real module imports directly.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

_qtwidgets = sys.modules.get("PySide6.QtWidgets")
_is_stub_env = "conftest" in getattr(
    getattr(_qtwidgets, "QWidget", None), "__module__", "",
)

if _is_stub_env:
    # Headless stubs: pyqtgraph isn't importable and the stub Qt modules
    # define only a subset of widgets. Provide a stub pyqtgraph and a
    # catch-all so the real DeviceTab and its widget dependencies import.
    # ordered_shutdown touches none of these widgets — they only need to
    # exist as importable stand-ins.
    if "pyqtgraph" not in sys.modules:
        sys.modules["pyqtgraph"] = types.ModuleType("pyqtgraph")
    _base = _qtwidgets.QWidget
    for _modname in ("PySide6.QtWidgets", "PySide6.QtGui", "PySide6.QtCore"):
        _mod = sys.modules[_modname]
        if not hasattr(_mod, "__getattr__"):
            _mod.__getattr__ = (lambda b: lambda name: b)(_base)

from ui.device_tab import DeviceTab  # noqa: E402 - imported after the stub shim above


class _FakeSignal:
    """Records the slots passed to disconnect()."""

    def __init__(self) -> None:
        self.disconnect_calls: list[object] = []

    def disconnect(self, slot: object) -> None:
        self.disconnect_calls.append(slot)


class _FakeReader:
    def __init__(self) -> None:
        self.enqueue_data = object()  # identity used by the relay disconnect
        self.line_received = _FakeSignal()
        self.rate_updated = _FakeSignal()
        self.error_occurred = _FakeSignal()
        self.events: list[str] = []

    def stop(self) -> None:
        self.events.append("stop")

    def wait(self, _ms: int) -> None:
        self.events.append("wait")


class _FakeLogEngine:
    def __init__(self, is_logging: bool = False) -> None:
        self.is_logging = is_logging
        self.error_occurred = _FakeSignal()
        self.events: list[str] = []

    def stop_logging(self) -> None:
        self.events.append("stop_logging")

    def stop(self) -> None:
        self.events.append("stop")

    def wait(self, _ms: int) -> None:
        self.events.append("wait")


def _make_fake_self(reader, log_engine):
    relay = _FakeSignal()  # serial_manager.data_received
    fake = SimpleNamespace(
        _serial_reader=reader,
        _log_engine=log_engine,
        _csv_engine=object(),
        _serial_manager=SimpleNamespace(data_received=relay, close=lambda: None),
        _plot_view=None,
        _plot_engine=SimpleNamespace(reset=lambda: None),
        _suppress_disconnect_ui=False,
        _session=SimpleNamespace(
            port_name="/dev/ttyACM0",
            reset_connection_state=lambda: None,
        ),
        _on_line_received=lambda *a: None,
        _on_rate_updated=lambda *a: None,
        _on_serial_error=lambda *a: None,
        _on_log_error=lambda *a: None,
    )
    return fake, relay


class TestReaderSignalsDisconnected:
    def test_all_three_reader_signals_disconnected(self):
        reader = _FakeReader()
        fake, _relay = _make_fake_self(reader, None)

        DeviceTab.ordered_shutdown(fake)

        assert reader.line_received.disconnect_calls == [fake._on_line_received]
        assert reader.rate_updated.disconnect_calls == [fake._on_rate_updated]
        assert reader.error_occurred.disconnect_calls == [fake._on_serial_error]
        assert fake._serial_reader is None

    def test_relay_still_disconnected(self):
        reader = _FakeReader()
        fake, relay = _make_fake_self(reader, None)

        DeviceTab.ordered_shutdown(fake)

        assert relay.disconnect_calls == [reader.enqueue_data]

    def test_outbound_disconnect_after_stop_and_wait(self):
        # The reader must be stopped and joined before its outbound signals
        # are disconnected, so the final flush reaches the slots.
        reader = _FakeReader()
        fake, _relay = _make_fake_self(reader, None)

        DeviceTab.ordered_shutdown(fake)

        assert reader.events == ["stop", "wait"]


class TestLogEngineSignalDisconnected:
    def test_log_error_signal_disconnected(self):
        log_engine = _FakeLogEngine(is_logging=False)
        fake, _relay = _make_fake_self(_FakeReader(), log_engine)

        DeviceTab.ordered_shutdown(fake)

        assert log_engine.error_occurred.disconnect_calls == [fake._on_log_error]
        assert fake._log_engine is None

    def test_logging_stopped_before_disconnect_when_active(self):
        log_engine = _FakeLogEngine(is_logging=True)
        fake, _relay = _make_fake_self(_FakeReader(), log_engine)

        DeviceTab.ordered_shutdown(fake)

        assert log_engine.events == ["stop_logging", "stop", "wait"]
        assert log_engine.error_occurred.disconnect_calls == [fake._on_log_error]


class TestNoComponents:
    def test_shutdown_safe_when_nothing_connected(self):
        fake, _relay = _make_fake_self(None, None)
        # Should not raise when reader and log engine are absent.
        DeviceTab.ordered_shutdown(fake)
        assert fake._serial_reader is None
        assert fake._log_engine is None
