# WireTrace: build and release guide

How a WireTrace release is built, signed and published, and how to make
a build on your own computer.

---

## 1. What a release consists of

| File | What it is |
|---|---|
| `WireTrace-Setup-vX.Y.Z.exe` | Windows installer |
| `WireTrace-vX.Y.Z-x86_64.AppImage` | Linux, runs without installing; updates itself |
| `wiretrace_X.Y.Z_amd64.deb` | Linux package for Debian and Ubuntu |
| `SHA256SUMS` | SHA-256 of every file of the release |
| `THIRD_PARTY_LICENSES.txt` | Licence texts of everything a build contains |
| `WireTrace-vX.Y.Z.cdx.json` | Bill of materials (CycloneDX) |
| `manifest.unsigned.json` | The update manifest, before it is signed |

There is no macOS build. On macOS WireTrace is run from source.

The names, the version and every wording come from `version.py`,
`app/constants.py` and the `updater/` package. The build changes no
file that the repository tracks: what it makes goes to `build/dist/`
and `deployment/`, which Git ignores.

---

## 2. How a release is made

Releases are built by the **Release** workflow
(`.github/workflows/release.yml`) on GitHub's machines, never by hand.
The workflow ends in a **draft** release. Nothing is public until you
publish the draft.

### 2.1 Prepare

1. Confirm that `UPDATE_PUBLIC_KEYS` in `version.py` holds the public
   halves of your two keys (section 4). A build without them is
   refused.
2. Set `APP_VERSION` in `version.py`.
3. Write `build/release_notes.txt`. This is what the update window
   shows. Its first line must be `WireTrace vX.Y.Z`.
4. Update `CHANGELOG.md`: move what is under `[Unreleased]` to a
   section for the version, with its date.
5. If a dependency changed, follow section 6.
6. Run the checks (see [CONTRIBUTING.md](./CONTRIBUTING.md)).
7. Commit and push to `main`. Wait for CI to pass.

### 2.2 Tag

```powershell
git tag vX.Y.Z
git push origin vX.Y.Z
```

The workflow then:

1. Confirms the tag is the version in `version.py`.
2. Runs every check that CI runs.
3. Builds Linux on Ubuntu 22.04 and Windows on Windows Server 2022,
   from `requirements-release.txt`, where every package is pinned by
   hash.
4. Runs each build's own `--smoke-test`, and for Linux measures the
   oldest C library the build runs on.
5. Installs the `.deb` on a bare Ubuntu 22.04 and starts it.
6. Signs the Windows program and installer, once code signing is set
   up (section 5).
7. Creates the draft release with the files of section 1, and records
   a build provenance attestation for them.

### 2.3 Sign the update manifest

Installed copies of WireTrace accept an update only if its manifest is
signed with your private key (section 4). The signing tool needs the
GitHub command line tool, `gh`, signed in to your account. On your own
computer, in a checkout at the release tag, download every file of the
draft release into one folder and sign:

```powershell
gh release download vX.Y.Z --repo TheNuanceProject/WireTrace --dir release-check
python tools/sign_manifest.py release-check --key PATH-TO-YOUR-KEY --out signed
```

Before it asks anything, the tool:

1. Asks GitHub to confirm that each installer and
   `manifest.unsigned.json` were built by `.github/workflows/release.yml`
   of this repository, from the tag `vX.Y.Z`, on GitHub's own machines
   (`gh attestation verify`). A file built anywhere else stops it.
2. Recomputes the size and SHA-256 of every installer and checks them
   against the manifest and against `SHA256SUMS`. A file changed after
   the build stops it.

It then shows the version, the moment of signing it will record, the
files with their SHA-256 values and the release notes, and asks before
it signs. It verifies its own result exactly as an installed WireTrace
would, and writes:

```
signed\wiretrace\manifest.json    read by versions from 2.0
signed\wiretrace-update.json      read by 1.x versions
```

