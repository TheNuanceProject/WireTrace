# SPDX-License-Identifier: GPL-3.0-or-later
"""When updates are looked for, and what is remembered about them.

A check fetches the signed manifest, verifies it and compares versions.
Nothing is downloaded here: the update window does that once the user
has chosen to update.

Automatic checks are made once shortly after start and then once per
interval for as long as the application stays open, while checks are
switched on in Preferences. A check the user asks for from the Help menu
is always made.

What a check sends is the ``User-Agent`` of ``updater.install`` and the
day of the previous counted check, as the server itself named it in its
answer then, or a fixed word on the first check. The same day number is
given to everyone who checks on that day, so it identifies no one, and
it lets the server count how many installations are in use without any
identifier. A day the server names is kept only if it is close to
today's, so no installation can be handed a number of its own. The
application's own clock is never sent. A check made by a version other
than the one that made the previous counted check also names that
version, with a one-time number for the change (``updater.version_change``).

"Skip this version" and "Remind later" are kept in the preferences, so
they hold across restarts.

The signed manifest of the release installed names the release before
it, which this installation can return to. A check that finds that
manifest keeps it in the cache folder, so the release before is known
at the next start and once a newer release is out; it is verified again
each time it is read.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from app._atomic import atomic_write_bytes
from app.config import ConfigManager
from app.constants import (
    INSTALLED_MANIFEST_FILENAME,
    MS_PER_HOUR,
    MS_PER_SECOND,
    UPDATE_CHECK_INTERVAL_HOURS,
    UPDATE_CHECK_STARTUP_DELAY_S,
    UPDATE_CHECKS_FAILING_DAYS,
    UPDATE_MANIFEST_MAX_BYTES,
    UPDATE_SERVER_DAY_HEADER,
    UPDATE_SERVER_DAY_TOLERANCE,
    UPDATE_SNOOZE_HOURS,
    get_cache_dir,
)
from updater.background import Task
from updater.download import DownloadError, Opener, fetch, remove_legacy_downloads
from updater.install import user_agent
from updater.manifest import (
    ManifestError,
    Release,
    ReleaseManifest,
    decode_public_key,
    format_signed_at,
    is_newer,
    parse_signed_at,
    read_signed_manifest,
)
from updater.version_change import Check, counted, prepare, unstated
from version import APP_VERSION, UPDATE_MANIFEST_URL, UPDATE_PUBLIC_KEYS

logger = logging.getLogger(__name__)


#: What a check that failed says: what happened and what to do, with the
#: release page as the way out. The technical cause is logged; what a
#: server answered is never shown, so it cannot put words in the window.
CHECK_UNREACHABLE = (
    "The update server could not be reached. Check the internet connection, "
    "or get the newest version from the release page."
)
CHECK_UNVERIFIED = (
    "The update information could not be verified, so it was not used. "
    "Try again later, or get the newest version from the release page."
)
CHECK_OUTDATED = (
    "The update information was older than what WireTrace has already seen, so it was "
    "not used. Try again later, or get the newest version from the release page."
)
CHECK_NO_KEYS = (
    "This copy of WireTrace cannot verify updates. "
    "Get the newest version from the release page."
)
#: Why the release to return to was not found: the check failed, or the
#: manifests at hand name none. The release list is the way out.
RETURN_CHECK_FAILED = (
    "The previous version could not be looked up. Check the internet connection and "
    "try again, or get an earlier version from the release list."
)
RETURN_UNKNOWN = (
    f"The update information does not name the version before {APP_VERSION}. "
    "Earlier versions are on the release list."
)
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
        check_failed(str):               A check the user asked for failed:
                                         what happened and what to do.
        offer_changed(object):           The release to keep offering, or
                                         None: a newer release the user has
                                         neither skipped nor put off.
        checks_failing_changed(object):  Since when automatic checks have
                                         failed, once that is
                                         ``UPDATE_CHECKS_FAILING_DAYS`` or
                                         more with none answered, or None.
        returnable_changed(object):      The release this installation can
                                         return to (a Release), or None.
        return_found(object):            ``find_return`` found the release
                                         to return to (a Release).
        return_not_found(str):           ``find_return`` found none: why,
                                         and what to do.
    """

    update_available = Signal(object, bool)
    up_to_date = Signal()
    check_failed = Signal(str)
    offer_changed = Signal(object)
    checks_failing_changed = Signal(object)
    returnable_changed = Signal(object)
    return_found = Signal(object)
    return_not_found = Signal(str)

    def __init__(
        self,
        config: ConfigManager,
        parent: QObject | None = None,
        *,
        opener: Opener | None = None,
        public_keys: Sequence[str] = UPDATE_PUBLIC_KEYS,
        now: Callable[[], datetime] = _utc_now,
        installed_manifest: Path | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._opener = opener
        self._keys = tuple(decode_public_key(key) for key in public_keys)
        self._now = now
        self._task: Task | None = None
        #: Whether the check under way was asked for by the user.
        self._checking_manually = False
        #: What the check under way sent.
        self._sent = Check()
        self._found: ReleaseManifest | None = None
        self._offer: ReleaseManifest | None = None
        self._failing_since: datetime | None = None
        #: Where the signed manifest of the installed release is kept.
        self._installed_manifest = installed_manifest or Path(get_cache_dir()) / INSTALLED_MANIFEST_FILENAME
        self._returnable: Release | None = None
        #: Whether ``find_return`` waits for the check under way.
        self._return_wanted = False

        self._first_check = QTimer(self)
        self._first_check.setSingleShot(True)
        self._first_check.setInterval(UPDATE_CHECK_STARTUP_DELAY_S * MS_PER_SECOND)
        self._first_check.timeout.connect(self._check_automatically)
        self._next_check = QTimer(self)
        self._next_check.setInterval(UPDATE_CHECK_INTERVAL_HOURS * MS_PER_HOUR)
        self._next_check.timeout.connect(self._check_automatically)
        # Offers the release again when "Remind later" has run its time. A
        # coarse timer of hours may fire a little early; a precise one, set
        # for what is left and set again if it still fires early, does not
        # leave the offer hidden until the next check.
        self._snooze_end = QTimer(self)
        self._snooze_end.setSingleShot(True)
        self._snooze_end.setTimerType(Qt.TimerType.PreciseTimer)
        self._snooze_end.timeout.connect(self._reoffer)

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def found(self) -> ReleaseManifest | None:
        """The newer release the last check found, if any."""
        return self._found

    @property
    def offer(self) -> ReleaseManifest | None:
        """The release to keep offering (``offer_changed``)."""
        return self._offer

    @property
    def checks_failing_since(self) -> datetime | None:
        """Since when automatic checks have failed (``checks_failing_changed``)."""
        return self._failing_since

    @property
    def returnable(self) -> Release | None:
        """The release this installation can return to, as the signed
        manifest of the installed release names it; None while unknown."""
        return self._returnable

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
        self._read_installed_manifest()
        self.reconsider()
        self._first_check.start()
        self._next_check.start()

    def stop(self) -> None:
        self._first_check.stop()
        self._next_check.stop()

    def check_now(self) -> None:
        """Check because the user asked. The answer always arrives as a signal."""
        self._check(manual=True)

    def find_return(self) -> None:
        """Find the release to return to, checking if it is not known.

        The answer always arrives as ``return_found`` or
        ``return_not_found``. A check made for this is not one the user
        asked about updates: a newer release it finds is announced as one
        found in the background.
        """
        if self._returnable is not None:
            self.return_found.emit(self._returnable)
            return
        self._return_wanted = True
        if not self.checking:
            self._check(manual=False)

    def snooze(self) -> None:
        """Announce no update until the snooze time has passed, then offer
        it again."""
        self._config.update_snoozed_until = self._now() + timedelta(hours=UPDATE_SNOOZE_HOURS)
        self._config.save()
        self._set_offer(None)
        self._arm_snooze_end()

    def skip_version(self, version: str) -> None:
        """Never announce ``version`` by itself again."""
        self._config.skipped_update_version = version
        self._config.save()
        if self._offer is not None and self._offer.version == version:
            self._set_offer(None)

    def reconsider(self) -> None:
        """Say again whether checks are failing, after the preferences may
        have changed: the note is shown only while automatic checks are on."""
        since = self._config.update_failing_since
        due = since is not None and self._now() - since >= timedelta(days=UPDATE_CHECKS_FAILING_DAYS)
        failing = since if due and self._may_check_automatically() else None
        if failing != self._failing_since:
            self._failing_since = failing
            self.checks_failing_changed.emit(failing)

    # ── Checking ─────────────────────────────────────────────────────────

    def _may_check_automatically(self) -> bool:
        return self._config.check_updates_on_startup

    def _check_automatically(self) -> None:
        self.reconsider()
        if self._may_check_automatically():
            self._check(manual=False)

    def _check(self, *, manual: bool) -> None:
        if self.checking:
            return
        if not self._keys:
            logger.warning("This build holds no key to verify updates with")
            if manual:
                self.check_failed.emit(CHECK_NO_KEYS)
            self._settle_return(found=False)
            return
        check = self._prepare_check()
        headers = {"User-Agent": user_agent(), **check.headers}
        opener, keys = self._opener, self._keys

        def work(_task: Task) -> tuple[ReleaseManifest, str, bytes]:
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
                return read_signed_manifest(data, keys), day, data
            except ManifestError as exc:
                logger.warning("The update manifest was refused: %s", exc)
                raise ManifestError(CHECK_UNVERIFIED) from exc

        # The task announces its end on its own thread; bound methods of
        # this object are called on the main thread, where it lives.
        task = Task(work, (DownloadError, ManifestError), self)
        task.succeeded.connect(self._on_check_answered)
        task.failed.connect(self._on_check_failed)
        self._task, self._checking_manually, self._sent = task, manual, check
        task.start()

    def _prepare_check(self) -> Check:
        """What this check sends, from the preferences as the file holds
        them, with a change's number made and kept for every window."""
        prepared: list[Check] = []

        def change(stored: dict[str, str]) -> dict[str, str]:
            check, updates = prepare(stored, APP_VERSION)
            prepared.append(check)
            return updates

        if self._config.change_shared("Updates", change):
            return prepared[-1]
        # The preferences cannot be written now: the change is not stated,
        # since a number that is not kept could not be sent again.
        logger.warning("The update check states no version change: the preferences cannot be written")
        check, _ = prepare(self._config.updates_section(), APP_VERSION)
        return unstated(check)

    def _on_check_answered(self, result: tuple[ReleaseManifest, str, bytes]) -> None:
        manifest, day, data = result
        self._on_checked(manifest, day, data, manual=self._checking_manually)
        self._settle_return(found=self._returnable is not None)

    def _on_check_failed(self, reason: str) -> None:
        self._on_failed(reason, manual=self._checking_manually)
        self._settle_return(found=False, failed=True)

    def _on_checked(self, manifest: ReleaseManifest, day: str, data: bytes, *, manual: bool) -> None:
        # The server names its day only when it counted the check.
        if self._is_plausible_day(day):
            check = self._sent
            self._config.change_shared(
                "Updates", lambda stored: counted(stored, check, day, APP_VERSION),
            )

        if not self._is_fresh(manifest):
            self._found = None
            self._set_offer(None)
            logger.warning("An update manifest signed before the newest one already accepted was refused")
            self._note_failure()
            if manual:
                self.check_failed.emit(CHECK_OUTDATED)
            return

        self._note_answered()
        if manifest.version == APP_VERSION:
            self._keep_installed_manifest(manifest, data)
        if not is_newer(manifest.version, APP_VERSION):
            self._found = None
            self._set_offer(None)
            logger.info("No update: installed %s, latest %s", APP_VERSION, manifest.version)
            if manual:
                self.up_to_date.emit()
            return

        self._found = manifest
        held_back = self._held_back(manifest.version)
        self._set_offer(None if held_back else manifest)
        if held_back:
            # Held back by "Remind later" in this run or an earlier one:
            # offered again when its time has run.
            self._arm_snooze_end()
        if not manual and held_back:
            return
        logger.info("Update available: %s to %s", APP_VERSION, manifest.version)
        self.update_available.emit(manifest, manual)

    # ── The release to return to ─────────────────────────────────────────

    def _settle_return(self, *, found: bool, failed: bool = False) -> None:
        """Answer ``find_return`` once the check it waited for has ended."""
        if not self._return_wanted:
            return
        self._return_wanted = False
        if found:
            self.return_found.emit(self._returnable)
        else:
            self.return_not_found.emit(RETURN_CHECK_FAILED if failed else RETURN_UNKNOWN)

    def _set_returnable(self, release: Release | None) -> None:
        if release != self._returnable:
            self._returnable = release
            self.returnable_changed.emit(release)

    def _keep_installed_manifest(self, manifest: ReleaseManifest, data: bytes) -> None:
        """Keep ``data``, the verified manifest of the installed release,
        and the release before it that it names."""
        self._set_returnable(manifest.previous)
        try:
            os.makedirs(self._installed_manifest.parent, exist_ok=True)
            atomic_write_bytes(str(self._installed_manifest), data)
        except OSError as exc:
            logger.warning("The manifest of the installed release could not be kept: %s", exc)

    def _read_installed_manifest(self) -> None:
        """Take the release to return to from the manifest of the
        installed release kept by an earlier check, once it is verified.
        One that is missing, refused or of another release names none."""
        try:
            with open(self._installed_manifest, "rb") as kept:
                data = kept.read(UPDATE_MANIFEST_MAX_BYTES + 1)
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("The manifest of the installed release could not be read: %s", exc)
            return
        if len(data) > UPDATE_MANIFEST_MAX_BYTES or not self._keys:
            return
        try:
            manifest = read_signed_manifest(data, self._keys)
        except ManifestError as exc:
            logger.warning("The kept manifest of the installed release was refused: %s", exc)
            return
        if manifest.version == APP_VERSION:
            self._set_returnable(manifest.previous)

    def _set_offer(self, manifest: ReleaseManifest | None) -> None:
        if manifest != self._offer:
            self._offer = manifest
            self.offer_changed.emit(manifest)

    def _snoozed_until(self) -> datetime | None:
        """When "Remind later" ends. An end more than its hours from now,
        which a clock put back after the snooze, or a preferences file
        edited by hand, could give, is brought back to its hours from now
        and kept so, so that it ends."""
        until = self._config.update_snoozed_until
        if until is None:
            return None
        latest = self._now() + timedelta(hours=UPDATE_SNOOZE_HOURS)
        if until > latest:
            self._config.update_snoozed_until = until = latest
            self._config.save()
        return until

    def _arm_snooze_end(self) -> None:
        """Set the timer for what is left of "Remind later", if anything."""
        until = self._snoozed_until()
        if until is None:
            return
        left_ms = math.ceil((until - self._now()).total_seconds() * MS_PER_SECOND)
        if left_ms > 0:
            self._snooze_end.start(left_ms)

    def _reoffer(self) -> None:
        """When "Remind later" has run its time, offer the release found
        again; if the timer fired before that time, set it for what is
        left."""
        if self._found is None:
            return
        if self._held_back(self._found.version):
            self._arm_snooze_end()
            return
        self._set_offer(self._found)

    def _note_answered(self) -> None:
        """A check was answered: checks are not failing."""
        if self._config.update_failing_since is not None:
            self._config.update_failing_since = None
            self._config.save()
        self.reconsider()

    def _note_failure(self) -> None:
        """A check failed: remember since when, if not already failing."""
        if self._config.update_failing_since is None:
            self._config.update_failing_since = self._now()
            self._config.save()
        self.reconsider()

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

    def _held_back(self, version: str) -> bool:
        """Whether the user asked not to be told of ``version`` for now."""
        if version == self._config.skipped_update_version:
            return True
        until = self._snoozed_until()
        return until is not None and self._now() < until

    def _on_failed(self, reason: str, *, manual: bool) -> None:
        logger.warning("Update check failed: %s", reason)
        self._note_failure()
        if manual:
            self.check_failed.emit(reason)
