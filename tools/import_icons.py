# SPDX-License-Identifier: GPL-3.0-or-later
"""Bring the interface icons in from Lucide, the icon set of the Nuance Design Language.

WireTrace's icons are Lucide's, at the language's stroke width. This tool
copies each one from an unpacked ``lucide-static`` package into
``resources/icons`` under the name the interface uses, and writes
``resources/icons/icons.json``: which Lucide icon and version each file
comes from, and the SHA-256 of the file written. A test checks the files
against that record, so an icon edited by hand is recognised.

Some icons are the marks of controls rather than pictures: the tick of a
chosen check box and the dot of a chosen radio button. A mark that says
"this one" is two pixels wide in the language, so the tick is Lucide's
drawn with that stroke at the indicator's size, and the dot is drawn
here, in ``OWN_ICONS``.

Usage (the package unpacked from ``npm pack lucide-static@<version>``):

    python tools/import_icons.py path/to/package
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.themes.design_tokens import MEASURES  # noqa: E402

ICONS_DIR = ROOT / "resources" / "icons"
MANIFEST = ICONS_DIR / "icons.json"
LICENSE_NAME = "Lucide-LICENSE.txt"

_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_ENCODING = "utf-8"

_GRID = 24


def _grid_units(pixels: int, shown_at: int) -> str:
    """``pixels`` at ``shown_at`` pixels, in units of the 24-unit grid."""
    return f"{pixels * _GRID / shown_at:g}"


#: The stroke of every icon: one pixel, the weight of the language's
#: lines, at its icon size.
STROKE_WIDTH = _grid_units(MEASURES["border.hairline"], MEASURES["size.icon.sm"])

#: The stroke of a control's mark: the language's mark, at the size of a
#: check box's indicator.
MARK_STROKE_WIDTH = _grid_units(MEASURES["border.mark"], MEASURES["size.indicator"])

#: The name the interface uses -> the Lucide icon drawn for it.
LUCIDE_ICONS: dict[str, str] = {
    "add_tab": "plus",
    "chevron_down": "chevron-down",
    "chevron_up": "chevron-up",
    "clear": "eraser",
    "close_tab": "x",
    "export": "download",
    "log_off": "square",
    "log_on": "circle-dot",
    "more": "ellipsis",
    "pause": "pause",
    "plot": "chart-spline",
    "refresh": "refresh-cw",
    "resume": "play",
    "search": "search",
    "tab_close": "x",
}

#: Marks of controls drawn from a Lucide icon: name -> the Lucide icon.
LUCIDE_MARKS: dict[str, str] = {
    "checkmark": "check",
}

#: Marks of controls, drawn on the same 24-unit grid.
OWN_ICONS: dict[str, str] = {
    # The dot inside a chosen radio button: a quarter of the indicator.
    "radio_dot": (
        f'<svg xmlns="{_SVG_NAMESPACE}" width="{_GRID}" height="{_GRID}" '
        f'viewBox="0 0 {_GRID} {_GRID}"><circle cx="12" cy="12" r="6" fill="currentColor"/></svg>\n'
    ),
}


class IconError(RuntimeError):
    """The package is not what this tool expects."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def lucide_drawing(source: Path, lucide_name: str, version: str, stroke: str) -> str:
    """The Lucide icon at ``source``, drawn with ``stroke``."""
    ElementTree.register_namespace("", _SVG_NAMESPACE)
    try:
        svg = ElementTree.fromstring(source.read_text(encoding=_ENCODING))
    except (OSError, ElementTree.ParseError) as exc:
        raise IconError(f"{source.name} could not be read: {exc}") from exc
    expected = {"viewBox": f"0 0 {_GRID} {_GRID}", "stroke": "currentColor", "fill": "none"}
    for attribute, value in expected.items():
        if svg.get(attribute) != value:
            raise IconError(f"{source.name}: {attribute} is {svg.get(attribute)!r}, not {value!r}")
    svg.attrib.pop("class", None)
    svg.set("stroke-width", stroke)
    body = ElementTree.tostring(svg, encoding="unicode")
    return f"<!-- Lucide {version} (ISC), {lucide_name} -->\n{body}\n"


def build(package: Path) -> dict[str, bytes]:
    """Every icon file and the record, by file name."""
    try:
        meta = json.loads((package / "package.json").read_text(encoding=_ENCODING))
    except (OSError, ValueError) as exc:
        raise IconError(f"{package} is not an unpacked lucide-static package: {exc}") from exc
    if meta.get("name") != "lucide-static":
        raise IconError(f"{package} holds {meta.get('name')!r}, not lucide-static")
    version = meta["version"]

    files: dict[str, bytes] = {}
    record: dict[str, dict[str, str]] = {}
    drawn = [(name, lucide, STROKE_WIDTH) for name, lucide in LUCIDE_ICONS.items()]
    drawn += [(name, lucide, MARK_STROKE_WIDTH) for name, lucide in LUCIDE_MARKS.items()]
    for name, lucide_name, stroke in sorted(drawn):
        source = package / "icons" / f"{lucide_name}.svg"
        data = lucide_drawing(source, lucide_name, version, stroke).encode(_ENCODING)
        files[f"{name}.svg"] = data
        record[name] = {
            "source": f"lucide-static {version}: icons/{lucide_name}.svg",
            "source_sha256": _sha256(source.read_bytes()),
            "stroke_width": stroke,
            "sha256": _sha256(data),
        }
    for name, drawing in sorted(OWN_ICONS.items()):
        data = drawing.encode(_ENCODING)
        files[f"{name}.svg"] = data
        record[name] = {"source": "WireTrace", "sha256": _sha256(data)}

    license_text = (package / "LICENSE").read_bytes()
    files[LICENSE_NAME] = license_text
    manifest = {
        "lucide_version": version,
        "stroke_width": STROKE_WIDTH,
        "mark_stroke_width": MARK_STROKE_WIDTH,
        "license": LICENSE_NAME,
        "license_sha256": _sha256(license_text),
        "icons": record,
    }
    files[MANIFEST.name] = (json.dumps(manifest, indent=2) + "\n").encode(_ENCODING)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("package", type=Path, help="the unpacked lucide-static package")
    args = parser.parse_args(argv)
    try:
        files = build(args.package)
    except IconError as exc:
        print(f"import_icons: {exc}", file=sys.stderr)
        return 1
    stale = sorted(
        path.name for path in ICONS_DIR.glob("*.svg") if path.name not in files
    )
    for name in stale:
        (ICONS_DIR / name).unlink()
        print(f"removed: {name}")
    for name, data in files.items():
        (ICONS_DIR / name).write_bytes(data)
        print(f"written: {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
