# SPDX-License-Identifier: GPL-3.0-or-later
"""The modules of one tab: attached when switched on, released when off.

A module never receives the port, the writer or a file. It is given a
``TabContext``, the parts of its tab it may use, and, if it reads lines,
a buffer of its own that the pipeline fills (see
``core.device_pipeline``). While a session is running, the host empties
those buffers on a timer, inside a share of each tick, so a busy module
slows nothing else. Between sessions it does not run: the records of a
new session wait in the buffers until every module has started the
session, so none is taken by a module still holding the last one.

Every call into a module is guarded, and so is every slot a module
connects through ``TabModule.guarded``. A module that raises is detached
from its tab, the error is logged with its traceback, and the tab is
told, so it can say that the feature is off; logging carries on.
"""

from __future__ import annotations

import importlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QSplitter, QWidget

from app.constants import (
    DISPLAY_CHUNK_LINES,
    GUI_UPDATE_INTERVAL_MS,
    MODULE_TICK_BUDGET_FRACTION,
    MS_PER_SECOND,
)
from app.diagnostic_log import log_data_failure
from app.modules import MODULES, ModuleSettings, ModuleSpec

if TYPE_CHECKING:
    from app.config import ConfigManager
    from app.port_memory import PortMemory
    from core.device_pipeline import DevicePipeline, DisplayBuffer
    from core.records import LineRecord
    from core.session_counters import SessionFigures
    from core.session_spool import SpoolPosition, SpoolSnapshot
    from ui.alert_notifier import AlertKind
    from ui.themes.theme_manager import ThemeManager
    from ui.widgets.banner import BannerArea
    from ui.widgets.tab_toolbar import TabToolbar

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TabContext:
    """The parts of a tab a module may use."""

    widget: QWidget
    theme_manager: ThemeManager
    config: ConfigManager
    port_memory: PortMemory
    toolbar: TabToolbar
    panels: QSplitter
    title: Callable[[], str]
    recorded_port: Callable[[], str]
    recorded_baud: Callable[[], int]
    # The framing of the connection, such as "8N1, no flow control".
    framing: Callable[[], str]
    is_connected: Callable[[], bool]
    # Whether the connection was lost and the board is waited for.
    is_waiting: Callable[[], bool]
    note_usage: Callable[[object], None]
    # What the session recorded after a position, from any thread
    # (``DevicePipeline.recording``).
    recording: Callable[[SpoolPosition | None, int | None], SpoolSnapshot | None]
    # The tab's banners, for problems a module must report.
    banners: BannerArea
    # Say that saving a file failed, and why; and open a folder.
    report_problem: Callable[[str, str], None]
    open_folder: Callable[[str], None]
    # The exact figures of the current or last session, from any thread
    # (``DevicePipeline.session_figures``).
    session_figures: Callable[[], SessionFigures | None]
    # Sound and notify an alert of this tab, shared with the other tabs
    # and paced by the time between alerts (``ui/alert_notifier.py``).
    alert: Callable[[AlertKind, str], None]


class TabModule(QObject):
    """A module attached to one tab.

    ``reads_lines`` modules are given every record of the session through
    ``take_records``. Every method may be left as it is; ``stop`` must
    release whatever ``start`` built. A module connects its own slots
    through ``guarded``.

    Signals:
        slot_failed(): A guarded slot raised; the host detaches the module.
    """

    reads_lines: ClassVar[bool] = False
    slot_failed = Signal()

    def __init__(self, context: TabContext) -> None:
        super().__init__(context.widget)
        self.context = context
        self._guarded_slots: dict[Callable[..., object], _GuardedSlot] = {}

    def guarded(self, slot: Callable[..., object]) -> Callable[..., None]:
        """``slot``, to connect to a signal: should it raise, the module is detached.

        The connection ends with the module. A slot guarded again is given
        the same wrapper, so connecting it often adds nothing.
        """
        wrapper = self._guarded_slots.get(slot)
        if wrapper is None:
            wrapper = self._guarded_slots[slot] = _GuardedSlot(self, slot)
        return wrapper.run

    def start(self) -> None:
        """Build what the module adds to its tab."""

    def stop(self) -> None:
        """Release everything the module built."""

    def take_records(self, records: list[LineRecord], skipped: int) -> None:
        """New records, after ``skipped`` that this module fell too far behind to receive."""

    def session_started(self) -> None:
        """A device was connected; a new session began."""

    def session_ended(self, unexpected: bool) -> None:
        """The connection ended and the session is on disk."""

    def connection_lost(self) -> None:
        """The connection was lost; the session waits for the board to return."""

    def connection_back(self, seconds_away: float) -> None:
        """The board is back and the session continues."""

    def title_changed(self) -> None:
        """The name of the tab changed."""

    def apply_preferences(self) -> None:
        """The preferences were saved."""


class _GuardedSlot(QObject):
    """A slot of a module that reports an error instead of raising it.

    A child of its module, so a connection to it ends when the module goes.
    """

    def __init__(self, module: TabModule, slot: Callable[..., object]) -> None:
        super().__init__(module)
        self._module = module
        self._slot = slot

    def run(self, *args: object) -> None:
        try:
            self._slot(*args)
        except Exception:
            log_data_failure(logger, "Module %s raised in a slot", type(self._module).__name__)
            self._module.slot_failed.emit()


