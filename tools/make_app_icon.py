# SPDX-License-Identifier: GPL-3.0-or-later
"""Make WireTrace's icon files from its mark, in the accent of the design language.

The mark is drawn once, as a vector: ``resources/app_icon.svg``. Its
colour is the ``accent`` token of the light theme, the colour the
design language gives the brand's marks (the slash of The Nuance
Project); this tool writes it into the SVG. The icon is one colour
whatever the theme: the system shows it on its own backgrounds.

Every size is drawn from the vector itself, never shrunk from a larger
picture, so each is as sharp as its pixels allow. Writes, beside it:

- ``app_icon.png``: 512 pixels; the window icon on Linux, and the
  AppImage and Debian package icon.
- ``app_icon.ico``: 16 to 256 pixels; the program, the installer and
  the window icon on Windows.
- ``app_icon.icns``: 16 to 256 pixels; for macOS.

Each size inside the .ico and .icns files is a PNG image, as Windows
Vista and later and macOS 10.7 and later read them.

Usage:
    python tools/make_app_icon.py           write the files
    python tools/make_app_icon.py --check   fail if a file is not what it would write

On one machine, running it again gives the same files. Another machine
may smooth the mark's edges by a few levels and compress the PNG images
differently, as processors and systems do; so a file is current when
it holds the same pictures, at the same sizes, as the tool would write
(``stale_files``), not when its bytes are the same.
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

import numpy as np
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QImageReader, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.themes.design_tokens import COLOURS  # noqa: E402

RESOURCES = ROOT / "resources"
SVG_PATH = RESOURCES / "app_icon.svg"
PNG_PATH = RESOURCES / "app_icon.png"
ICO_PATH = RESOURCES / "app_icon.ico"
ICNS_PATH = RESOURCES / "app_icon.icns"

#: The side of the PNG image and of the mark's canvas.
PNG_SIZE = 512
#: The sizes in the .ico file.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
#: The sizes in the .icns file, by the type macOS gives each.
ICNS_TYPES = ((b"icp4", 16), (b"icp5", 32), (b"ic07", 128), (b"ic08", 256))

#: The theme and the token the mark is drawn in.
THEME = "light"
TOKEN = "accent"

_ICO_HEADER = struct.Struct("<HHH")
_ICO_ENTRY = struct.Struct("<BBBBHHII")
_ICO_TYPE_ICON = 1
_ICO_PLANES = 1
_ICO_BITS = 32
#: An .ico entry writes 256 as 0: its size fields are one byte each.
_ICO_SIZE_WRAP = 256
_ICNS_HEADER = struct.Struct(">4sI")
#: The fill of the mark's path in the SVG.
_FILL = re.compile(r'fill="#[0-9A-Fa-f]{6}"')

#: How far, in levels of 255 of any channel, a pixel of a picture may
#: differ from the same pixel drawn on another machine and still be the
#: same picture. Measured on the mark: processors smooth its edges
#: differently by a few levels; a mark drawn in another colour differs
#: by 29 levels or more, and a mark moved by one pixel by up to 255.
RENDERING_TOLERANCE = 8
_CHANNELS = 4


def accent() -> QColor:
    """The colour of the mark, from the design tokens."""
    hex_colour, opacity = COLOURS[THEME][TOKEN]
    colour = QColor(hex_colour)
    colour.setAlphaF(opacity)
    return colour


def coloured_svg(source: str, colour: QColor) -> str:
    """The mark's SVG with its fill set to ``colour``."""
    if len(_FILL.findall(source)) != 1:
        raise ValueError(f"{SVG_PATH.name} must hold one filled path")
    return _FILL.sub(f'fill="{colour.name().upper()}"', source)


def drawn(svg: str, size: int) -> QImage:
    """The mark drawn from its vector at ``size`` pixels square."""
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    if not renderer.isValid():
        raise ValueError(f"{SVG_PATH.name} could not be read as SVG")
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image.convertToFormat(QImage.Format.Format_ARGB32)


def png_bytes(image: QImage) -> bytes:
    """``image`` as a PNG file."""
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise OSError("the image could not be written as PNG")
    buffer.close()
    return bytes(data.data())


