# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace entry point.

Starts the application, which lives in app/application.py. Nothing else
happens here.

Special mode:
  --smoke-test  Self-check used by the build. Imports every module the
                program loads late, starts the window system, draws a
                plot, fills the user guide, verifies a known update
                signature and reads the licence texts. Exits 0 on
                success, 1 otherwise. Shows no window.
"""

from __future__ import annotations

import logging
import sys

from version import APP_NAME, APP_VERSION

#: (module, description) pairs: every module imported inside a function,
#: plus the core modules as a safety net. Add a module here when a new
#: late import is introduced.
_LATE_IMPORTS: tuple[tuple[str, str], ...] = (
    # The plot: pyqtgraph and numpy are imported only through plot_view.
    ("pyqtgraph", "pyqtgraph"),
    ("numpy", "numpy"),
    ("regex", "regex"),
    ("ui.widgets.plot_view", "Plot"),
    ("ui.widgets.plot_window", "Plot window"),
    ("ui.dialogs.plot_config_dialog", "Configure plot"),
    # Dialogs and widgets
    ("app.help_loader", "Help loader"),
    ("ui.widgets.toast", "Toast"),
    ("ui.dialogs.new_log_dialog", "Start a log"),
    ("ui.dialogs.export_dialog", "Export"),
    ("ui.dialogs.preferences_dialog", "Preferences"),
    ("ui.dialogs.saved_commands_dialog", "Saved commands"),
    ("ui.dialogs.shortcuts_dialog", "Keyboard shortcuts"),
    ("ui.dialogs.about_dialog", "About"),
    ("ui.dialogs.licenses_dialog", "Third-party licences"),
    # Core
    ("core.plot_engine", "Plot engine"),
    ("core.plot_parsers", "Plot parsers"),
    ("core.device_pipeline", "Device pipeline"),
    ("core.log_writer", "Session writer"),
    ("core.transport", "Serial transport"),
    ("core.tag_detector", "Tag detector"),
    # Updates
    ("updater.update_manager", "Update manager"),
    ("updater.update_dialog", "Update window"),
)


def setup_logging() -> None:
    """Configure application logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _draw_a_plot() -> str:
    import numpy
    import pyqtgraph

    plot = pyqtgraph.PlotWidget()
    plot.plot(numpy.arange(8, dtype=float))
    if plot.grab().isNull():
        raise RuntimeError("the plot drew nothing")
    return "drawn"


def _start_the_window_system() -> str:
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance() or QApplication(sys.argv[:1])
    return application.platformName()


def _fill_the_user_guide() -> str:
    from app.help_loader import load_user_guide

    return f"{len(load_user_guide())} characters"


def _verify_a_signature() -> str:
    from updater.selfcheck import check_signature_verification

    return f"{check_signature_verification()} trusted key(s)"


def _read_the_licence_texts() -> str:
    from app.licenses import load_third_party_licenses

    return f"{len(load_third_party_licenses())} characters"


def _name_the_installation() -> str:
    from updater.install import install_channel

    return install_channel()


def run_smoke_test() -> int:
    """Check that a build contains everything the program loads late.

    Bundlers that find modules by reading imports can miss a module that
    is imported inside a function, and the packages it needs; pyqtgraph
    and numpy, for example, are reached only when the plot is first
    opened. A build can also leave out the files the program reads, or
    be unable to start the window system on the computer it runs on.

    Each check is logged. A failed one does not stop the others, so one
    run reports everything that is wrong with a build.

    Returns:
        0 if every check passed, 1 otherwise.
    """
    logger = logging.getLogger("smoke_test")
    logger.info("Smoke test of %s %s", APP_NAME, APP_VERSION)

    checks = [
        *((f"Import: {description}", lambda module=module: __import__(module) and "")
          for module, description in _LATE_IMPORTS),
        # Qt ends the process with a message of its own if the window
        # system cannot start, which fails the smoke test as well.
        ("Window system", _start_the_window_system),
        ("Plot", _draw_a_plot),
        ("User guide", _fill_the_user_guide),
        ("Update signatures", _verify_a_signature),
        ("Licence texts", _read_the_licence_texts),
        ("Installed as", _name_the_installation),
    ]

    failures = 0
    for name, check in checks:
        try:
            result = check()
        except Exception as exc:
            failures += 1
            logger.error("  FAIL  %s: %s: %s", name, type(exc).__name__, exc)
        else:
            logger.info("  OK    %s%s", name, f": {result}" if result else "")

    if failures:
        logger.error("Smoke test failed: %d check(s) did not pass", failures)
        return 1
    logger.info("Smoke test passed: every check succeeded")
    return 0


def main() -> int:
    """Application entry point."""
    setup_logging()

    if "--smoke-test" in sys.argv:
        return run_smoke_test()

    logger = logging.getLogger(__name__)
    logger.info("Starting %s v%s", APP_NAME, APP_VERSION)

    try:
        from app.application import WireTraceApp
        return WireTraceApp(sys.argv).run()
    except Exception:
        logger.exception("Fatal error during startup")
        return 1


if __name__ == "__main__":
    sys.exit(main())
