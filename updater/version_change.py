# SPDX-License-Identifier: GPL-3.0-or-later
"""What an update check says of the check before it, and what is kept of
a check that was counted.

The server counts each installation in the version of its latest check
in a period. It holds no identifier, so it learns of a version change
only from the check that follows it: a check made by a version other
than the one that made the previous counted check names that earlier
version and a one-time number made for this change alone. The number is
128 bits from the system's secure generator, derived from nothing; it
is sent with every check until one is counted, so the server can count
the change once however often it arrives, and is then deleted. The next
change gets a new number, unrelated to the last. A check of a version
that has not changed sends neither.

The server names its day in its answer only when it counted the check.
That answer is what is kept: the day, and the version that made the
check, as one value, so that a day written by a version that keeps no
version (WireTrace 2.1 and earlier write the day alone) is never taken
for this version's. When the version of the previous counted check is
not known that way, a check states no change, and once it is counted
the day of that previous check is kept as the last day whose version is
unknown; later changes name it, so the server does not move an
installation whose version in a period it cannot know.

Everything here works on the preferences' ``Updates`` section as the
file holds it, read and written under its lock
(``ConfigManager.change_shared``), so two WireTrace windows checking at
once agree on the number and on what was counted.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from app.constants import (
    UPDATE_CHANGE_HEADER,
    UPDATE_CHANGE_TOKEN_BYTES,
    UPDATE_FIRST_CHECK_VALUE,
    UPDATE_LAST_CHECK_HEADER,
    UPDATE_LAST_VERSION_HEADER,
    UPDATE_UNKNOWN_UNTIL_HEADER,
)

#: The keys of the preferences' ``Updates`` section this works on.
LAST_DAY = "last_check_day"
LAST_VERSION = "last_check_version"
UNKNOWN_UNTIL = "unknown_until"
CHANGE = "change"

_DAY = re.compile(r"\d{1,6}")
_VERSION = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")
_TOKEN = re.compile(rf"[0-9a-f]{{{2 * UPDATE_CHANGE_TOKEN_BYTES}}}")
# How a version and the day of its check are kept together, and how a
# change's number is kept with the version and day it changes from.
_AT = "@"
_SEPARATOR = " "


def new_token() -> str:
    """A change's one-time number."""
    return secrets.token_hex(UPDATE_CHANGE_TOKEN_BYTES)


@dataclass(frozen=True)
class Check:
    """What one check sends, and what it stated."""

    headers: dict[str, str] = field(default_factory=dict)
    #: The day of the previous counted check it named, or empty for a
    #: first check.
    previous_day: str = ""
    #: The one-time number it sent, or None.
    token: str | None = None
    #: Whether the version of the previous counted check was not stated.
    version_unknown: bool = False


def _day(value: str | None) -> str:
    return value if value is not None and _DAY.fullmatch(value) else ""


def _version_at(value: str | None) -> tuple[str, str]:
    """The version and the day of a kept ``version@day``, or empties."""
    version, _, day = (value or "").partition(_AT)
    if _VERSION.fullmatch(version) and _DAY.fullmatch(day):
        return version, day
    return "", ""


def _change(value: str | None) -> tuple[str, str]:
    """The number and the ``version@day`` it changes from, or empties."""
    token, _, base = (value or "").partition(_SEPARATOR)
    if _TOKEN.fullmatch(token) and _version_at(base) != ("", ""):
        return token, base
    return "", ""


def prepare(
    stored: Mapping[str, str], version: str, make_token: Callable[[], str] = new_token,
) -> tuple[Check, dict[str, str]]:
    """What a check by ``version`` sends, given the ``Updates`` section as
    the file holds it, and the settings to write for it.

    The number of a change is made once and kept until a check of it is
    counted; a number kept for another change, or once nothing changes,
    is dropped.
    """
    day = _day(stored.get(LAST_DAY))
    if not day:
        return Check(headers={UPDATE_LAST_CHECK_HEADER: UPDATE_FIRST_CHECK_VALUE}), {}
    headers = {UPDATE_LAST_CHECK_HEADER: day}
    last_version, version_day = _version_at(stored.get(LAST_VERSION))
    kept_token, kept_base = _change(stored.get(CHANGE))
    clear = {CHANGE: ""} if stored.get(CHANGE) else {}
    if not last_version or version_day != day:
        return Check(headers=headers, previous_day=day, version_unknown=True), clear
    base = f"{last_version}{_AT}{day}"
    token = kept_token if kept_base == base else ""
    if last_version == version and not token:
        return Check(headers=headers, previous_day=day), clear
    updates: dict[str, str] = {}
    if not token:
        token = make_token()
        updates[CHANGE] = f"{token}{_SEPARATOR}{base}"
    headers[UPDATE_LAST_VERSION_HEADER] = last_version
    headers[UPDATE_CHANGE_HEADER] = token
    unknown_until = _day(stored.get(UNKNOWN_UNTIL))
    if unknown_until:
        headers[UPDATE_UNKNOWN_UNTIL_HEADER] = unknown_until
    return Check(headers=headers, previous_day=day, token=token), updates


def unstated(check: Check) -> Check:
    """``check`` without the change it would state, for when its number
    could not be kept: a number no other check could repeat might be
    counted twice. Its version is then unknown to the server."""
    if check.token is None:
        return check
    return Check(
        headers={UPDATE_LAST_CHECK_HEADER: check.headers[UPDATE_LAST_CHECK_HEADER]},
        previous_day=check.previous_day,
        version_unknown=True,
    )


def counted(stored: Mapping[str, str], check: Check, server_day: str, version: str) -> dict[str, str]:
    """The settings to write once the server has counted ``check``, made
    by ``version``, and named ``server_day``.

    Nothing is written if a later counted check is already kept. The
    change's number is deleted if it is the one this check sent; a
    previous day whose version was not stated becomes the last day of
    unknown version.
    """
    kept_day = _day(stored.get(LAST_DAY))
    if kept_day and int(kept_day) > int(server_day):
        return {}
    updates = {LAST_DAY: server_day, LAST_VERSION: f"{version}{_AT}{server_day}"}
    if check.version_unknown and check.previous_day:
        kept = _day(stored.get(UNKNOWN_UNTIL))
        updates[UNKNOWN_UNTIL] = str(max(int(check.previous_day), int(kept) if kept else 0))
    token, _ = _change(stored.get(CHANGE))
    if check.token is not None and token == check.token:
        updates[CHANGE] = ""
    return updates
