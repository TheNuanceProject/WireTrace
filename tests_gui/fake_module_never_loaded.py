# SPDX-License-Identifier: GPL-3.0-or-later
"""A module whose code must not be imported while it is switched off."""

from __future__ import annotations

from ui.module_host import TabModule


class NeverLoadedModule(TabModule):
    """Exists only to be left unloaded."""
