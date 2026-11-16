# SPDX-License-Identifier: GPL-3.0-or-later
"""The session report of a tab, as a module that can be switched off.

It opens the end-of-test report (``ui/features/session_report.py``) of
the current or last session, and saves it as a text file.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime

from PySide6.QtWidgets import QFileDialog, QWidget

from app._atomic import atomic_write_text
from app.file_names import output_base_path
from app.modules import RESTARTS, ModuleSettings
from ui.dialogs.session_report_dialog import SessionReportDialog
from ui.features.session_report import session_report
from ui.module_host import TabModule
from ui.widgets.toast import Toast
from updater.usage import Feature

logger = logging.getLogger(__name__)

_FILE_NAME = "report"
_FILE_FILTER = "Text files (*.txt);;All files (*)"
_NOT_SAVED = "The report could not be saved"


class ReportFeature(TabModule):
    """The session report of one tab."""

    def report_text(self) -> str | None:
        """The report of the current or last session, or None before the first."""
        figures = self.context.session_figures()
        if figures is None:
            return None
        return session_report(
            figures,
            datetime.now(),
            title=self.context.title(),
            framing=self.context.framing(),
            restarts_counted=ModuleSettings(self.context.config).is_enabled(RESTARTS),
        )

    def open_report(self) -> None:
        """Show the report of the current or last session."""
        text = self.report_text()
        if text is None:
            Toast.info(self.context.widget, "There is no session to report on yet")
            return
        self.context.note_usage(Feature.REPORT)
        SessionReportDialog(text, self._save, self.context.widget).exec()

    def _save(self, parent: QWidget, text: str) -> None:
        config = self.context.config
        suggested = output_base_path(
            config.export_directory, _FILE_NAME, self.context.recorded_port(), datetime.now(),
        ) + ".txt"
        path, _ = QFileDialog.getSaveFileName(parent, "Save the session report", suggested, _FILE_FILTER)
        if not path:
            return
        config.export_directory = os.path.dirname(path)
        try:
            atomic_write_text(path, text)
        except OSError as exc:
            logger.error("Could not save the session report: %s", exc)
            self.context.report_problem(_NOT_SAVED, str(exc))
            Toast.error(parent, f"{_NOT_SAVED}: {exc}")
            return
        folder = os.path.dirname(path)
        Toast.success(
            parent, f"Saved the report: {os.path.basename(path)}",
            "Show", lambda: self.context.open_folder(folder),
        )
