# SPDX-License-Identifier: GPL-3.0-or-later
"""Collect the licence texts of everything a WireTrace build contains.

A build carries other people's work: Qt, numpy, the Python interpreter
and more. Their licences require that their texts and notices travel
with the binary. This tool writes them into one file,
``resources/licenses/THIRD_PARTY_LICENSES.txt``, which the build
includes and the About window shows.

What is collected:
  - Every package named in ``requirements.txt`` and every package those
    need in turn, as installed in the environment the tool runs in.
    Each package's own licence files are used. A package that ships
    none must have its texts named in ``resources/licenses/components.json``;
    otherwise the tool fails, so nothing can be left out by accident.
  - The bundled font and the runtime of the compiler, as that file
    describes them.

What a build carries of the system it is made on (the Python
interpreter, and on Linux some system libraries) differs from build to
build. The build adds those texts itself; see ``build/build.py``.

Run it in an environment installed from ``requirements-release.txt``, so
that the versions written are the versions a release contains.

The same list can be written as a software bill of materials (CycloneDX
JSON), which a release carries beside its installers.

Usage:
    python tools/collect_licenses.py              # write the file
    python tools/collect_licenses.py --check      # verify it lists what is installed (exit 1 if not)
    python tools/collect_licenses.py --sbom PATH  # write the bill of materials to PATH
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import sys
import sysconfig
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.licenses import ENCODING, SECTION_RULE, section_heading
from version import APP_LICENSE, APP_NAME, APP_VERSION

REQUIREMENTS = ROOT / "requirements.txt"
LICENSES_DIR = ROOT / "resources" / "licenses"
COMPONENTS = LICENSES_DIR / "components.json"
TEXTS_DIR = LICENSES_DIR / "texts"
OUTPUT = LICENSES_DIR / "THIRD_PARTY_LICENSES.txt"

RULE = SECTION_RULE
THIN_RULE = "-" * 78
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_LICENSE_FILE = re.compile(r"(LICEN[CS]E|COPYING|NOTICE)", re.IGNORECASE)
_INTERPRETER_LICENSE = "LICENSE.txt"
#: A section's heading: a name, then a version if the component has one.
_HEADING = re.compile(rf"^{RULE}\n(.+?)(?: (\d\S*))?\n{RULE}$", re.MULTILINE)
_SBOM_FORMAT = "CycloneDX"
_SBOM_VERSION = "1.6"
_SBOM_SCHEMA = "http://cyclonedx.org/schema/bom-1.6.schema.json"


class LicenseError(RuntimeError):
    """A component's licence text could not be found."""


def normalise(name: str) -> str:
    """A package name as the packaging standard compares names."""
    return re.sub(r"[-_.]+", "-", name).lower()


def required_names(requirements: Path) -> list[str]:
    """The package names a requirements file lists."""
    names = []
    for line in requirements.read_text(encoding=ENCODING).splitlines():
        match = _NAME.match(line.strip())
        if match and not line.lstrip().startswith(("#", "-")):
            names.append(normalise(match.group(0)))
    return names


def runtime_packages(roots: list[str]) -> dict[str, metadata.Distribution]:
    """The root packages and everything they need, as installed here.

    A requirement that belongs to an optional extra is not followed. One
    that does not apply to this platform is simply not installed, and is
    left out for that reason.
    """
    installed = {normalise(dist.metadata["Name"]): dist for dist in metadata.distributions()}
    found: dict[str, metadata.Distribution] = {}
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in found:
            continue
        if name not in installed:
            if name in roots:
                raise LicenseError(f"{name} is required but not installed in this environment")
            continue
        found[name] = installed[name]
        for requirement in installed[name].requires or []:
            specification, _, marker = requirement.partition(";")
            if "extra" in marker:
                continue
            match = _NAME.match(specification.strip())
            if match:
                pending.append(normalise(match.group(0)))
    return dict(sorted(found.items()))


def _source_address(dist: metadata.Distribution) -> str:
    """Where a package says its source or home is."""
    addresses = {}
    for entry in dist.metadata.get_all("Project-URL") or []:
        label, _, address = entry.partition(",")
        addresses[label.strip().lower()] = address.strip()
    for label in ("repository", "source", "source code", "homepage"):
        if label in addresses:
            return addresses[label]
    return dist.metadata.get("Home-page") or ""


