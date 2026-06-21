# SPDX-License-Identifier: MIT
"""Atomic file-write primitive.

Writing config and other small state files with a bare ``open(path, "w")``
truncates the destination *before* the new content is written. A crash,
power loss, or disk-full condition between truncation and completion
leaves the destination empty or partial — and the next load silently
falls back to defaults, losing the user's preferences and saved plot
profiles.

``atomic_write_text`` removes that window. It writes to a temporary
sibling, flushes and fsyncs that file to physical disk, then atomically
renames it over the destination. ``os.replace`` is atomic at the
filesystem level on both POSIX (``rename(2)``) and Windows
(``MoveFileExW`` with ``MOVEFILE_REPLACE_EXISTING``), so any reader sees
either the complete old file or the complete new file — never a partial
one.

This module has no project dependencies and touches nothing but the
filesystem, so it is safe to reuse from any persistence call site.
"""

from __future__ import annotations

import contextlib
import os

#: Suffix appended to the destination path to form the temporary sibling.
#: The temp file lives in the *same directory* as the destination so the
#: final ``os.replace`` stays on one filesystem — a cross-filesystem
#: rename is not atomic and would raise.
_TMP_SUFFIX = ".tmp"


def atomic_write_text(path: str, data: str, encoding: str = "utf-8") -> None:
    """Atomically write ``data`` to ``path``.

    Writes to ``path + ".tmp"``, flushes and fsyncs it to physical disk,
    then atomically replaces ``path`` with the temp file. On any failure
    the destination is left untouched, and a best-effort attempt is made
    to remove the temporary sibling.

    Args:
        path: Destination file path.
        data: Full file contents to write.
        encoding: Text encoding (default UTF-8).

    Raises:
        OSError: If the temp write, fsync, or replace fails. The
            destination file is never left partially written; only the
            temporary sibling can be affected, and it is cleaned up on
            the failure path.
    """
    tmp_path = path + _TMP_SUFFIX
    try:
        with open(tmp_path, "w", encoding=encoding) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError:
        # ``os.replace`` is the only writer of ``path``, so a failure
        # anywhere above leaves the destination exactly as it was. Remove
        # the partial temp sibling; ignore cleanup errors so the original
        # failure propagates unmasked.
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
