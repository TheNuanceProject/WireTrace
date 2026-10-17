# SPDX-License-Identifier: GPL-3.0-or-later
"""The workflows that check and release WireTrace.

They run only on GitHub, so what can be held to here is what they say:
that every action is pinned to an exact commit, that every download is
pinned to a checksum, and that they name files and versions that exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
RELEASE = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
LOCK_FILE = (ROOT / "requirements-release.txt").read_text(encoding="utf-8")

_USES = re.compile(r"^\s*(?:- )?uses:\s*(\S+)", re.MULTILINE)
_PINNED = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")
_LOCAL = "./"


def test_there_are_workflows_to_check() -> None:
    assert {path.name for path in WORKFLOWS} >= {"ci.yml", "release.yml"}


@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda path: path.name)
def test_every_action_is_pinned_to_a_commit(workflow: Path) -> None:
    """A tag can be moved to other code; a commit cannot."""
    used = _USES.findall(workflow.read_text(encoding="utf-8"))
    assert used
    for action in used:
        assert action.startswith(_LOCAL) or _PINNED.fullmatch(action), action


def test_every_download_of_the_release_is_pinned_to_a_checksum() -> None:
    addresses = re.findall(r"^\s*(\w+)_URL:\s*https://\S+$", RELEASE, re.MULTILINE)
    assert addresses
    checked_by_shell = 0
    for name in addresses:
        assert re.search(rf"^\s*{name}_SHA256:\s*[0-9a-f]{{64}}$", RELEASE, re.MULTILINE), name
        # Checked with sha256sum on Linux, or with Get-FileHash on Windows.
        by_shell = f'echo "${name}_SHA256 ' in RELEASE
        by_powershell = re.search(
            rf"Get-FileHash -Algorithm SHA256 .*\n.*-ne \$env:{name}_SHA256\) {{ throw", RELEASE,
        )
        assert by_shell or by_powershell, name
        checked_by_shell += by_shell
    assert RELEASE.count("sha256sum --check") == checked_by_shell


def test_the_release_is_built_with_the_python_the_lock_file_was_made_for() -> None:
    locked_for = re.search(r"--python-version (\d+\.\d+)", LOCK_FILE).group(1)
    assert re.search(rf"PYTHON_VERSION:\s*'{re.escape(locked_for)}'", RELEASE)


def test_dependencies_are_installed_only_from_the_lock_file_with_hashes() -> None:
    installs = re.findall(r"pip install (.+)", RELEASE)
    assert installs
    assert set(installs) == {"--require-hashes --requirement requirements-release.txt"}


def test_the_release_names_files_that_exist() -> None:
    """Files of the repository, that is; what a build produces has other endings."""
    named = set(re.findall(
        r"(?<![\w/.-])((?:build|tools|resources)/[\w./-]+\.(?:py|txt))", RELEASE,
    ))
    named.update(re.findall(r"uses: \./(\S+)", RELEASE))
    named.update(re.findall(r"(?<![\w/.-])(requirements[\w-]*\.txt)", RELEASE))
    assert {
        "build/build.py", "build/release_notes.txt", "tools/release_manifest.py",
        "tools/collect_licenses.py", "resources/licenses/THIRD_PARTY_LICENSES.txt",
        ".github/workflows/ci.yml", "requirements-release.txt",
    } <= named
    for path in named:
        assert (ROOT / path).is_file(), path


def test_a_release_is_only_ever_created_as_a_draft() -> None:
    created = [line for line in RELEASE.splitlines() if "gh release create" in line]
    assert len(created) == 1
    command = RELEASE[RELEASE.index("gh release create"):].split("\n\n", 1)[0]
    assert "--draft" in command
    assert "--verify-tag" in command


def test_only_the_release_job_may_write() -> None:
    assert re.search(r"^permissions:\n  contents: read$", RELEASE, re.MULTILINE)
    assert RELEASE.count("contents: write") == 1
    assert RELEASE.index("contents: write") > RELEASE.index("\n  release:\n")


def test_signing_runs_only_when_it_is_set_up_and_only_for_a_tag() -> None:
    assert "github.ref_type == 'tag' && vars.SIGNPATH_ORGANIZATION_ID != ''" in RELEASE
    steps = RELEASE.split("\n      - name: ")
    signing = [step for step in steps if "signpath/" in step or "signed" in step.split("\n")[0]]
    assert len(signing) >= 4
    for step in signing:
        assert "if: env.SIGN == 'true'" in step, step.split("\n")[0]
    assert "secrets.SIGNPATH_API_TOKEN" in RELEASE


def test_the_compiler_downloads_nothing_the_release_did_not_pin() -> None:
    build_script = (ROOT / "build" / "build.py").read_text(encoding="utf-8")
    assert '"--assume-yes-for-downloads"' not in build_script
    assert "NUITKA_CACHE_DIR_DOWNLOADS=$downloads" in RELEASE
    assert "$env:DEPENDS_URL" in RELEASE
