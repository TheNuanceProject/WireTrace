# SPDX-License-Identifier: GPL-3.0-or-later
"""The tools a release is made with: the signing keys, the description of
a release, and the signing of its manifest."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tools import release_keys, release_manifest, sign_manifest
from updater.manifest import (
    ManifestError,
    build_payload,
    decode_public_key,
    expected_file_name,
    key_id,
    parse_manifest,
    read_signed_manifest,
)
from version import (
    APP_NAME,
    APP_VERSION,
    RELEASES_BASE_URL,
    UPDATE_LEGACY_JSON_FILE,
    UPDATE_MANIFEST_URL,
    UPDATE_MIN_VERSION,
)

PASSPHRASE = "a passphrase of some length"
SIGNED = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)
PLATFORMS = ("windows-x86_64", "linux-x86_64")


@pytest.fixture
def key_file(tmp_path):
    path = tmp_path / "release.key"
    release_keys.write_new_key(path, PASSPHRASE)
    return path


@pytest.fixture
def release_folder(tmp_path):
    """A folder holding an installer for each platform, and one more file."""
    folder = tmp_path / "release"
    folder.mkdir()
    for index, key in enumerate(PLATFORMS):
        (folder / expected_file_name(key, APP_VERSION)).write_bytes(b"installer" * (index + 1))
    (folder / "THIRD_PARTY_LICENSES.txt").write_text("licences", encoding="utf-8")
    return folder


@pytest.fixture
def trusted(key_file, monkeypatch):
    """The key in ``key_file`` is one this version trusts."""
    key = release_keys.load_key(key_file, PASSPHRASE)
    monkeypatch.setattr(sign_manifest, "UPDATE_PUBLIC_KEYS", (release_keys.public_text(key),))
    return key


class TestKeys:
    def test_a_new_key_is_read_back_with_its_passphrase(self, key_file):
        key = release_keys.load_key(key_file, PASSPHRASE)
        assert len(decode_public_key(release_keys.public_text(key))) == 32

    def test_the_file_is_encrypted(self, key_file):
        content = key_file.read_text(encoding="ascii")
        assert "ENCRYPTED PRIVATE KEY" in content

    @pytest.mark.skipif(os.name != "posix", reason="file modes are a POSIX matter")
    def test_the_file_is_for_its_owner_only(self, key_file):
        assert key_file.stat().st_mode & 0o777 == 0o600

    def test_a_wrong_passphrase_is_refused(self, key_file):
        with pytest.raises(release_keys.SigningKeyError):
            release_keys.load_key(key_file, PASSPHRASE + "x")

    def test_a_file_that_is_not_a_key_is_refused(self, tmp_path):
        path = tmp_path / "not.key"
        path.write_text("not a key", encoding="utf-8")
        with pytest.raises(release_keys.SigningKeyError):
            release_keys.load_key(path, PASSPHRASE)

    def test_a_missing_file_is_reported(self, tmp_path):
        with pytest.raises(release_keys.SigningKeyError):
            release_keys.load_key(tmp_path / "absent.key", PASSPHRASE)

    def test_a_short_passphrase_is_refused(self, tmp_path):
        path = tmp_path / "short.key"
        with pytest.raises(release_keys.SigningKeyError):
            release_keys.write_new_key(path, "x" * (release_keys.MIN_PASSPHRASE_LENGTH - 1))
        assert not path.exists()

    def test_an_existing_file_is_never_written_over(self, key_file):
        before = key_file.read_bytes()
        with pytest.raises(release_keys.SigningKeyError):
            release_keys.write_new_key(key_file, PASSPHRASE)
        assert key_file.read_bytes() == before

    def test_a_key_is_not_written_inside_the_repository(self):
        path = release_keys.ROOT / "release.key"
        with pytest.raises(release_keys.SigningKeyError):
            release_keys.write_new_key(path, PASSPHRASE)
        assert not path.exists()

    def test_new_prints_the_public_half_and_never_the_private(
        self, tmp_path, monkeypatch, capsys,
    ):
        path = tmp_path / "new.key"
        monkeypatch.setattr(release_keys.getpass, "getpass", lambda prompt: PASSPHRASE)
        assert release_keys.main(["new", str(path)]) == 0
        printed = capsys.readouterr().out
        key = release_keys.load_key(path, PASSPHRASE)
        assert release_keys.public_text(key) in printed
        assert key_id(release_keys.public_bytes(key)) in printed
        assert "PRIVATE" not in printed
        assert PASSPHRASE not in printed

    def test_new_refuses_two_different_passphrases(self, tmp_path, monkeypatch, capsys):
        answers = iter([PASSPHRASE, PASSPHRASE + "x"])
        monkeypatch.setattr(release_keys.getpass, "getpass", lambda prompt: next(answers))
        assert release_keys.main(["new", str(tmp_path / "new.key")]) == 1
        assert not (tmp_path / "new.key").exists()

    def test_show_prints_the_same_public_half(self, key_file, monkeypatch, capsys):
        monkeypatch.setattr(release_keys.getpass, "getpass", lambda prompt: PASSPHRASE)
        assert release_keys.main(["show", str(key_file)]) == 0
        key = release_keys.load_key(key_file, PASSPHRASE)
        assert release_keys.public_text(key) in capsys.readouterr().out


class TestReleaseNotes:
    def test_the_notes_in_the_repository_are_those_of_this_version(self):
        assert release_manifest.release_notes().startswith(f"{APP_NAME} v{APP_VERSION}")

    def test_notes_of_another_version_are_refused(self, tmp_path):
        path = tmp_path / "notes.txt"
        path.write_text(f"{APP_NAME} v0.0.1\n\nOld news.\n", encoding="utf-8")
        with pytest.raises(release_manifest.ReleaseError):
            release_manifest.release_notes(path)

    def test_missing_notes_are_reported(self, tmp_path):
        with pytest.raises(release_manifest.ReleaseError):
            release_manifest.release_notes(tmp_path / "absent.txt")


class TestDescribingARelease:
    def test_the_manifest_states_the_size_and_hash_of_each_installer(self, release_folder):
        manifest = release_manifest.build_manifest(release_folder, "notes")
        assert set(manifest.platforms) == set(PLATFORMS)
        for key, release in manifest.platforms.items():
            content = (release_folder / expected_file_name(key, APP_VERSION)).read_bytes()
            assert release.file_size == len(content)
            assert release.sha256 == hashlib.sha256(content).hexdigest()
        assert manifest.version == APP_VERSION
        assert manifest.min_version == UPDATE_MIN_VERSION

    def test_a_missing_installer_is_an_error(self, release_folder):
        (release_folder / expected_file_name("linux-x86_64", APP_VERSION)).unlink()
        with pytest.raises(release_manifest.ReleaseError):
            release_manifest.build_manifest(release_folder, "notes")

    def test_the_unsigned_manifest_is_one_the_application_reads(self, release_folder):
        manifest = release_manifest.describe_release(release_folder)
        payload = (release_folder / release_manifest.UNSIGNED_MANIFEST_NAME).read_bytes()
        assert parse_manifest(payload) == manifest
        assert manifest.release_notes == release_manifest.release_notes()

    def test_the_checksums_cover_every_file_but_themselves(self, release_folder):
        release_manifest.describe_release(release_folder)
        lines = (release_folder / release_manifest.CHECKSUMS_NAME).read_text(
            encoding="utf-8",
        ).splitlines()
        listed = {}
        for line in lines:
            digest, name = line.split("  ", 1)
            listed[name] = digest
        present = {path.name for path in release_folder.iterdir()}
        assert set(listed) == present - {release_manifest.CHECKSUMS_NAME}
        assert list(listed) == sorted(listed)
        for name, digest in listed.items():
            assert digest == hashlib.sha256((release_folder / name).read_bytes()).hexdigest()

    def test_main_reports_a_folder_without_installers(self, tmp_path, capsys):
        assert release_manifest.main([str(tmp_path)]) == 1
        assert "error:" in capsys.readouterr().err


class TestSigning:
    @pytest.fixture
    def described(self, release_folder):
        """The release folder as the release build leaves it."""
        release_manifest.describe_release(release_folder)
        return release_folder

    @pytest.fixture
    def manifest(self, release_folder):
        return sign_manifest.stamped(release_manifest.build_manifest(release_folder, "notes"), SIGNED)

    def test_the_signed_manifest_is_accepted_by_the_application(self, manifest, key_file, trusted):
        envelope, signed = sign_manifest.sign(manifest, key_file, PASSPHRASE)
        assert read_signed_manifest(envelope, [release_keys.public_bytes(trusted)]) == signed == manifest

    def test_it_carries_the_moment_of_signing_to_the_second(self, release_folder):
        moment = datetime(2026, 10, 14, 9, 30, 15, 987654, tzinfo=timezone.utc)
        manifest = sign_manifest.stamped(release_manifest.build_manifest(release_folder, "notes"), moment)
        assert manifest.signed_at == datetime(2026, 10, 14, 9, 30, 15, tzinfo=timezone.utc)
        assert b'"signed_at": "2026-10-14T09:30:15Z"' in build_payload(manifest)

    def test_a_manifest_without_a_moment_of_signing_is_refused(self, release_folder, key_file, trusted):
        with pytest.raises(ManifestError):
            sign_manifest.sign(release_manifest.build_manifest(release_folder, "notes"), key_file, PASSPHRASE)

    def test_the_signature_covers_the_payload_exactly_as_it_was_given(self, manifest, key_file, trusted):
        envelope, _ = sign_manifest.sign(manifest, key_file, PASSPHRASE)
        signature = json.loads(envelope)["signatures"][0]
        assert signature["key"] == key_id(release_keys.public_bytes(trusted))
        assert base64.b64decode(json.loads(envelope)["payload"]) == build_payload(manifest)

    def test_a_key_the_application_does_not_trust_is_refused(self, manifest, key_file):
        with pytest.raises(ManifestError):
            sign_manifest.sign(manifest, key_file, PASSPHRASE)

    def test_a_manifest_of_another_version_is_refused(self, tmp_path, key_file, trusted):
        folder = tmp_path / "other"
        folder.mkdir()
        for key in PLATFORMS:
            (folder / expected_file_name(key, "9.9.9")).write_bytes(b"installer")
        manifest = sign_manifest.stamped(release_manifest.build_manifest(folder, "notes", "9.9.9"), SIGNED)
        with pytest.raises(ManifestError):
            sign_manifest.sign(manifest, key_file, PASSPHRASE)

    def test_a_wrong_passphrase_signs_nothing(self, manifest, key_file, trusted):
        with pytest.raises(release_keys.SigningKeyError):
            sign_manifest.sign(manifest, key_file, "wrong passphrase!")

    def test_both_manifests_are_written_where_the_website_serves_them(self, manifest, key_file, trusted, tmp_path):
        envelope, signed = sign_manifest.sign(manifest, key_file, PASSPHRASE)
        out = tmp_path / "updates"
        signed_path, legacy = sign_manifest.write_signed(out, envelope, signed)
        assert signed_path.read_bytes() == envelope
        assert UPDATE_MANIFEST_URL.endswith("/" + signed_path.relative_to(out).as_posix())
        assert legacy == out / UPDATE_LEGACY_JSON_FILE

    def test_the_legacy_manifest_is_in_the_form_1x_reads(self, manifest):
        legacy = json.loads(sign_manifest.legacy_manifest(manifest))
        assert legacy["latest_version"] == APP_VERSION
        assert legacy["min_version"] == UPDATE_MIN_VERSION
        assert legacy["release_notes"] == "notes"
        assert set(legacy["platforms"]) == {"windows", "linux"}
        windows = legacy["platforms"]["windows"]
        release = manifest.platforms["windows-x86_64"]
        assert windows == {
            "download_url": f"{RELEASES_BASE_URL}/v{APP_VERSION}/{release.file_name}",
            "file_size": release.file_size,
            "sha256_hash": release.sha256,
        }

    def test_what_the_update_window_will_show_is_printed_before_signing(self, manifest):
        printed = sign_manifest.summary(manifest)

        assert manifest.release_notes.strip()
        for line in manifest.release_notes.splitlines():
            assert line in printed
        assert "2026-10-14T09:30:00Z" in printed

    # ── Where the files came from ────────────────────────────────────────

    def test_github_is_asked_about_every_installer_and_the_manifest(self, described):
        asked: list[str] = []

        manifest = sign_manifest.check_release(described, lambda path: asked.append(path.name))

        assert sorted(asked) == sorted(
            [release_manifest.UNSIGNED_MANIFEST_NAME, *(r.file_name for r in manifest.platforms.values())]
        )

    def test_github_is_asked_for_this_repository_workflow_and_tag(self):
        command = sign_manifest.attestation_command("gh", Path("file"), "2.0.0")

        assert command[:4] == ["gh", "attestation", "verify", "file"]
        assert command[command.index("--repo") + 1] == "TheNuanceProject/WireTrace"
        assert command[command.index("--signer-workflow") + 1] == (
            "TheNuanceProject/WireTrace/.github/workflows/release.yml"
        )
        assert command[command.index("--source-ref") + 1] == "refs/tags/v2.0.0"
        assert "--deny-self-hosted-runners" in command

    def test_a_file_github_does_not_confirm_stops_everything(self, described):
        def refuse(path: Path) -> None:
            raise sign_manifest.ProvenanceError(f"{path.name} was not confirmed")

        with pytest.raises(sign_manifest.ProvenanceError):
            sign_manifest.check_release(described, refuse)

    def test_an_installer_changed_after_the_build_is_refused(self, described):
        installer = next(described.glob("*.exe"), None) or next(
            path for path in described.iterdir() if path.name.startswith(APP_NAME)
        )
        installer.write_bytes(installer.read_bytes() + b"x")

        with pytest.raises(ManifestError):
            sign_manifest.check_release(described, lambda path: None)

    @pytest.mark.parametrize("named", ["installer", "manifest"])
    def test_checksums_that_disagree_with_the_files_are_refused(self, described, named):
        manifest = parse_manifest((described / release_manifest.UNSIGNED_MANIFEST_NAME).read_bytes())
        name = (
            manifest.platforms["windows-x86_64"].file_name if named == "installer"
            else release_manifest.UNSIGNED_MANIFEST_NAME
        )
        sums = described / release_manifest.CHECKSUMS_NAME
        lines = [
            f"{'0' * 64}  {name}" if line.endswith(f"  {name}") else line
            for line in sums.read_text(encoding="utf-8").splitlines()
        ]
        sums.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(ManifestError):
            sign_manifest.check_release(described, lambda path: None)

    def test_a_missing_installer_is_refused(self, described):
        manifest = parse_manifest((described / release_manifest.UNSIGNED_MANIFEST_NAME).read_bytes())
        (described / manifest.platforms["linux-x86_64"].file_name).unlink()

        with pytest.raises(OSError):
            sign_manifest.check_release(described, lambda path: None)

    # ── The command ──────────────────────────────────────────────────────

    def _run_main(self, monkeypatch, tmp_path, folder, key_file, answer, verify=lambda path: None):
        monkeypatch.setattr("builtins.input", lambda prompt: answer)
        monkeypatch.setattr(sign_manifest.getpass, "getpass", lambda prompt: PASSPHRASE)
        out = tmp_path / "out"
        code = sign_manifest.main(
            [str(folder), "--key", str(key_file), "--out", str(out)],
            verifier=lambda version: verify, now=lambda: SIGNED,
        )
        return code, out

    def test_main_signs_after_a_yes(self, monkeypatch, tmp_path, described, key_file, trusted, capsys):
        code, out = self._run_main(monkeypatch, tmp_path, described, key_file, "yes")
        assert code == 0
        assert sign_manifest.signed_manifest_path(out).is_file()
        assert (out / UPDATE_LEGACY_JSON_FILE).is_file()
        printed = capsys.readouterr().out
        manifest = parse_manifest((described / release_manifest.UNSIGNED_MANIFEST_NAME).read_bytes())
        for release in manifest.platforms.values():
            assert release.sha256 in printed

    def test_main_writes_nothing_without_a_yes(self, monkeypatch, tmp_path, described, key_file, trusted):
        code, out = self._run_main(monkeypatch, tmp_path, described, key_file, "y")
        assert code == 1
        assert not out.exists()

    def test_main_writes_nothing_with_an_untrusted_key(self, monkeypatch, tmp_path, described, key_file, capsys):
        code, out = self._run_main(monkeypatch, tmp_path, described, key_file, "yes")
        assert code == 1
        assert not out.exists()
        assert "error:" in capsys.readouterr().err

    def test_main_writes_nothing_when_github_does_not_confirm(
        self, monkeypatch, tmp_path, described, key_file, trusted, capsys,
    ):
        def refuse(path: Path) -> None:
            raise sign_manifest.ProvenanceError("not built by the release workflow")

        code, out = self._run_main(monkeypatch, tmp_path, described, key_file, "yes", refuse)
        assert code == 1
        assert not out.exists()
        assert "not built by the release workflow" in capsys.readouterr().err