The moment of signing is part of what is signed. An installed WireTrace
remembers the latest it has accepted and refuses a manifest signed
earlier, so an old manifest cannot be served again to hold users back.
Sign on a computer whose clock is right.

### 2.4 Publish

1. Publish the draft release on GitHub. The downloads now exist.
2. Copy the contents of `signed\` into the website's `updates` folder,
   keeping the folder structure, and deploy the website.

In that order: a manifest must never name a file that cannot be
downloaded yet.

### 2.5 Rehearsal

Start the Release workflow by hand (Actions > Release > Run workflow)
to build and check everything without creating a release. The result
is kept as the workflow artifact `release-rehearsal`. A rehearsal is
never code-signed, also once code signing is set up: only a tag is.

---

## 3. Building on your own computer

A build is made on the system it is for.

### 3.1 Windows

1. Python 3.12, 64-bit.
2. Visual Studio Build Tools with "Desktop development with C++".
3. [Inno Setup 6](https://jrsoftware.org/isdl.php).
4. The locked dependencies:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --require-hashes --requirement requirements-release.txt
```

Build:

```powershell
python build/build.py
```

The first build asks whether the compiler may download Dependency
Walker, which it uses to find the libraries the program needs. Answer
yes; it is kept for later builds. The release workflow instead fetches
it itself and checks it against the SHA-256 in `release.yml`, and the
compiler is not allowed to download anything else.

Result: `deployment\windows\WireTrace-Setup-vX.Y.Z.exe`

### 3.2 Linux

Build on Ubuntu 22.04, the baseline: a build made on a newer system
needs that system's C library, and is refused. The build reads licence
texts from the package system, so it is made on Debian or Ubuntu.

Ubuntu 22.04's own Python is 3.10. The build uses Python 3.12, the
version the lock file was made for, so install it separately first
(for example with pyenv or uv).

```bash
sudo apt install build-essential patchelf dpkg-dev
python3.12 -m venv .venv && source .venv/bin/activate
python -m pip install --require-hashes --requirement requirements-release.txt
```

The build carries along the libraries Qt's X11 plugin needs, so they
must be installed where it is made. If one is missing, the build stops
and names it. The release workflow lists the packages.

`appimagetool` must be on the `PATH`. The release workflow names the
version it uses and its SHA-256. `appimagetool` downloads the AppImage
runtime it embeds unless it is given one; to build from a runtime file
you have checked yourself, as the release workflow does, pass
`--appimage-runtime PATH-TO-RUNTIME`.

Build, on a desktop or under `xvfb-run`:

```bash
python build/build.py
```

Results: `deployment/linux/WireTrace-vX.Y.Z-x86_64.AppImage` and
`deployment/linux/wiretrace_X.Y.Z_amd64.deb`

### 3.3 Stages

```
python build/build.py --stage compile    # the checked program folder, build/dist/WireTrace/
python build/build.py --stage package    # the installer or packages, from that folder
```

The release workflow runs the two stages separately, so that the
Windows program can be code-signed before the installer is made from
it.

### 3.4 What the build checks

The build stops at the first check that fails. Nothing is packaged
from a build that failed.

| Check | Why |
|---|---|
| `UPDATE_PUBLIC_KEYS` holds a readable key | A build that trusts no key could never be updated |
| The licence file lists the installed packages at their versions | The texts shipped must be those of what is shipped |
| Linux: every library the X11 plugin needs is in the build or on the AppImage project's exclude list | Stock installations of several distributions lack one or another of them, and Qt cannot open a window without them |
| The licence text of each part of the build system that the build carries is found | The interpreter, and on Linux each system library, must travel with its licence |
| The files the program reads at run time are in the build | The guide, icons, fonts, stylesheet and licence texts are files beside the program |
| `--smoke-test` passes on the built program | Imports everything loaded late, starts the window system, draws a plot, fills the guide, verifies a known signature, reads the licence texts |
| Linux: glibc needed is at most 2.35 | The build must run on the baseline system |
| Linux: the finished AppImage passes `--smoke-test` | It is the file the update check installs |

