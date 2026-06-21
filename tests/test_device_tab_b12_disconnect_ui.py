# SPDX-License-Identifier: MIT
"""Regression tests for B12 — tab UI stuck "connected" after a device drop.

The bug: when a device dropped (USB unplug → ResourceError → B1 closes the
port → manager emits ``disconnected``), only ``_on_disconnected`` ran, and
it merely updated the status-bar text. The full reset (revert the
Connect/Disconnect button, clear the port field, set is_connected False,
stop the reader/log threads, show the "device lost" dialog) lived only in
the user-button handler ``_on_disconnect``. So an unplug left the tab
looking connected. A second defect: ``_on_serial_error`` decided "fatal"
by substring-matching the error text, and Qt's "Input/output error" did
not contain the "i/o" token, so even that fallback never fired.

The fix makes the manager's ``disconnected`` signal the single
authoritative trigger for a guarded teardown shared by every disconnect
(user button, device drop). The user button just requests the close;
``_on_serial_error`` decides to tear down from the manager's real
``is_open()`` state, not error strings. Programmatic shutdowns (tab close,
app quit) suppress the live-disconnect dialog.

``DeviceTab`` pulls in the full widget stack, so — as with the other UI
lifecycle tests here — we don't construct the widget; we call the real
methods against a minimal fake ``self``.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

_qtwidgets = sys.modules.get("PySide6.QtWidgets")
_is_stub_env = "conftest" in getattr(
    getattr(_qtwidgets, "QWidget", None), "__module__", "",
)

if _is_stub_env:
    if "pyqtgraph" not in sys.modules:
        sys.modules["pyqtgraph"] = types.ModuleType("pyqtgraph")
    _base = _qtwidgets.QWidget
    for _modname in ("PySide6.QtWidgets", "PySide6.QtGui", "PySide6.QtCore"):
        _mod = sys.modules[_modname]
        if not hasattr(_mod, "__getattr__"):
            _mod.__getattr__ = (lambda b: lambda name: b)(_base)

import ui.device_tab as device_tab_mod  # noqa: E402 - after the stub shim
from ui.device_tab import DeviceTab  # noqa: E402 - after the stub shim


@pytest.fixture(autouse=True)
def _stub_toast(monkeypatch):
    # Toast is a real widget that treats its first arg as a Qt parent;
    # our fake self isn't one. The disconnect dialog is stubbed via the
    # fake's _show_unexpected_disconnect_dialog, so only Toast needs this.
    monkeypatch.setattr(device_tab_mod, "Toast", SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        success=lambda *a, **k: None,
        error=lambda *a, **k: None,
    ))


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *args) -> None:
        self.calls.append(args)


class _Session:
    def __init__(self) -> None:
        self.port_name = "/dev/ttyACM0"
        self.is_connected = True
        self.is_logging = False
        self.reset_calls = 0

    def reset_connection_state(self) -> None:
        self.reset_calls += 1
        self.is_connected = False
        self.port_name = ""


def _make_fake(*, is_open: bool):
    """A fake DeviceTab self exercising the real disconnect handlers.

    ``ordered_shutdown`` is replaced by a recorder here — its own
    behaviour is covered by the B8 tests; what B12 verifies is that the
    teardown is invoked and the UI/session reset happens. The recorder
    also flips ``is_connected`` False, mirroring the real
    ordered_shutdown's step-6 ``reset_connection_state``.
    """
    session = _Session()
    events: list[str] = []

    def fake_ordered_shutdown():
        events.append("ordered_shutdown")
        session.reset_connection_state()

    fake = SimpleNamespace(
        _disconnecting=False,
        _user_initiated_disconnect=False,
        _suppress_disconnect_ui=False,
        _session=session,
        _serial_manager=SimpleNamespace(
            is_open=lambda: is_open,
            close=_Recorder(),
        ),
        _connection_panel=SimpleNamespace(set_connected=_Recorder()),
        _status_bar=SimpleNamespace(
            set_disconnected=_Recorder(),
            set_status=_Recorder(),
        ),
        _set_connected_ui_visible=_Recorder(),
        title_changed=SimpleNamespace(emit=_Recorder()),
        connection_changed=SimpleNamespace(emit=_Recorder()),
        ordered_shutdown=fake_ordered_shutdown,
        _show_unexpected_disconnect_dialog=_Recorder(),
        _events=events,
    )
    # Bind the REAL teardown to the fake so _on_disconnected / _on_serial_error
    # exercise it (ordered_shutdown stays a recorder — B8 covers the real one).
    fake._teardown_after_disconnect = (
        lambda port_name, *, user_initiated: DeviceTab._teardown_after_disconnect(
            fake, port_name, user_initiated=user_initiated,
        )
    )
    return fake


class TestBackendDisconnectResetsUI:
    def test_device_drop_resets_panel_and_session(self):
        fake = _make_fake(is_open=True)
        # Simulate the manager emitting ``disconnected`` after a drop.
        DeviceTab._on_disconnected(fake, "/dev/ttyACM0")

        assert fake._events == ["ordered_shutdown"]            # threads torn down
        assert fake._session.is_connected is False             # state reset
        assert fake._connection_panel.set_connected.calls == [(False,)]
        assert fake._set_connected_ui_visible.calls == [(False,)]
        assert fake.connection_changed.emit.calls == [(False,)]

    def test_device_drop_shows_unexpected_dialog(self):
        fake = _make_fake(is_open=True)
        DeviceTab._on_disconnected(fake, "/dev/ttyACM0")
        # Not user-initiated → the "device lost" dialog must be shown.
        assert len(fake._show_unexpected_disconnect_dialog.calls) == 1

    def test_user_disconnect_is_quiet(self):
        fake = _make_fake(is_open=True)
        fake._user_initiated_disconnect = True
        DeviceTab._on_disconnected(fake, "/dev/ttyACM0")
        # User asked for it → reset happens, but no alarming dialog.
        assert fake._connection_panel.set_connected.calls == [(False,)]
        assert fake._show_unexpected_disconnect_dialog.calls == []


class TestSuppressionForProgrammaticShutdown:
    def test_suppressed_disconnect_does_nothing(self):
        # During tab close / app quit, ordered_shutdown sets this flag, so
        # the port-close's disconnected signal must not run the teardown.
        fake = _make_fake(is_open=True)
        fake._suppress_disconnect_ui = True
        DeviceTab._on_disconnected(fake, "/dev/ttyACM0")

        assert fake._events == []
        assert fake._connection_panel.set_connected.calls == []
        assert fake._show_unexpected_disconnect_dialog.calls == []


class TestSerialErrorUsesManagerState:
    def test_error_with_closed_port_tears_down(self):
        # The manager is no longer open → the error took the link down;
        # tear down regardless of the error text (no keyword matching).
        fake = _make_fake(is_open=False)
        DeviceTab._on_serial_error(fake, "Input/output error")
        assert fake._events == ["ordered_shutdown"]
        assert len(fake._show_unexpected_disconnect_dialog.calls) == 1

    def test_error_with_open_port_is_transient(self):
        # Manager still open → transient error, no teardown.
        fake = _make_fake(is_open=True)
        DeviceTab._on_serial_error(fake, "some recoverable glitch")
        assert fake._events == []
        assert fake._session.is_connected is True

    def test_error_after_already_disconnected_is_noop(self):
        # Drop path: _on_disconnected already ran (is_connected False), so
        # the trailing error_occurred must not double-report.
        fake = _make_fake(is_open=False)
        fake._session.is_connected = False
        DeviceTab._on_serial_error(fake, "Input/output error")
        assert fake._events == []


class TestTeardownRunsOnce:
    def test_reentrancy_guard(self):
        # If teardown is somehow entered twice, the second is a no-op.
        fake = _make_fake(is_open=True)
        fake._disconnecting = True  # pretend a teardown is in progress
        DeviceTab._teardown_after_disconnect(fake, "/dev/ttyACM0", user_initiated=False)
        assert fake._events == []
        assert fake._connection_panel.set_connected.calls == []
