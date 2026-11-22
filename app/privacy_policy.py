# SPDX-License-Identifier: GPL-3.0-or-later
"""The privacy policy: one file, read and shown the same everywhere.

The policy of WireTrace and of its website is ``PRIVACY.md`` at the top
of the repository, and nowhere else. The Windows installer shows it as
plain text before anything is installed (``build/build.py``), the user
guide shows it in its Privacy section (``app/help_loader.py``), and the
website shows the file of the released version. So the policy can never
say one thing in one place and another elsewhere.

It is written in a small part of Markdown, read strictly here so that
every place shows all of it and none of its markup: a title (``#``), the
line ``Last updated <day> <month> <year>``, then sections (``##``), each
of paragraphs and lists (``- `` at the start of each line, one line an
item). Within text, only ``**bold**`` and links ``[text](https://...)``
or ``[address](mailto:address)``. Anything else is refused, as is any
character outside ASCII, which the installer cannot show, and any
control character but the line feed. The website
reads the file by the same rules (``app/lib/privacy`` in its repository).

This module has no Qt dependency.
"""

from __future__ import annotations

import html
import re
import sys
from dataclasses import dataclass
from pathlib import Path

POLICY_FILENAME = "PRIVACY.md"

_TITLE = re.compile(r"# (\S.*)")
_UPDATED = re.compile(r"Last updated \d{1,2} [A-Z][a-z]+ \d{4}")
_SECTION = re.compile(r"## (\S.*)")
_ITEM = "- "
_INLINE = re.compile(
    r"\*\*(?P<bold>[^*\[\]]+)\*\*"
    r"|\[(?P<text>[^\[\]]+)\]\((?P<url>https://[^\s()]+|mailto:[^\s()]+)\)"
)
# Markup this reading does not take: a character that would show as it
# stands in one place and mean something in another.
_UNTAKEN = re.compile(r"[*_`\[\]<>\\]")
# The start of a line in Markdown's other blocks: headings, quotes,
# numbered lists, other list marks, code and rules.
_OTHER_BLOCK = re.compile(r"(#|>|\d+[.)]\s|[*+]\s|```|---|___|\s)")


# Every control character but the line feed: Python and JavaScript would
# not agree on which of them are spaces.
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


class PolicyError(ValueError):
    """The policy file does not keep to the part of Markdown it is read in."""


@dataclass(frozen=True)
class Run:
    """A piece of text: plain, bold, or a link to ``url``."""

    text: str
    bold: bool = False
    url: str | None = None


Text = tuple[Run, ...]


@dataclass(frozen=True)
class Block:
    """A paragraph (one text) or a list (one text an item)."""

    texts: tuple[Text, ...]
    is_list: bool


@dataclass(frozen=True)
class Section:
    label: str
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class Policy:
    title: str
    updated: str
    sections: tuple[Section, ...]


def find_policy() -> Path:
    """The policy file, beside the source tree (running from source),
    beside the executable (a packaged build), in the folder a one-file
    build unpacks itself to, or in the ``Resources`` folder of a macOS
    application bundle.

    Raises:
        PolicyError: it is in none of those places.
    """
    executable_dir = Path(sys.executable).resolve().parent
    roots = [Path(__file__).resolve().parent.parent, executable_dir]
    unpacked = getattr(sys, "_MEIPASS", None)
    if unpacked:
        roots.append(Path(unpacked))
    if sys.platform == "darwin":
        roots.append(executable_dir.parent / "Resources")
    for root in roots:
        candidate = root / POLICY_FILENAME
        if candidate.is_file():
            return candidate
    raise PolicyError(f"{POLICY_FILENAME} was not found beside the program")