def _own_license_files(dist: metadata.Distribution) -> list[tuple[str, str]]:
    """``(name, text)`` of each licence file a package ships in its metadata."""
    texts = []
    for file in sorted(dist.files or [], key=str):
        parts = file.parts
        if not parts or not parts[0].endswith(".dist-info"):
            continue
        if not _LICENSE_FILE.search(file.name) and "licenses" not in parts:
            continue
        if file.name in {"METADATA", "RECORD", "WHEEL", "INSTALLER", "REQUESTED"}:
            continue
        texts.append(("/".join(parts[1:]), file.read_text(encoding=ENCODING)))
    return texts


def _license_name(dist: metadata.Distribution) -> str:
    expression = dist.metadata.get("License-Expression")
    if expression:
        return expression
    declared = (dist.metadata.get("License") or "").strip()
    if declared and "\n" not in declared:
        return declared
    classifiers = [
        value.rsplit("::", 1)[-1].strip()
        for value in dist.metadata.get_all("Classifier") or []
        if value.startswith("License ::")
    ]
    return ", ".join(classifiers)


def interpreter_license() -> Path:
    """The licence file of the interpreter this runs on: in its library
    folder, or on Windows at the top of its installation.

    Raises:
        LicenseError: it is in neither place.
    """
    candidates = [
        Path(sysconfig.get_path("stdlib")) / _INTERPRETER_LICENSE,
        Path(sys.base_prefix) / _INTERPRETER_LICENSE,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise LicenseError(
        "the interpreter's licence was not found at "
        + " or ".join(str(candidate) for candidate in candidates)
    )


@dataclass(frozen=True)
class Component:
    """One piece of other people's work that a build contains."""

    name: str
    version: str
    license: str
    url: str
    note: str
    texts: tuple[tuple[str, str], ...]
    #: Its name on the Python Package Index, if it comes from there.
    package: str = ""


def components() -> list[Component]:
    """Everything a build contains, with the texts of its licences.

    Raises:
        LicenseError: a licence text could not be found.
    """
    described = json.loads(COMPONENTS.read_text(encoding=ENCODING))
    supplements: dict[str, list[str]] = described["supplements"]
    notes: dict[str, str] = described["notes"]

    found: list[Component] = []
    for key, dist in runtime_packages(required_names(REQUIREMENTS)).items():
        texts = _own_license_files(dist)
        if not texts:
            texts = [
                (file_name, (TEXTS_DIR / file_name).read_text(encoding=ENCODING))
                for file_name in supplements.get(key, [])
            ]
        if not texts:
            raise LicenseError(
                f"{dist.metadata['Name']} ships no licence text and none is named for it in "
                f"{COMPONENTS.relative_to(ROOT)}"
            )
        found.append(Component(
            name=dist.metadata["Name"], version=dist.version, license=_license_name(dist),
            url=_source_address(dist), note=notes.get(key, ""), texts=tuple(texts), package=key,
        ))

    for entry in described["files"]:
        path = ROOT / entry["path"]
        found.append(Component(
            name=entry["name"], version="", license=entry["license"], url=entry["url"], note="",
            texts=((path.name, path.read_text(encoding=ENCODING)),),
        ))

    runtime = described["build_runtime"]
    compiler = metadata.distribution(runtime["package"])
    compiler_texts = tuple(
        (title, text) for title, text in _own_license_files(compiler)
        if Path(title).name in runtime["license_files"]
    )
    if len(compiler_texts) != len(runtime["license_files"]):
        raise LicenseError(f"{runtime['package']} does not ship {runtime['license_files']}")
    found.append(Component(
        name=runtime["name"], version=compiler.version, license=runtime["license"],
        url=_source_address(compiler), note="", texts=compiler_texts,
        package=normalise(runtime["package"]),
    ))
    return found


def collect() -> str:
    """The whole licence file, as text.

    A text that was already written in full is referred to instead of
    being written again.
    """
    sections: list[str] = []
    written: dict[str, str] = {}
    for component in components():
        heading = f"{component.name} {component.version}".strip()
        lines = [section_heading(heading)]
        if component.license:
            lines.append(f"Licence: {component.license}")
        if component.url:
            lines.append(f"Source:  {component.url}")
        if component.note:
            lines.extend(["", component.note])
        for title, text in component.texts:
            body = text.strip("\n")
            first = written.setdefault(body, f"{heading}, {title}")
            lines.extend(["", THIN_RULE, title, THIN_RULE, ""])
            if first == f"{heading}, {title}":
                lines.append(body)
            else:
                lines.append(f"The same text as given above under: {first}")
        sections.append("\n".join(lines) + "\n")

    header = (
        "Third-party licences\n\n"
        "WireTrace contains the work of others, listed here with the licence each is\n"
        "used under. Generated by tools/collect_licenses.py; do not edit by hand.\n\n"
    )
    return header + "\n".join(sections)


def listed_versions(text: str) -> dict[str, str]:
    """``name -> version`` of each component a licence file lists."""
    return {name: version or "" for name, version in _HEADING.findall(text)}


def installed_versions() -> dict[str, str]:
    """``name -> version`` of each versioned component as installed here,
    named as the licence file names it."""
    described = json.loads(COMPONENTS.read_text(encoding=ENCODING))
    versions = {
        dist.metadata["Name"]: dist.version
        for dist in runtime_packages(required_names(REQUIREMENTS)).values()
    }
    runtime = described["build_runtime"]
    versions[runtime["name"]] = metadata.version(runtime["package"])
    return versions


def differences(text: str) -> list[str]:
    """How the licence file ``text`` differs from what is installed here.

    Compared by name and version: the same version of a package carries
    the same licence text on every system, so this holds on each system
    a build is made on.
    """
    listed = {name: version for name, version in listed_versions(text).items() if version}
    installed = installed_versions()
    found = []
    for name in sorted(set(listed) | set(installed)):
        if name not in installed:
            found.append(f"{name} {listed[name]} is listed but not installed")
        elif name not in listed:
            found.append(f"{name} {installed[name]} is installed but not listed")
        elif listed[name] != installed[name]:
            found.append(f"{name}: {listed[name]} is listed, {installed[name]} is installed")
    return found


def bill_of_materials() -> dict:
    """What a build contains, as a CycloneDX document."""
    interpreter = json.loads(COMPONENTS.read_text(encoding=ENCODING))["interpreter"]
    listed: list[dict[str, object]] = [{
        "type": "library",
        "name": interpreter["name"],
        "version": platform.python_version(),
        "licenses": [{"license": {"name": interpreter["license"]}}],
        "externalReferences": [{"type": "website", "url": interpreter["url"]}],
    }]
    for component in components():
        entry: dict[str, object] = {"type": "library", "name": component.name}
        if component.version:
            entry["version"] = component.version
        if component.package:
            entry["purl"] = f"pkg:pypi/{component.package}@{component.version}"
        if component.license:
            entry["licenses"] = [{"license": {"name": component.license}}]
        if component.url:
            entry["externalReferences"] = [{"type": "website", "url": component.url}]
        listed.append(entry)
    return {
        "$schema": _SBOM_SCHEMA,
        "bomFormat": _SBOM_FORMAT,
        "specVersion": _SBOM_VERSION,
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "name": APP_NAME,
                "version": APP_VERSION,
                "licenses": [{"license": {"id": APP_LICENSE}}],
            },
        },
        "components": listed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify the file is up to date")
    parser.add_argument("--sbom", type=Path, help="write a bill of materials to this path")
    arguments = parser.parse_args()
    try:
        if arguments.sbom:
            document = json.dumps(bill_of_materials(), indent=2) + "\n"
            arguments.sbom.write_text(document, encoding=ENCODING, newline="\n")
            print(f"wrote {arguments.sbom}")
            return 0
        if arguments.check:
            current = OUTPUT.read_text(encoding=ENCODING) if OUTPUT.is_file() else ""
            found = differences(current)
            if found:
                print(f"{OUTPUT.relative_to(ROOT)} does not list what is installed:", file=sys.stderr)
                for line in found:
                    print(f"  {line}", file=sys.stderr)
                print("Run: python tools/collect_licenses.py", file=sys.stderr)
                return 1
            print(f"{OUTPUT.relative_to(ROOT)} lists what is installed")
            return 0
        text = collect()
    except (LicenseError, OSError, KeyError, metadata.PackageNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    OUTPUT.write_text(text, encoding=ENCODING, newline="\n")
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({len(text):,} characters)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
