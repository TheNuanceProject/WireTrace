# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace's icon: its mark, in the accent of the design language."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter

from tools import make_app_icon

#: Alpha of a pixel that is wholly the mark, not its smoothed edge.
_OPAQUE = 255
#: PNG quality 100: stored with the least compression, so its bytes
#: differ from the committed file's while its pixels are what was drawn.
_LEAST_COMPRESSION = 100
#: A colour that is not the accent.
_ANOTHER_COLOUR = QColor("#3A6FC0")
_SMALLEST = min(make_app_icon.ICO_SIZES)


def _opaque_colours(image: QImage) -> set[str]:
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    return {
        image.pixelColor(x, y).name()
        for y in range(image.height())
        for x in range(image.width())
        if image.pixelColor(x, y).alpha() == _OPAQUE
    }


def test_the_icon_files_are_made_from_the_tokens(qapp):
    assert make_app_icon.stale_files(make_app_icon.icon_files()) == []


def test_a_picture_drawn_on_another_machine_is_the_same_picture(qapp):
    """Edges smoothed a few levels differently, as another processor
    smooths them, and compressed differently: still the same picture."""
    image = QImage(str(make_app_icon.PNG_PATH)).convertToFormat(QImage.Format.Format_ARGB32)
    smoothed = image.copy()
    edges = [
        (x, y)
        for y in range(image.height())
        for x in range(image.width())
        if 0 < image.pixelColor(x, y).alpha() < _OPAQUE
    ]
    for x, y in edges:
        colour = smoothed.pixelColor(x, y)
        colour.setAlpha(colour.alpha() + (1 if colour.alpha() < _OPAQUE - 1 else -1))
        smoothed.setPixelColor(x, y, colour)
    unpacked = QByteArray()
    buffer = QBuffer(unpacked)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert smoothed.save(buffer, "PNG", _LEAST_COMPRESSION)
    buffer.close()

    assert edges
    assert bytes(unpacked.data()) != make_app_icon.PNG_PATH.read_bytes()
    assert make_app_icon.same_pictures(bytes(unpacked.data()), make_app_icon.PNG_PATH.read_bytes())


@pytest.mark.parametrize("change", ["colour", "shape", "size"])
def test_a_changed_mark_is_a_stale_file(qapp, change):
    svg = make_app_icon.coloured_svg(
        make_app_icon.SVG_PATH.read_text(encoding="utf-8"), make_app_icon.accent(),
    )
    expected = make_app_icon.png_bytes(make_app_icon.drawn(svg, make_app_icon.PNG_SIZE))
    if change == "colour":
        other = make_app_icon.coloured_svg(svg, _ANOTHER_COLOUR)
        found = make_app_icon.drawn(other, make_app_icon.PNG_SIZE)
    elif change == "shape":
        drawn = make_app_icon.drawn(svg, make_app_icon.PNG_SIZE)
        found = QImage(drawn.size(), drawn.format())
        found.fill(Qt.GlobalColor.transparent)
        painter = QPainter(found)
        painter.drawImage(1, 0, drawn)
        painter.end()
    else:
        found = make_app_icon.drawn(svg, make_app_icon.PNG_SIZE // 2)

    assert not make_app_icon.same_pictures(make_app_icon.png_bytes(found), expected)


def test_a_missing_or_unreadable_file_is_stale(qapp, tmp_path):
    missing, broken = tmp_path / "missing.png", tmp_path / "broken.png"
    broken.write_bytes(b"not a picture")
    expected = make_app_icon.png_bytes(make_app_icon.drawn(
        make_app_icon.coloured_svg(
            make_app_icon.SVG_PATH.read_text(encoding="utf-8"), make_app_icon.accent(),
        ),
        _SMALLEST,
    ))

    assert make_app_icon.stale_files({missing: expected, broken: expected}) == [
        "missing.png", "broken.png",
    ]


def test_the_mark_is_the_accent(qapp):
    fill = f'fill="{make_app_icon.accent().name().upper()}"'
    assert make_app_icon.SVG_PATH.read_text(encoding="utf-8").count(fill) == 1
    assert _opaque_colours(QImage(str(make_app_icon.PNG_PATH))) == {make_app_icon.accent().name()}


@pytest.mark.parametrize(
    ("make", "sizes"),
    [
        (make_app_icon.ico_bytes, make_app_icon.ICO_SIZES),
        (make_app_icon.icns_bytes, tuple(size for _, size in make_app_icon.ICNS_TYPES)),
    ],
    ids=["ico", "icns"],
)
def test_every_size_is_drawn_from_the_vector_not_shrunk(qapp, make, sizes):
    """Each size is the mark drawn at that size, compared exactly: on one
    machine, drawing is the same every time, and a picture shrunk from a
    larger one differs from it by a few levels."""
    svg = make_app_icon.coloured_svg(
        make_app_icon.SVG_PATH.read_text(encoding="utf-8"), make_app_icon.accent(),
    )
    images = make_app_icon.images_in(make(svg))

    assert sorted(images) == sorted(sizes)
    for size, image in images.items():
        assert make_app_icon.difference(image, make_app_icon.drawn(svg, size)) == 0, size


@pytest.mark.parametrize(
    ("path", "sizes"),
    [
        (make_app_icon.ICO_PATH, make_app_icon.ICO_SIZES),
        (make_app_icon.ICNS_PATH, tuple(size for _, size in make_app_icon.ICNS_TYPES)),
    ],
    ids=["ico", "icns"],
)
def test_every_size_is_there_and_in_the_accent(qapp, path, sizes):
    reader = QImageReader(str(path))
    found = []
    for _ in range(reader.imageCount()):
        image = reader.read()
        assert not image.isNull(), reader.errorString()
        found.append(image.width())
        assert _opaque_colours(image) <= {make_app_icon.accent().name()}
        reader.jumpToNextImage()
    assert sorted(found) == sorted(sizes)
