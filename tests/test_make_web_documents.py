# SPDX-License-Identifier: GPL-3.0-or-later
"""The tool that writes the web editions of the documents for a release."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.help_loader import WEB_DOCUMENTS, render_web_document
from tools import make_web_documents


def test_every_document_is_written_as_its_web_edition(tmp_path: Path) -> None:
    written = make_web_documents.write_web_documents(tmp_path / "documents")

    assert sorted(path.name for path in written) == sorted(
        f"{name}.html" for name in WEB_DOCUMENTS.values()
    )
    for template, name in WEB_DOCUMENTS.items():
        text = (tmp_path / "documents" / f"{name}.html").read_text(encoding="utf-8")
        assert text == render_web_document(template)


def test_the_digest_of_each_file_is_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert make_web_documents.main([str(tmp_path)]) == 0

    printed = capsys.readouterr().out.splitlines()
    assert sorted(printed) == sorted(
        f"{hashlib.sha256((tmp_path / f'{name}.html').read_bytes()).hexdigest()}  {name}.html"
        for name in WEB_DOCUMENTS.values()
    )
