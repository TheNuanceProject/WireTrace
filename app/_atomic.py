# SPDX-License-Identifier: GPL-3.0-or-later
"""Writing a small file so that it is never seen half written.

Opening a file for writing empties it before the new content arrives. A
crash, a power loss or a full disk in between leaves it empty or
partial, and the next start would fall back to the defaults, losing the
user's preferences and saved plot profiles.

``atomic_write_text`` removes that window. It writes to a temporary
file beside the destination, flushes and syncs it to the disk, then
renames it over the destination. ``os.replace`` is atomic on POSIX
(``rename(2)``) and on Windows (``MoveFileExW`` with
``MOVEFILE_REPLACE_EXISTING``), so a reader sees either the complete old
file or the complete new one, never a partial one.

This module depends on nothing else in the project.
"""

from __future__ import annotations

import contextlib
import os
import tempfile

#: The temporary file is made in the same directory as the destination,
#: so the final ``os.replace`` stays on one filesystem: a rename across
#: filesystems is not atomic and would raise. Its name is unique, so no
#: file that is already there is ever written over.
_TMP_PREFIX = ".wiretrace-"
_TMP_SUFFIX = ".tmp"


def atomic_write_text(path: str, data: str, encoding: str = "utf-8") -> None:
    """Write ``data`` to ``path``, all or nothing.

    The file is readable and writable by its owner only.

    Args:
        path: Destination file path.
        data: Full file contents to write.
        encoding: Text encoding.

    Raises:
        OSError: The temporary file could not be written, synced or
            renamed. The destination is left exactly as it was, and the
            temporary file is removed.
    """
    descriptor, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(path) or os.curdir, prefix=_TMP_PREFIX, suffix=_TMP_SUFFIX,
    )
    try:
        with os.fdopen(descriptor, "w", encoding=encoding) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        # ``os.replace`` is the only writer of ``path``, so a failure
        # anywhere above leaves the destination exactly as it was. The
        # temporary file is removed; an error in doing so is ignored, so
        # that the original failure is the one reported.
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise
