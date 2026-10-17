# SPDX-License-Identifier: GPL-3.0-or-later
"""When updates are looked for, and what is remembered about them.

A check fetches the signed manifest, verifies it and compares versions.
Nothing is downloaded here: the update window does that once the user
has chosen to update.

Automatic checks are made once shortly after start and then once per
interval for as long as the application stays open, while checks are
switched on in Preferences. A check the user asks for from the Help menu
is always made.

What a check sends is the ``User-Agent`` of ``updater.install`` and one
more header: the day of the previous check, as the server itself named
it in its answer then, or a fixed word on the first check. The same day
number is given to everyone who checks on that day, so it identifies no
one, and it lets the server count how many installations are in use
without any identifier. A day the server names is kept only if it is
close to today's, so no installation can be handed a number of its own.
The application's own clock is never sent.

"Skip this version" and "Remind later" are kept in the preferences, so
they hold across restarts.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone

from PySide6.QtCore import QObject, QTimer, Signal

from app.config import ConfigManager
from app.constants import (
    UPDATE_CHECK_INTERVAL_HOURS,
    UPDATE_CHECK_STARTUP_DELAY_S,
    UPDATE_FIRST_CHECK_VALUE,
    UPDATE_LAST_CHECK_HEADER,
    UPDATE_SERVER_DAY_HEADER,
    UPDATE_SERVER_DAY_TOLERANCE,
    UPDATE_SNOOZE_HOURS,
)
from updater.background import Task
from updater.download import DownloadError, Opener, fetch, remove_legacy_downloads
from updater.install import user_agent
from updater.manifest import (
    ManifestError,
    ReleaseManifest,
    decode_public_key,
    format_signed_at,
    is_newer,
    parse_signed_at,
    read_signed_manifest,
)
from version import APP_VERSION, UPDATE_MANIFEST_URL, UPDATE_PUBLIC_KEYS

logger = logging.getLogger(__name__)

_MS_PER_SECOND = 1000
_MS_PER_HOUR = 3_600_000

#: What a check that failed says. The cause is logged; what a server
#: answered is never shown, so it cannot put words in the window.
CHECK_UNREACHABLE = "The server could not be reached"
CHECK_UNVERIFIED = "The update information could not be verified"
CHECK_OUTDATED = "The update information is older than what this copy has already seen"
#: A day number as the server writes it: days since 1 January 1970.
_SERVER_DAY = re.compile(r"\d{1,6}")
_SECONDS_PER_DAY = 86_400


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UpdateManager(QObject):
    """Looks for updates and remembers the user's answers.

    Signals:
        update_available(object, bool):  A newer release, and whether the
                                         user asked for this check.
        up_to_date():                    A check the user asked for found
                                         nothing newer.
        check_failed(str):               A check the user asked for failed.
    """

    update_available = Signal(object, bool)
    up_to_date = Signal()
    check_failed = Signal(str)

    def __init__(
        self,
        config: ConfigManager,
        parent: QObject | None = None,
        *,
        opener: Opener | None = None,
        public_keys: Sequence[str] = UPDATE_PUBLIC_KEYS,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._opener = opener
        self._keys = tuple(decode_public_key(key) for key in public_keys)
        self._now = now
        self._task: Task | None = None
        #: Whether the check under way was asked for by the user.
        self._checking_manually = False
        self._found: ReleaseManifest | None = None

        self._first_check = QTimer(self)
        self._first_check.setSingleShot(True)
        self._first_check.setInterval(UPDATE_CHECK_STARTUP_DELAY_S * _MS_PER_SECOND)
        self._first_check.timeout.connect(self._check_automatically)
        self._next_check = QTimer(self)
        self._next_check.setInterval(UPDATE_CHECK_INTERVAL_HOURS * _MS_PER_HOUR)
        self._next_check.timeout.connect(self._check_automatically)

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def found(self) -> ReleaseManifest | None:
        """The newer release the last check found, if any."""
        return self._found

    @property
    def opener(self) -> Opener | None:
        """What requests are sent through; None means the network."""
        return self._opener

    @property
    def checking(self) -> bool:
        return self._task is not None and self._task.running

    def start(self) -> None:
        """Begin the automatic checks. Whether one is made is decided each
        time, so a change in Preferences takes effect without a restart."""
        remove_legacy_downloads()
        self._first_check.start()
        self._next_check.start()

    def stop(self) -> None:
        self._first_check.stop()
        self._next_check.stop()

    def check_now(self) -> None:
        """Check because the user asked. The answer always arrives as a signal."""
        self._check(manual=True)

    def snooze(self) -> None:
        """Announce no update until the snooze time has passed."""
        self._config.update_snoozed_until = self._now() + timedelta(hours=UPDATE_SNOOZE_HOURS)
        self._config.save()

    def skip_version(self, version: str) -> None:
        """Never announce ``version`` by itself again."""
        self._config.skipped_update_version = version
        self._config.save()

    # ── Checking ─────────────────────────────────────────────────────────

    def _may_check_automatically(self) -> bool:
        return self._config.check_updates_on_startup

    def _check_automatically(self) -> None:
        if self._may_check_automatically():
            self._check(manual=False)

    def _check(self, *, manual: bool) -> None:
        if self.checking:
            return
        if not self._keys:
            logger.warning("This build holds no key to verify updates with")
            if manual:
                self.check_failed.emit("This build cannot verify updates")
            return
        headers = {
            "User-Agent": user_agent(),
            UPDATE_LAST_CHECK_HEADER: self._previous_day() or UPDATE_FIRST_CHECK_VALUE,
        }
        opener, keys = self._opener, self._keys

        def work(_task: Task) -> tuple[ReleaseManifest, str]:
            try:
                data, answer = fetch(UPDATE_MANIFEST_URL, headers, opener=opener)
            except DownloadError as exc:
                logger.warning("The update check could not reach the server: %s", exc)
                raise DownloadError(CHECK_UNREACHABLE) from exc
            day = next(
                (value for name, value in answer.items()
                 if name.lower() == UPDATE_SERVER_DAY_HEADER.lower()),
                "",
            )
            try:
                return read_signed_manifest(data, keys), day
            except ManifestError as exc:
                logger.warning("The update manifest was refused: %s", exc)
                raise ManifestError(CHECK_UNVERIFIED) from exc

        # The task announces its end on its own thread; bound methods of
        # this object are called on the main thread, where it lives.
        task = Task(work, (DownloadError, ManifestError), self)
        task.succeeded.connect(self._on_check_answered)
        task.failed.connect(self._on_check_failed)
        self._task, self._checking_manually = task, manual
        task.start()

    def _on_check_answered(self, result: tuple[ReleaseManifest, str]) -> None:
        manifest, day = result
        self._on_checked(manifest, day, manual=self._checking_manually)

    def _on_check_failed(self, reason: str) -> None:
        self._on_failed(reason, manual=self._checking_manually)

    def _on_checked(self, manifest: ReleaseManifest, day: str, *, manual: bool) -> None:
        if self._is_plausible_day(day) and day != self._config.update_last_check_day:
            self._config.update_last_check_day = day
            self._config.save()

        if not self._is_fresh(manifest):
            self._found = None
            logger.warning("An update manifest signed before the newest one already accepted was refused")
            if manual:
                self.check_failed.emit(CHECK_OUTDATED)
            return

        if not is_newer(manifest.version, APP_VERSION):
            self._found = None
            logger.info("No update: installed %s, latest %s", APP_VERSION, manifest.version)
            if manual:
                self.up_to_date.emit()
            return

        self._found = manifest
        if not manual and self._held_back(manifest.version):
            return
        logger.info("Update available: %s to %s", APP_VERSION, manifest.version)
        self.update_available.emit(manifest, manual)

    def _is_fresh(self, manifest: ReleaseManifest) -> bool:
        """Whether ``manifest`` was signed no earlier than the newest one
        accepted before; if it is newer, it becomes the newest.

        A manifest that was genuinely signed for an earlier release could
        otherwise be served again to hold this installation back.
        """
        signed_at = manifest.signed_at
        if signed_at is None:
            return False
        stored = self._config.update_newest_manifest_signed_at
        try:
            newest = parse_signed_at(stored) if stored else None
        except ManifestError:
            # Edited by hand into something else: start again from this one.
            newest = None
        if newest is not None and signed_at < newest:
            return False
        if newest is None or signed_at > newest:
            self._config.update_newest_manifest_signed_at = format_signed_at(signed_at)
            self._config.save()
        return True

    def _is_plausible_day(self, day: str) -> bool:
        """Whether ``day`` is a day number close to today's.

        Only such a value is stored and sent back. A server could
        otherwise hand each installation a number of its own and
        recognise it at the next check.
        """
        if _SERVER_DAY.fullmatch(day) is None:
            return False
        today = int(self._now().timestamp()) // _SECONDS_PER_DAY
        return abs(int(day) - today) <= UPDATE_SERVER_DAY_TOLERANCE

    def _previous_day(self) -> str:
        """The stored day of the previous check, if it is a day number.
        The preferences file can be edited; nothing else is ever sent."""
        day = self._config.update_last_check_day
        return day if _SERVER_DAY.fullmatch(day) else ""

    def _held_back(self, version: str) -> bool:
        """Whether the user asked not to be told of ``version`` for now."""
        if version == self._config.skipped_update_version:
            return True
        until = self._config.update_snoozed_until
        return until is not None and self._now() < until

    def _on_failed(self, reason: str, *, manual: bool) -> None:
        logger.warning("Update check failed: %s", reason)
        if manual:
            self.check_failed.emit(reason)