def load_policy() -> Policy:
    """The policy, read from its file.

    Raises:
        PolicyError: it cannot be found, read or taken.
    """
    path = find_policy()
    try:
        return parse_policy(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise PolicyError(f"{path} could not be read: {exc}") from exc


def _text(line: str, where: int) -> Text:
    runs: list[Run] = []
    position = 0
    for match in _INLINE.finditer(line):
        runs.extend(_plain(line[position:match.start()], where))
        if match.group("bold") is not None:
            runs.append(Run(match.group("bold"), bold=True))
        else:
            text, url = match.group("text"), match.group("url")
            _refuse_markup(text, where)
            runs.append(Run(text, url=url))
        position = match.end()
    runs.extend(_plain(line[position:], where))
    if not runs:
        raise PolicyError(f"line {where}: empty text")
    return tuple(runs)


def _plain(text: str, where: int) -> list[Run]:
    if not text:
        return []
    _refuse_markup(text, where)
    return [Run(text)]


def _refuse_markup(text: str, where: int) -> None:
    if _UNTAKEN.search(text):
        raise PolicyError(f"line {where}: markup that is not read: {text!r}")


def parse_policy(markdown: str) -> Policy:
    """The policy in ``markdown``.

    Raises:
        PolicyError: it does not keep to the part of Markdown it is read in.
    """
    if not markdown.isascii():
        raise PolicyError("only ASCII characters are taken: the installer cannot show others")
    if _CONTROL.search(markdown):
        raise PolicyError("lines end with a line feed alone, and no other control character is taken")
    # Paragraphs: runs of lines between blank lines, with their first
    # line's number.
    paragraphs: list[tuple[int, list[str]]] = []
    for number, line in enumerate(markdown.split("\n"), start=1):
        if line != line.rstrip():
            raise PolicyError(f"line {number}: ends with a space")
        if not line:
            continue
        if paragraphs and paragraphs[-1][0] + len(paragraphs[-1][1]) == number:
            paragraphs[-1][1].append(line)
        else:
            paragraphs.append((number, [line]))
    if len(paragraphs) < 3:
        raise PolicyError("a title, the day it was last updated, and sections are needed")
    (title_at, title_lines), (updated_at, updated_lines) = paragraphs[0], paragraphs[1]
    title = _TITLE.fullmatch(title_lines[0])
    if len(title_lines) != 1 or title is None:
        raise PolicyError(f"line {title_at}: the first line is the title, after '# '")
    if len(updated_lines) != 1 or not _UPDATED.fullmatch(updated_lines[0]):
        raise PolicyError(f"line {updated_at}: then 'Last updated <day> <month> <year>'")
    sections: list[tuple[str, list[Block]]] = []
    for at, lines in paragraphs[2:]:
        heading = _SECTION.fullmatch(lines[0])
        if heading is not None:
            if len(lines) != 1:
                raise PolicyError(f"line {at}: a section heading is a line of its own")
            sections.append((heading.group(1), []))
            continue
        if not sections:
            raise PolicyError(f"line {at}: text before the first section")
        if lines[0].startswith(_ITEM):
            if not all(line.startswith(_ITEM) for line in lines):
                raise PolicyError(f"line {at}: every line of a list begins with '- '")
            items = tuple(_text(line[len(_ITEM):], at + offset) for offset, line in enumerate(lines))
            sections[-1][1].append(Block(items, is_list=True))
            continue
        for offset, line in enumerate(lines):
            if _OTHER_BLOCK.match(line):
                raise PolicyError(f"line {at + offset}: markup that is not read: {line!r}")
        sections[-1][1].append(Block((_text(" ".join(lines), at),), is_list=False))
    for label, blocks in sections:
        _refuse_markup(label, 0)
        if not blocks:
            raise PolicyError(f"the section {label!r} is empty")
    return Policy(
        title=title.group(1),
        updated=updated_lines[0],
        sections=tuple(Section(label, tuple(blocks)) for label, blocks in sections),
    )


def _plain_run(run: Run) -> str:
    if run.url is None or run.url in (run.text, f"mailto:{run.text}"):
        return run.text
    return f"{run.text} ({run.url})"


def to_plain_text(policy: Policy) -> str:
    """The policy as plain text, one line a paragraph or an item, for the
    installer."""
    lines = [policy.title, "", policy.updated]
    for section in policy.sections:
        lines += ["", section.label]
        for block in section.blocks:
            lines.append("")
            texts = ["".join(_plain_run(run) for run in text) for text in block.texts]
            lines += [f"{_ITEM}{text}" for text in texts] if block.is_list else texts
    return "\n".join(lines) + "\n"


def _html_run(run: Run) -> str:
    text = html.escape(run.text)
    if run.bold:
        return f"<strong>{text}</strong>"
    if run.url is not None:
        return f'<a href="{html.escape(run.url)}">{text}</a>'
    return text


def to_html(policy: Policy) -> str:
    """The policy below its title, as HTML for the user guide: the day it
    was last updated, then each section under a third-level heading."""
    parts = [f"<p>{html.escape(policy.updated)}</p>"]
    for section in policy.sections:
        parts.append(f"<h3>{html.escape(section.label)}</h3>")
        for block in section.blocks:
            texts = ["".join(_html_run(run) for run in text) for text in block.texts]
            if block.is_list:
                parts.append("<ul>" + "".join(f"<li>{text}</li>" for text in texts) + "</ul>")
            else:
                parts.append(f"<p>{texts[0]}</p>")
    return "\n".join(parts)
