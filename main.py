# SPDX-License-Identifier: MIT
"""WireTrace entry point — minimal bootstrap.

Per spec section 3.4: main.py owns QApplication creation and entry point.
Zero business logic. All work delegated to app/application.py.
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


def main() -> int:
    """Application entry point."""
    setup_logging()

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
