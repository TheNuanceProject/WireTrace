# SPDX-License-Identifier: MIT
"""Add or verify SPDX license identifiers on every source file.

Per the SPDX standard (https://spdx.dev), license identification is a
single machine-parseable comment at the top of each source file:

    # SPDX-License-Identifier: MIT

Used by the Linux kernel, most modern open-source projects, and
automated license-compliance tools (FOSSology, REUSE, GitHub's
license detector).

Usage:
    python tools/add_spdx_headers.py              # add missing headers
    python tools/add_spdx_headers.py --check      # verify only (exit 1 if missing)

Idempotent: running twice does nothing the second time.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# The SPDX identifier to add. See https://spdx.org/licenses/
SPDX_LINE = "# SPDX-License-Identifier: MIT"

# Files we add headers to. Glob patterns relative to repo root.
INCLUDE_GLOBS: tuple[str, ...] = (
    "*.py",
    "app/**/*.py",
    "core/**/*.py",
    "ui/**/*.py",
    "updater/**/*.py",
    "build/**/*.py",
    "tests/**/*.py",
    "tools/**/*.py",
)

# Directories to skip even if files match.
EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".venv", "venv", "env",
    "build/dist", "build/WireTrace.build", "build/WireTrace.dist",
    "deployment",
    ".git", "__pycache__", ".mypy_cache", ".ruff_cache", ".pytest_cache",
})


def is_excluded(path: Path, repo_root: Path) -> bool:
    """Return True if any part of the path matches an exclude directory."""
    try:
        rel_parts = path.relative_to(repo_root).parts
    except ValueError:
        return True
    return any(part in EXCLUDE_DIRS for part in rel_parts)


def has_spdx(content: str) -> bool:
    """Check whether the first ~5 lines contain any SPDX identifier."""
    head = content.splitlines()[:5]
    return any("SPDX-License-Identifier:" in line for line in head)


def add_spdx(content: str) -> str:
    """Prepend the SPDX line to file content.

    Placement rules:
      - After a shebang (#!/usr/bin/env python) if present
      - Otherwise at the very top
      - Followed by one blank line before existing content
    """
    lines = content.splitlines(keepends=True)

    # Preserve shebang as the first line if present
    if lines and lines[0].startswith("#!"):
        return lines[0] + SPDX_LINE + "\n" + "".join(lines[1:])

    return SPDX_LINE + "\n" + content


def collect_files(repo_root: Path) -> list[Path]:
    """Find all source files matching INCLUDE_GLOBS, excluding EXCLUDE_DIRS."""
    found: set[Path] = set()
    for pattern in INCLUDE_GLOBS:
        for path in repo_root.glob(pattern):
            if path.is_file() and not is_excluded(path, repo_root):
                found.add(path)
    return sorted(found)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit non-zero if any file is missing the SPDX header. Does not modify files.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    files = collect_files(repo_root)

    if not files:
        print("No source files found.")
        return 0

    missing: list[Path] = []
    added: list[Path] = []

    for path in files:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"SKIP {path.relative_to(repo_root)} ({exc})", file=sys.stderr)
            continue

        if has_spdx(content):
            continue

        missing.append(path)

        if not args.check:
            path.write_text(add_spdx(content), encoding="utf-8")
            added.append(path)

    if args.check:
        if missing:
            print(f"✗ {len(missing)} file(s) missing SPDX header:")
            for p in missing:
                print(f"    {p.relative_to(repo_root)}")
            return 1
        print(f"✓ All {len(files)} source files have SPDX headers.")
        return 0

    if added:
        print(f"✓ Added SPDX header to {len(added)} file(s).")
        for p in added:
            print(f"    {p.relative_to(repo_root)}")
    else:
        print(f"✓ All {len(files)} source files already have SPDX headers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
