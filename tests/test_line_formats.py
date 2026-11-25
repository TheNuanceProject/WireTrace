# SPDX-License-Identifier: GPL-3.0-or-later
"""The line format reference states what WireTrace does.

Every example in ``resources/help/line_formats.html`` that a device
could print carries ``data-check`` (what is checked: ``level``, ``plot``,
``values``, ``restart`` or ``csv``) and ``data-expect`` (the result the page states).
Each is fed here through the program's own code, the way a received line
goes: framed, decoded, then given to the level detector, the restart
detector, the plot's parsers or the CSV engine. A change of behaviour
that the page does not follow fails here, not in front of a reader.

The stated result must also be what the reader sees beside the example,
so the attribute and the visible text cannot drift apart.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

import pytest

from app.constants import (
    CSV_AUTODETECT_SAMPLE_SIZE,
    LEVEL_NAMES,
    PLOT_AUTODETECT_SAMPLE_SIZE,
    PLOT_MAX_LINE_CHARACTERS,
    PLOT_MAX_SERIES,
    STRUCTURE_MAX_LINE_CHARACTERS,
)
from app.help_loader import (
    LINE_FORMATS_FILENAME,
    render_web_document,
    resolve_user_guide_source,
)
from core.csv_engine import CSVEngine
from core.framing import LineFramer, decode_line
from core.plot_parsers import DelimitedParser, ParserPipeline
from core.reset_detector import ResetDetector
from core.tag_detector import TagDetector

TEMPLATE = resolve_user_guide_source(LINE_FORMATS_FILENAME).read_text(encoding="utf-8")
DOCUMENT = render_web_document(LINE_FORMATS_FILENAME)

#: What a stated "nothing" reads as beside an example, by check.
_NOTHING_SEEN = {"plot": "not plotted"}
_NOTHING = "none"
_CSV_TIMESTAMP = "12:00:00.000"


# ── Reading the page ────────────────────────────────────────────────────────


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None
    children: list[_Node | str] = field(default_factory=list)

    def text(self) -> str:
        return "".join(child if isinstance(child, str) else child.text() for child in self.children)

    def elements(self) -> list[_Node]:
        return [child for child in self.children if isinstance(child, _Node)]


class _Tree(HTMLParser):
    """The page as a tree of elements, enough to find each example and
    what is written beside it."""

    _VOID = frozenset({"br", "img", "source", "hr"})

    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {}, None)
        self._open = self.root
        self.feed(text)
        self.close()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {name: value or "" for name, value in attrs}, self._open)
        self._open.children.append(node)
        if tag not in self._VOID:
            self._open = node

    def handle_endtag(self, tag: str) -> None:
        node: _Node | None = self._open
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self._open = node.parent

    def handle_data(self, data: str) -> None:
        self._open.children.append(data)


def _walk(node: _Node):
    for child in node.elements():
        yield child
        yield from _walk(child)


@dataclass(frozen=True)
class _Example:
    check: str
    expect: str
    lines: tuple[str, ...]
    beside: str

    def __str__(self) -> str:
        return f"{self.check}: {' / '.join(self.lines)}"


def _beside(node: _Node) -> str:
    """What the reader sees as the example's result: for a block of
    lines, the paragraph after it; for an example inside a sentence or a
    table, that sentence, list item or table row."""
    if node.tag == "pre":
        siblings = node.parent.elements()
        following = siblings[siblings.index(node) + 1:]
        return following[0].text() if following else ""
    ancestor = node.parent
    while ancestor is not None and ancestor.tag not in {"tr", "li", "p"}:
        ancestor = ancestor.parent
    return ancestor.text() if ancestor is not None else ""


def _examples(text: str) -> list[_Example]:
    return [
        _Example(
            check=node.attrs["data-check"],
            expect=node.attrs.get("data-expect", ""),
            lines=tuple(node.text().split("\n")),
            beside=_beside(node),
        )
        for node in _walk(_Tree(text).root)
        if "data-check" in node.attrs
    ]


EXAMPLES = _examples(DOCUMENT)


# ── Running an example through the program ──────────────────────────────────


def _received(lines: tuple[str, ...]) -> list[str]:
    """The lines as WireTrace receives them: sent with a newline each,
    framed and decoded."""
    framer = LineFramer()
    raw = "".join(f"{line}\n" for line in lines).encode("utf-8")
    decoded = [decode_line(part) for part in framer.feed(raw) + framer.flush()]
    return [line for line in decoded if line is not None]


def _cycled(lines: list[str], count: int) -> list[str]:
    """``lines`` repeated in order until there are ``count`` of them,
    as a device that keeps printing them would send them."""
    return list(itertools.islice(itertools.cycle(lines), count))


def _level(lines: list[str]) -> str:
    (line,) = lines
    return LEVEL_NAMES[TagDetector.detect(line)]


def _plot(lines: list[str]) -> str:
    columns = ParserPipeline().detect(_cycled(lines, PLOT_AUTODETECT_SAMPLE_SIZE))
    return _NOTHING if columns is None else ", ".join(columns[:PLOT_MAX_SERIES])


def _values(lines: list[str]) -> str:
    """The values the plot takes from the last line, once it has found
    its columns in lines like these."""
    pipeline = ParserPipeline()
    if pipeline.detect(_cycled(lines, PLOT_AUTODETECT_SAMPLE_SIZE)) is None:
        return _NOTHING
    values = pipeline.extract(lines[-1]) or {}
    return ", ".join(f"{name} = {value:g}" for name, value in values.items()) or _NOTHING


def _restart(lines: list[str]) -> str:
    detector = ResetDetector()
    reasons = [reason for line in lines if (reason := detector.check(line)) is not None]
    return ", ".join(reasons) or _NOTHING


def _csv(lines: list[str]) -> str:
    engine = CSVEngine()
    for line in _cycled(lines, CSV_AUTODETECT_SAMPLE_SIZE):
        engine.feed(_CSV_TIMESTAMP, line)
    engine.finalize()
    return engine.header


_CHECKS = {"level": _level, "plot": _plot, "values": _values, "restart": _restart, "csv": _csv}


# ── The tests ───────────────────────────────────────────────────────────────


def test_the_reference_has_an_example_of_every_kind() -> None:
    assert {example.check for example in EXAMPLES} == set(_CHECKS)


@pytest.mark.parametrize("example", EXAMPLES, ids=str)
def test_every_example_gives_the_result_the_page_states(example: _Example) -> None:
    assert example.check in _CHECKS, f"unknown check {example.check!r}"
    assert example.expect, "an example states its result in data-expect"

    assert _CHECKS[example.check](_received(example.lines)) == example.expect


@pytest.mark.parametrize("example", EXAMPLES, ids=str)
def test_the_stated_result_is_what_the_reader_sees(example: _Example) -> None:
    beside = example.beside.casefold()
    if example.expect == _NOTHING:
        assert _NOTHING_SEEN[example.check] in beside
        return
    for part in example.expect.split(", "):
        assert part.casefold() in beside, f"{part!r} is not written beside {example}"


def test_the_separators_are_tried_in_the_order_the_page_gives() -> None:
    # The page: "comma, tab, semicolon, bar (|), then spaces".
    assert DelimitedParser._DELIMITERS == (",", "\t", ";", "|")
    assert "comma, tab, semicolon, bar (<code>|</code>), then spaces" in DOCUMENT


def test_csv_columns_read_as_much_of_a_line_as_the_plot() -> None:
    # The page: CSV columns are "found as for the plot", and the plot reads
    # the first PLOT_MAX_LINE_CHARACTERS characters of a line.
    assert STRUCTURE_MAX_LINE_CHARACTERS == PLOT_MAX_LINE_CHARACTERS


def test_every_link_inside_the_reference_leads_somewhere() -> None:
    anchors = set(re.findall(r'\sid="([^"]+)"', DOCUMENT))
    targets = set(re.findall(r'href="#([^"]+)"', DOCUMENT))
    assert targets <= anchors, f"no section for {sorted(targets - anchors)}"


def test_the_reference_is_a_web_document_only() -> None:
    """The reference is shown on the website; it has no page of its own,
    so it holds nothing of one."""
    for page_part in ("{{css:", "{{below:", "<html", "<head", "<style", "<script"):
        assert page_part not in TEMPLATE
