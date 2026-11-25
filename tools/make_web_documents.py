# SPDX-License-Identifier: GPL-3.0-or-later
"""Write the web editions of the user guide and the line format reference.

The website shows both documents in its own design. This script fills
each template of ``resources/help`` in its web edition (see
``app/help_loader.py``) and writes it to a folder, one file a document:
``user-guide.html`` and ``line-formats.html``. Each is an ``<article>``
naming the document and the version it describes. The release workflow
publishes them with the release; the website reads them from there.

Usage:
    python tools/make_web_documents.py <folder>

The folder is created if it does not exist. The SHA-256 of each file is
printed, so the files can be checked wherever they are taken.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.help_loader import WEB_DOCUMENTS, render_web_document  # noqa: E402

_ENCODING = "utf-8"
_SUFFIX = ".html"


def write_web_documents(folder: Path) -> list[Path]:
    """Write every web document to ``folder`` and return the files written."""
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for template, name in WEB_DOCUMENTS.items():
        target = folder / f"{name}{_SUFFIX}"
        target.write_bytes(render_web_document(template).encode(_ENCODING))
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", type=Path, help="where the documents are written")
    arguments = parser.parse_args(argv)
    for path in write_web_documents(arguments.folder):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"{digest}  {path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