def ico_bytes(svg: str) -> bytes:
    """An .ico file holding the mark at every size of ``ICO_SIZES``."""
    images = [png_bytes(drawn(svg, size)) for size in ICO_SIZES]
    offset = _ICO_HEADER.size + _ICO_ENTRY.size * len(images)
    header = _ICO_HEADER.pack(0, _ICO_TYPE_ICON, len(images))
    entries = b""
    for size, data in zip(ICO_SIZES, images, strict=True):
        side = size % _ICO_SIZE_WRAP
        entries += _ICO_ENTRY.pack(side, side, 0, 0, _ICO_PLANES, _ICO_BITS, len(data), offset)
        offset += len(data)
    return header + entries + b"".join(images)


def icns_bytes(svg: str) -> bytes:
    """An .icns file holding the mark at every size of ``ICNS_TYPES``."""
    blocks = b"".join(
        _ICNS_HEADER.pack(kind, _ICNS_HEADER.size + len(data)) + data
        for kind, data in ((kind, png_bytes(drawn(svg, size))) for kind, size in ICNS_TYPES)
    )
    return _ICNS_HEADER.pack(b"icns", _ICNS_HEADER.size + len(blocks)) + blocks


def images_in(data: bytes) -> dict[int, QImage]:
    """Every image a PNG, .ico or .icns file holds, by its side in pixels.

    Raises:
        ValueError: the data cannot be read as images, or holds two of
            one size.
    """
    array = QByteArray(data)
    buffer = QBuffer(array)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    images: dict[int, QImage] = {}
    for _ in range(max(reader.imageCount(), 1)):
        image = reader.read()
        if image.isNull():
            raise ValueError(f"an image could not be read: {reader.errorString()}")
        if image.width() in images:
            raise ValueError(f"two images of {image.width()} pixels")
        images[image.width()] = image
        if not reader.jumpToNextImage():
            break
    buffer.close()
    return images


def _premultiplied(image: QImage) -> np.ndarray:
    """The pixels of ``image``, premultiplied, so that a fully transparent
    pixel reads the same whatever colour it was given."""
    image = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    rows = np.frombuffer(image.constBits(), dtype=np.uint8, count=image.sizeInBytes())
    rows = rows.reshape(image.height(), image.bytesPerLine())
    return rows[:, : image.width() * _CHANNELS].astype(np.int16)


def difference(first: QImage, second: QImage) -> int:
    """The most any channel of any pixel differs between two pictures of
    one size, in levels of 255.

    Raises:
        ValueError: the pictures are not the same size.
    """
    if first.size() != second.size():
        raise ValueError("the pictures are not the same size")
    return int(np.abs(_premultiplied(first) - _premultiplied(second)).max())


def same_pictures(found: bytes, expected: bytes) -> bool:
    """Whether two image files hold the same pictures at the same sizes,
    within ``RENDERING_TOLERANCE``."""
    try:
        have, want = images_in(found), images_in(expected)
    except ValueError:
        return False
    return have.keys() == want.keys() and all(
        difference(have[size], want[size]) <= RENDERING_TOLERANCE for size in want
    )


def stale_files(files: dict[Path, bytes]) -> list[str]:
    """The names of the files that are not as ``files`` says they should be.

    The SVG is text and must match exactly; each picture file must hold
    the same pictures, at the same sizes, as the tool draws.
    """
    stale = []
    for path, expected in files.items():
        found = path.read_bytes() if path.is_file() else b""
        current = found == expected if path == SVG_PATH else same_pictures(found, expected)
        if not current:
            stale.append(path.name)
    return stale


def icon_files() -> dict[Path, bytes]:
    """Every icon file, as it should be."""
    svg = coloured_svg(SVG_PATH.read_text(encoding="utf-8"), accent())
    return {
        SVG_PATH: svg.encode("utf-8"),
        PNG_PATH: png_bytes(drawn(svg, PNG_SIZE)),
        ICO_PATH: ico_bytes(svg),
        ICNS_PATH: icns_bytes(svg),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only check the files")
    arguments = parser.parse_args(argv)
    _application = QGuiApplication.instance() or QGuiApplication(["make_app_icon", "-platform", "offscreen"])
    files = icon_files()
    if arguments.check:
        stale = stale_files(files)
        if stale:
            print(f"Not as tools/make_app_icon.py writes them: {', '.join(stale)}", file=sys.stderr)
            return 1
        print("The icon files are up to date.")
        return 0
    for path, data in files.items():
        path.write_bytes(data)
        print(f"Wrote {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