class ModuleHost(QObject):
    """Attaches the switched-on modules to a tab and guards every call.

    Signals:
        module_failed(str):  A module stopped after an error and is off
                             for this tab; the text says so.
    """

    module_failed = Signal(str)

    def __init__(
        self,
        context: TabContext,
        pipeline: DevicePipeline,
        settings: ModuleSettings,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._context = context
        self._pipeline = pipeline
        self._settings = settings
        self._modules: dict[str, TabModule] = {}
        self._buffers: dict[str, DisplayBuffer] = {}
        # Modules that failed in this tab stay off here until it closes.
        self._failed: set[str] = set()
        # This tab's own choice for a module; one not chosen follows
        # Preferences.
        self._chosen: dict[str, bool] = {}
        self._drain_timer = QTimer(self)
        self._drain_timer.setInterval(GUI_UPDATE_INTERVAL_MS)
        self._drain_timer.timeout.connect(self.drain)

    # ── Attaching ────────────────────────────────────────────────────────

    def sync(self) -> None:
        """Attach the modules switched on and detach those switched off."""
        for spec in MODULES:
            if spec.import_path is None:
                continue
            wanted = self.is_chosen_on(spec) and spec.id not in self._failed
            if wanted and spec.id not in self._modules:
                self._attach(spec)
            elif not wanted and spec.id in self._modules:
                self._detach(spec)

    def is_chosen_on(self, spec: ModuleSpec) -> bool:
        """Whether ``spec`` is on for this tab: its own choice, or Preferences."""
        chosen = self._chosen.get(spec.id)
        return self._settings.is_enabled(spec) if chosen is None else chosen

    def choose(self, spec: ModuleSpec, on: bool) -> None:
        """Switch ``spec`` for this tab only, until it closes."""
        self._chosen[spec.id] = on
        self.sync()

    def module(self, spec: ModuleSpec) -> TabModule | None:
        """The attached module of ``spec``, or None while it is off."""
        return self._modules.get(spec.id)

    def set_interval(self, interval_ms: int) -> None:
        self._drain_timer.setInterval(interval_ms)

    def shutdown(self) -> None:
        """Detach every module."""
        self._drain_timer.stop()
        for spec in MODULES:
            if spec.id in self._modules:
                self._detach(spec)

    # ── Telling Modules ──────────────────────────────────────────────────

    def session_started(self) -> None:
        self._each(lambda module: module.session_started())
        self._run_drain()

    def session_ended(self, unexpected: bool) -> None:
        self.drain(unbounded=True)
        self._drain_timer.stop()
        self._each(lambda module: module.session_ended(unexpected))

    def connection_lost(self) -> None:
        self._each(lambda module: module.connection_lost())

    def connection_back(self, seconds_away: float) -> None:
        self._each(lambda module: module.connection_back(seconds_away))

    def title_changed(self) -> None:
        self._each(lambda module: module.title_changed())

    def apply_preferences(self) -> None:
        self._each(lambda module: module.apply_preferences())

    def drain(self, unbounded: bool = False) -> None:
        """Give each module the records waiting for it, within a share of the tick."""
        budget = self._drain_timer.interval() * MODULE_TICK_BUDGET_FRACTION / MS_PER_SECOND
        started = time.perf_counter()
        for spec_id, buffer in list(self._buffers.items()):
            module = self._modules.get(spec_id)
            while module is not None:
                records, skipped = buffer.take(DISPLAY_CHUNK_LINES)
                if not records and not skipped:
                    break
                if not self._guard(spec_id, lambda m=module, r=records, s=skipped: m.take_records(r, s)):
                    break
                if not unbounded and time.perf_counter() - started >= budget:
                    return

    # ── Internal ─────────────────────────────────────────────────────────

    def _attach(self, spec: ModuleSpec) -> None:
        try:
            module_name, class_name = spec.import_path.split(":")
            module_class = getattr(importlib.import_module(module_name), class_name)
            module = module_class(self._context)
        except Exception:
            logger.exception("Module %s could not be loaded", spec.id)
            self._failed.add(spec.id)
            self.module_failed.emit(f"{spec.title} could not be started and is off for this tab.")
            return
        self._modules[spec.id] = module
        # Queued, so the module is detached once its slot has returned.
        module.slot_failed.connect(
            lambda spec_id=spec.id: self._fail(spec_id), Qt.ConnectionType.QueuedConnection,
        )
        if module.reads_lines:
            self._buffers[spec.id] = self._pipeline.add_consumer()
        if self._guard(spec.id, module.start) and self._context.is_connected():
            self._guard(spec.id, module.session_started)
            self._run_drain()

    def _detach(self, spec: ModuleSpec) -> None:
        module = self._modules.pop(spec.id, None)
        buffer = self._buffers.pop(spec.id, None)
        if buffer is not None:
            self._pipeline.remove_consumer(buffer)
        self._run_drain()
        if module is None:
            return
        try:
            module.stop()
        except Exception:
            logger.exception("Module %s raised while stopping", spec.id)
        module.deleteLater()

    def _run_drain(self) -> None:
        """Run the drain timer while a session runs and a module reads lines."""
        if self._buffers and self._context.is_connected():
            self._drain_timer.start()
        else:
            self._drain_timer.stop()

    def _each(self, call: Callable[[TabModule], None]) -> None:
        for spec_id, module in list(self._modules.items()):
            self._guard(spec_id, lambda m=module: call(m))

    def _guard(self, spec_id: str, call: Callable[[], None]) -> bool:
        """Run ``call``; on an error, detach the module. Returns whether it succeeded."""
        try:
            call()
        except Exception:
            log_data_failure(logger, "Module %s raised", spec_id)
            self._fail(spec_id)
            return False
        return True

    def _fail(self, spec_id: str) -> None:
        """Detach a module that raised, and say that its feature is off for this tab."""
        if spec_id not in self._modules:
            return
        spec = next(spec for spec in MODULES if spec.id == spec_id)
        logger.error("Module %s is detached from its tab after an error", spec_id)
        self._failed.add(spec_id)
        self._detach(spec)
        self.module_failed.emit(
            f"{spec.title} stopped after an error and is off for this tab. "
            "Logging is unaffected."
        )
