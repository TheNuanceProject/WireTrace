# SPDX-License-Identifier: MIT
"""WireTrace entry point — minimal bootstrap.

Per spec section 3.4: main.py owns QApplication creation and entry point.
Zero business logic. All work delegated to app/application.py.

Special modes:
  --smoke-test  Headless import-chain verification, used by the build
                pipeline to confirm that lazy-imported modules
                (pyqtgraph, numpy, dialogs, help loader) are all
                bundled correctly into the frozen binary. Exits 0 on
                success, non-zero on any import failure. Never opens
                a window; suitable for unattended CI execution.
"""

from __future__ import annotations

import logging
import sys

from version import APP_NAME, APP_VERSION


def setup_logging() -> None:
    """Configure application logging."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def run_smoke_test() -> int:
    """Verify every lazy-imported module resolves in the frozen binary.

    This is the canonical defence against the "works in dev, broken in
    .exe" class of bug. The Plot panel, Configure Plot dialog, Help
    loader, and Toast widget are all lazy-imported in production code.
    Static-analysis-based bundlers (Nuitka, PyInstaller) can miss the
    third-party packages those lazy imports depend on — most
    famously pyqtgraph and numpy, which are reachable only through
    DeviceTab._ensure_plot_constructed().

    This function imports every lazy entry point that production
    code paths reach. If any import fails, it prints the failing
    module to stderr and returns a non-zero exit code, which the
    build script translates into a hard build failure. Catching the
    miss here is hundreds of times cheaper than catching it via a
    user report after release.

    Returns:
        0 on success, 1 on any import failure.
    """
    logger = logging.getLogger("smoke_test")
    logger.info("Running smoke test — verifying lazy-import chains")

    # List of (module_path, friendly_name) pairs. Each pair represents
    # a production lazy-import. Add to this list whenever a new lazy
    # import is introduced anywhere in the codebase.
    lazy_imports = [
        # Plot subsystem — the v1.1.0 regression that motivated this
        # smoke test. pyqtgraph and numpy are imported transitively
        # through plot_view.
        ("pyqtgraph", "pyqtgraph (live plotter rendering library)"),
        ("numpy", "numpy (ring-buffer backing array)"),
        ("ui.widgets.plot_view", "PlotView widget (lazy-loaded from DeviceTab)"),
        ("ui.dialogs.plot_config_dialog",
         "Configure Plot dialog (lazy-loaded from menu/toolbar/CTA)"),
        # Help — version-stamped, lazy-resolved in ui.main_window
        ("app.help_loader", "Help loader (lazy-loaded from Help menu)"),
        # Other lazy entry points reachable from production paths
        ("ui.widgets.toast", "Toast widget"),
        ("ui.dialogs.new_log_dialog", "New Log dialog"),
        ("ui.dialogs.export_dialog", "Export dialog"),
        ("ui.dialogs.preferences_dialog", "Preferences dialog"),
        ("ui.dialogs.about_dialog", "About dialog"),
        # Core engines — eagerly imported by application.py but
        # listed here too as a safety net.
        ("core.plot_engine", "Plot engine"),
        ("core.plot_parsers", "Plot parsers"),
        ("core.log_engine", "Log engine"),
        ("core.tag_detector", "Tag detector"),
    ]

    failures: list[str] = []
    for module_name, friendly in lazy_imports:
        try:
            __import__(module_name)
            logger.info("  OK   %s", friendly)
        except Exception as exc:
            msg = f"FAIL  {friendly}: {type(exc).__name__}: {exc}"
            logger.error("  %s", msg)
            failures.append(msg)

    # Additionally exercise the Help loader's source-path resolution.
    # This is critical for frozen builds: the resource may be missing
    # from the bundle even if the loader module imports fine.
    try:
        from app.help_loader import resolve_user_guide_source
        source = resolve_user_guide_source()
        logger.info("  OK   User Guide source resolved: %s", source)
    except Exception as exc:
        msg = f"FAIL  User Guide source resolution: {type(exc).__name__}: {exc}"
        logger.error("  %s", msg)
        failures.append(msg)

    if failures:
        logger.error(
            "Smoke test FAILED — %d import(s) broken in this build:",
            len(failures),
        )
        for f in failures:
            logger.error("  %s", f)
        return 1

    logger.info("Smoke test PASSED — all lazy-import chains resolve")
    return 0


def main() -> int:
    """Application entry point."""
    setup_logging()

    # Handle special modes before opening any window.
    if "--smoke-test" in sys.argv:
        return run_smoke_test()

    logger = logging.getLogger(__name__)
    logger.info("Starting %s v%s", APP_NAME, APP_VERSION)

    try:
        from app.application import WireTraceApp
        app = WireTraceApp(sys.argv)
        return app.run()
    except Exception:
        logger.exception("Fatal error during startup")
        return 1


if __name__ == "__main__":
    sys.exit(main())
