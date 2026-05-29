# SPDX-License-Identifier: MIT
"""Pytest shared setup.

Installs lightweight stubs for PySide6 so that the pure-Python logic
in ``app``, ``core``, and ``updater`` can be tested without requiring
the Qt binary stack in CI.

The stubs cover only what the modules under test actually import. If
a test exercises a Qt-specific widget or signal semantics, write it
as a pytest-qt test instead — don't extend these stubs further.
"""

from __future__ import annotations

import contextlib
import sys
import types
from pathlib import Path

# ── Make the project root importable ─────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── PySide6 stubs ────────────────────────────────────────────────────
# Install once, at collection time, before any test module imports.

def _install_pyside6_stubs() -> None:
    if "PySide6.QtCore" in sys.modules:
        return  # already installed (e.g., real PySide6 on a dev machine)

    class _Base:
        """Base stub — accepts any init args, exposes any attribute."""

        def __init__(self, *args, **kwargs):
            pass

        def __getattr__(self, name):
            # Return a no-op callable for any method we haven't stubbed.
            return lambda *a, **k: None

    class _Signal:
        """Stand-in for Qt's Signal descriptor.

        Real Qt signals dispatch to connected slots; the original stub
        was a no-op which made signal-emission assertions impossible.
        This minimal implementation invokes connected callables on
        emit, matching the observable behaviour of Qt for tests.
        """

        def __init__(self, *args, **kwargs):
            self._slots = []

        def emit(self, *args, **kwargs):
            for slot in list(self._slots):
                # Tests assert on state; swallow callback errors so
                # one bad slot doesn't mask the rest of the suite.
                with contextlib.suppress(Exception):
                    slot(*args, **kwargs)

        def connect(self, slot, *args, **kwargs):
            if callable(slot):
                self._slots.append(slot)

        def disconnect(self, slot=None, *args, **kwargs):
            if slot is None:
                self._slots.clear()
            else:
                with contextlib.suppress(ValueError):
                    self._slots.remove(slot)

    def _signal_factory(*args, **kwargs):
        return _Signal()

    def _slot_decorator(*args, **kwargs):
        # Qt's @Slot decorator — accept any args, return identity decorator
        def _wrap(fn):
            return fn

        return _wrap

    # QtCore — the bulk of what update_manager and log_engine need
    qtcore = types.ModuleType("PySide6.QtCore")
    qtcore.QObject = _Base
    qtcore.QThread = _Base
    qtcore.QTimer = _Base
    qtcore.QMutex = _Base
    qtcore.QMutexLocker = _Base
    qtcore.QEventLoop = _Base
    qtcore.Qt = _Base
    qtcore.Signal = _signal_factory
    qtcore.Slot = _slot_decorator

    # QtWidgets — update_manager's UpdateDialog and PlotConfigDialog use these
    qtwidgets = types.ModuleType("PySide6.QtWidgets")
    for name in (
        "QApplication", "QDialog", "QHBoxLayout", "QLabel",
        "QMessageBox", "QProgressBar", "QPushButton", "QTextEdit",
        "QVBoxLayout", "QWidget", "QProxyStyle", "QStyle",
        # PlotConfigDialog
        "QButtonGroup", "QComboBox", "QFrame", "QInputDialog",
        "QListWidget", "QListWidgetItem", "QPlainTextEdit",
        "QRadioButton", "QSizePolicy", "QStackedLayout",
        # Preferences
        "QCheckBox", "QFileDialog", "QLineEdit", "QScrollArea",
        "QSpinBox",
    ):
        setattr(qtwidgets, name, _Base)

    # QtGui — some modules transitively reference this
    qtgui = types.ModuleType("PySide6.QtGui")
    qtgui.QFont = _Base
    qtgui.QFontDatabase = _Base
    qtgui.QIcon = _Base
    qtgui.QPixmap = _Base
    qtgui.QAction = _Base
    qtgui.QKeySequence = _Base
    # ConsoleView and friends
    qtgui.QColor = _Base
    qtgui.QTextCharFormat = _Base
    qtgui.QTextCursor = _Base
    qtgui.QPalette = _Base
    qtgui.QBrush = _Base
    qtgui.QGuiApplication = _Base
    qtgui.QDesktopServices = _Base

    # QtSerialPort — serial_manager uses this
    qtserial = types.ModuleType("PySide6.QtSerialPort")
    qtserial.QSerialPort = _Base
    qtserial.QSerialPortInfo = _Base

    # Top-level PySide6 package
    ps = types.ModuleType("PySide6")
    ps.QtCore = qtcore
    ps.QtWidgets = qtwidgets
    ps.QtGui = qtgui
    ps.QtSerialPort = qtserial

    sys.modules["PySide6"] = ps
    sys.modules["PySide6.QtCore"] = qtcore
    sys.modules["PySide6.QtWidgets"] = qtwidgets
    sys.modules["PySide6.QtGui"] = qtgui
    sys.modules["PySide6.QtSerialPort"] = qtserial


_install_pyside6_stubs()