The `.deb` states what it depends on as `dpkg-shlibdeps` works it out
from the libraries the program loads; nothing is listed by hand.

If the smoke test names a missing module, add it to `nuitka_command()`
in `build/build.py`: `--include-package=NAME` for a whole package,
`--include-module=NAME.PART` for one module of a package that is
already included.

---

## 4. The keys that sign update manifests

Two Ed25519 keys: one to work with and a spare. Their public halves are
in `UPDATE_PUBLIC_KEYS` in `version.py`. The private halves are never in
the repository, on GitHub or in a build.

Make a key (run it twice, once for each key, with different paths
outside the repository):

```powershell
python tools/release_keys.py new PATH-TO-NEW-KEY-FILE
```

It prints the public key to add to `UPDATE_PUBLIC_KEYS`. To print it
again later:

```powershell
python tools/release_keys.py show PATH-TO-KEY-FILE
```

If the working key is lost or may have been seen by someone else: sign
the next release with the spare, and in that release replace the lost
key's public half in `UPDATE_PUBLIC_KEYS` with that of a newly made
key. Copies that have installed that release then no longer accept the
lost key.

A key cannot be recovered without its file and its passphrase. If both
keys are lost, installed copies can no longer be updated from within;
their users would have to install a new version by hand.

---

## 5. Windows code signing

The release workflow signs the Windows program and installer through
[SignPath](https://signpath.io) when these are set in the repository
(Settings > Secrets and variables > Actions):

| Kind | Name | Value |
|---|---|---|
| Secret | `SIGNPATH_API_TOKEN` | API token of a SignPath user who may submit |
| Variable | `SIGNPATH_ORGANIZATION_ID` | The SignPath organisation |
| Variable | `SIGNPATH_PROJECT_SLUG` | The SignPath project |
| Variable | `SIGNPATH_SIGNING_POLICY_SLUG` | The signing policy to use |

Until `SIGNPATH_ORGANIZATION_ID` is set, the same workflow produces an
unsigned installer.

The SignPath project needs two artifact configurations, named
`program` and `installer`. Each receives a ZIP that holds one file,
`WireTrace.exe` or the installer, and signs it with Authenticode. Only
WireTrace's own executable is signed; the libraries of others inside
the build are left as their makers shipped them. The SignPath GitHub
App must be installed on the repository.

---

## 6. Changing a dependency

1. Edit `requirements.txt` (what the program needs) or
   `requirements-build.txt` (what builds it).
2. Regenerate the lock file:

   ```bash
   uv pip compile requirements.txt requirements-build.txt --universal \
       --python-version 3.12 --generate-hashes -o requirements-release.txt
   ```

3. In an environment installed from the new lock file, regenerate the
   licence file:

   ```bash
   python tools/collect_licenses.py
   ```

   A package that ships no licence text of its own must have one named
   in `resources/licenses/components.json`; the tool says so.
4. Run the tests. They fail if the lock file and the licence file
   disagree.

---

## 7. The Windows installer

Installs for the current user (no administrator rights needed) or for
all users; the person installing chooses.

Before anything is installed it shows the privacy statement: what
stays on the computer, what an update check sends, and what usage
statistics send if the user switches them on. It is the policy itself,
as SignPath Foundation's terms ask, not a summary. The text is
`PRIVACY_STATEMENT` in `updater/privacy.py`, beside the code it
describes, and tests hold it to the fields that code sends.

Among its options is the update check, ticked unless the person
installing unticks it. The choice is written to `install.ini` beside
the program, and WireTrace takes it over on its first start; after
that, the setting in Preferences is the only one that counts. The
option is not offered when an installation that already holds a choice
is updated, and a silent installation records nothing. On a computer
where WireTrace has been used before, the setting already in
Preferences is kept whatever the installer recorded.

Uninstalling removes the program and its working files. Settings and
logs are left as they are.
