# SPDX-License-Identifier: GPL-3.0-or-later
"""The user guide and the line format reference: filled in and made ready.

Both are templates in ``resources/help``: ``user_guide.html``, the guide
Help opens, and ``line_formats.html``, the reference of the lines
WireTrace reads, for people writing firmware. Wherever a template states
something the program itself defines (a shortcut, a limit, a colour, a
folder, a picture of a screen), it holds a placeholder instead of the
value. The placeholders are filled here from the same constants,
shortcut table and design tokens the program uses, so a document cannot
say something the program does not do.

Each template is filled in one of two editions:

  app  The guide as Help opens it: a whole page, in WireTrace's own
       type and colours, with the shortcuts and folders of the
       computer it runs on. It is written to the user's cache folder,
       never into the installation, which may be read-only. Pictures
       and fonts are embedded, so it is one file that can be opened,
       saved or sent on its own.
  web  What the website shows: the document alone, between the
       ``<!-- document -->`` and ``<!-- /document -->`` marks of its
       template, as an ``<article>`` naming the document and the
       version it describes, with the shortcuts and folders of every
       system. The website lays it out in its own design.

This module has no Qt dependency.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import ntpath
import os
import posixpath
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import fields
from enum import Enum
from pathlib import Path

import version
from app import constants
from app.constants import (
    BRACKETED_LEVEL_TAGS,
    ESP_IDF_LEVEL_TAGS,
    LEVEL_NAMES,
    MS_PER_SECOND,
    SEVERITY_KEYWORDS,
    ZEPHYR_LEVEL_TAGS,
    FolderSystem,
    ThemeID,
    get_cache_dir,
    get_config_dir,
    get_default_log_dir,
    get_diagnostic_log_dir,
    get_settings_copy_dir,
)
from app.privacy_policy import PolicyError, load_policy, to_html
from core import records
from ui import shortcuts
from ui.formatting import format_size
from ui.themes import design_tokens, tokens

logger = logging.getLogger(__name__)

USER_GUIDE_FILENAME = "user_guide.html"
LINE_FORMATS_FILENAME = "line_formats.html"
IMAGES_DIRECTORY = "images"
#: Beside ``help`` in ``resources``.
FONTS_DIRECTORY = "fonts"

_RESOURCE_PATH = ("resources", "help")
_CACHE_SUBDIRECTORY = "help"
_CACHE_NAME_FORMAT = "user_guide-{version}-{digest}.html"
_CACHE_NAME_PATTERN = "user_guide-*.html"
_DIGEST_LENGTH = 16
_PARTIAL_SUFFIX = ".tmp"
_ENCODING = "utf-8"
_CHANNEL_MAX = 255

#: ``{{kind}}`` or ``{{kind:argument}}``.
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)(?::([^{}]*))?\}\}")

#: Where the document of a template begins and ends, for the web edition.
_DOCUMENT_START = "<!-- document -->"
_DOCUMENT_END = "<!-- /document -->"

#: The name each template's web edition gives itself, and the file it is
#: written to (see ``tools/make_web_documents.py``).
WEB_DOCUMENTS: Mapping[str, str] = {
    USER_GUIDE_FILENAME: "user-guide",
    LINE_FORMATS_FILENAME: "line-formats",
}

#: How the modifier keys of Qt's portable form are named on macOS.
_MAC_KEY_NAMES = {"Ctrl": "Cmd", "Alt": "Option"}
_KEY_SEPARATOR = "+"

_IMAGE_SUFFIXES = {ThemeID.LIGHT: "-light.png", ThemeID.DARK: "-dark.png"}
_DARK_MEDIA = "(prefers-color-scheme: dark)"


class Edition(Enum):
    """Who a filled template is for."""

    APP = "app"
    WEB = "web"


#: The systems the web edition states folders for, by the name a reader
#: knows them by, each with its folders written as a reader writes them.
_WEB_SYSTEMS: tuple[tuple[str, FolderSystem], ...] = (
    ("Windows", FolderSystem(
        platform="win32",
        join=ntpath.join,
        home="%USERPROFILE%",
        environ={"APPDATA": "%APPDATA%", "LOCALAPPDATA": "%LOCALAPPDATA%"},
    )),
    ("macOS", FolderSystem(platform="darwin", join=posixpath.join, home="~", environ={})),
    ("Linux", FolderSystem(platform="linux", join=posixpath.join, home="~", environ={})),
)


def _mac_key_sentence() -> str:
    """"On macOS, Ctrl is Cmd and Alt is Option.", from the key names."""
    pairs = [f"{portable} is {mac}" for portable, mac in _MAC_KEY_NAMES.items()]
    joined = pairs[0] if len(pairs) == 1 else ", ".join(pairs[:-1]) + " and " + pairs[-1]
    return f"On macOS, {joined}."


#: Sentences that are true of one edition only, side by side.
_EDITION_TEXT: Mapping[str, Mapping[Edition, Callable[[], str]]] = {
    "guide_lead": {
        Edition.APP: lambda: (
            f"For version {html.escape(version.APP_VERSION)}. Press "
            f"{_keys_html(shortcuts.SHORTCUTS['user_guide'].keys, sys.platform)} in "
            f"{html.escape(version.APP_NAME)} to open this page at any time."
        ),
        Edition.WEB: lambda: (
            f"For version {html.escape(version.APP_VERSION)}. Inside "
            f"{html.escape(version.APP_NAME)}, press "
            f"{_keys_html(shortcuts.SHORTCUTS['user_guide'].keys, None)} to open this guide."
        ),
    },
    "shortcuts_intro": {
        Edition.APP: lambda: "These are the shortcuts on this computer.",
        Edition.WEB: _mac_key_sentence,
    },
    "folders_intro": {
        Edition.APP: lambda: "On this computer:",
        Edition.WEB: lambda: "On each system:",
    },
    "folders_note": {
        Edition.APP: lambda: "",
        Edition.WEB: lambda: (
            "<p>On Linux, the working files and the diagnostic logs follow "
            "<code>XDG_CACHE_HOME</code> and <code>XDG_STATE_HOME</code> when they are set.</p>"
        ),
    },
}


class UserGuideNotFoundError(FileNotFoundError):
    """The guide is missing from the installation."""


class UserGuideError(ValueError):
    """The guide holds a placeholder that cannot be filled."""


# ── Finding the guide ────────────────────────────────────────────────────────


def resolve_user_guide_source(filename: str = USER_GUIDE_FILENAME) -> Path:
    """Find a template of ``resources/help`` on disk, the guide's by default.

    Looked for, in this order: beside the source tree (running from
    source), beside the executable (a packaged build), in the folder a
    one-file build unpacks itself to, and in the ``Resources`` folder of
    a macOS application bundle.

    Raises:
        UserGuideNotFoundError: it is in none of those places.
    """
    executable_dir = Path(sys.executable).resolve().parent
    roots = [Path(__file__).resolve().parent.parent, executable_dir]
    unpacked = getattr(sys, "_MEIPASS", None)
    if unpacked:
        roots.append(Path(unpacked))
    if sys.platform == "darwin":
        roots.append(executable_dir.parent / "Resources")

    candidates = [root.joinpath(*_RESOURCE_PATH, filename) for root in roots]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise UserGuideNotFoundError(
        f"{filename} was not found. Looked in:\n  - "
        + "\n  - ".join(str(path) for path in candidates),
    )


# ── Filling the placeholders ─────────────────────────────────────────────────


def _lookup(name: str) -> object:
    """A constant by name. ``NAME.KEY`` reads one entry of a mapping."""
    name, _, key = name.partition(".")
    for module in (constants, version, records):
        if hasattr(module, name):
            value = getattr(module, name)
            break
    else:
        raise KeyError(name)
    return value[key] if key else value


def _plain(value: object) -> str:
    if isinstance(value, (tuple, list)):
        return ", ".join(_plain(item) for item in value)
    if isinstance(value, float):
        return f"{value:g}"
    return html.escape(str(value))


def _fill_const(name: str) -> str:
    """A constant as it is: ``115200``, ``JetBrains Mono``, ``fatal, panic``."""
    return _plain(_lookup(name))


def _fill_count(name: str) -> str:
    """A whole number with thousands separators: ``100,000``."""
    value = _lookup(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(name)
    return f"{value:,}"


def _fill_size(name: str) -> str:
    """A number of bytes as a size: ``1 GB``."""
    return format_size(int(_lookup(name)))


def _fill_percent(name: str) -> str:
    """A share as a percentage: ``0.7`` as ``70 %``."""
    value = _lookup(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError(name)
    return f"{value * 100:g} %"


def _level_groups(markers: Mapping[str, str]) -> str:
    """Level markers grouped by the level they give, in the order of the
    table: ``<code>D</code>, <code>V</code> Debug``, groups apart by ``;``."""
    groups: dict[str, list[str]] = {}
    for marker, tag in markers.items():
        groups.setdefault(tag, []).append(marker)
    return "; ".join(
        ", ".join(f"<code>{html.escape(marker)}</code>" for marker in group)
        + f" {html.escape(LEVEL_NAMES[tag])}"
        for tag, group in groups.items()
    )


#: The marker tables of the level detector (``core/tag_detector.py``).
_LEVEL_MARKERS: Mapping[str, Mapping[str, str]] = {
    "esp_idf": ESP_IDF_LEVEL_TAGS,
    "zephyr": ZEPHYR_LEVEL_TAGS,
    "bracketed": BRACKETED_LEVEL_TAGS,
}


def _fill_levels(name: str) -> str:
    """The markers of one logging framework, by the level each gives."""
    return _level_groups(_LEVEL_MARKERS[name])


def _fill_keywords(_: str) -> str:
    """The words that give a level, as the rows of a table, most severe first."""
    return "\n".join(
        f"<tr><td>{html.escape(LEVEL_NAMES[tag])}</td>"
        f"<td>{html.escape(', '.join(words))}</td></tr>"
        for tag, words in SEVERITY_KEYWORDS.items()
    )


def _fill_seconds(name: str) -> str:
    """A number of milliseconds as seconds: ``30``."""
    return f"{int(_lookup(name)) / MS_PER_SECOND:g}"


def key_names(keys: str, platform: str | None = sys.platform) -> list[str]:
    """The keys of a combination in Qt's portable form, named for a platform.

    ``None`` keeps the portable names (Ctrl, Alt), as the web edition
    writes them for every system.
    """
    if keys.endswith(_KEY_SEPARATOR * 2):
        parts = [*keys[:-2].split(_KEY_SEPARATOR), _KEY_SEPARATOR]
    else:
        parts = keys.split(_KEY_SEPARATOR)
    if platform == "darwin":
        parts = [_MAC_KEY_NAMES.get(part, part) for part in parts]
    return parts


def _keys_html(keys: str, platform: str | None) -> str:
    return _KEY_SEPARATOR.join(
        f"<kbd>{html.escape(part)}</kbd>" for part in key_names(keys, platform)
    )


def _key_platform(edition: Edition) -> str | None:
    """The app edition names keys as this computer does; the web edition
    keeps the portable names."""
    return sys.platform if edition is Edition.APP else None


def _fill_key(edition: Edition, name: str) -> str:
    """The keys of one action of the shortcut table."""
    return _keys_html(shortcuts.SHORTCUTS[name].keys, _key_platform(edition))


def _fill_shortcuts(edition: Edition, _: str) -> str:
    """Every shortcut, as the rows of a table, one group after another."""
    platform = _key_platform(edition)
    rows: list[str] = []
    group = ""
    for name, shortcut in shortcuts.SHORTCUTS.items():
        if shortcut.group != group:
            group = shortcut.group
            rows.append(f'<tr><th colspan="2" scope="colgroup">{html.escape(group)}</th></tr>')
        alternates = "".join(
            f" or {_keys_html(keys, platform)}"
            for keys in shortcuts.ALTERNATE_KEYS.get(name, ())
        )
        rows.append(
            f"<tr><td>{_keys_html(shortcut.keys, platform)}{alternates}</td>"
            f"<td>{html.escape(shortcut.description)}</td></tr>"
        )
    return "\n".join(rows)


#: Each is called through its name here, so that a test can replace it.
#: ``None`` is the computer the program runs on.
_DIRECTORIES: Mapping[str, Callable[[FolderSystem | None], str]] = {
    "config": lambda system: get_config_dir(system),
    "logs": lambda system: get_default_log_dir(system),
    "cache": lambda system: get_cache_dir(system),
    "settings_copies": lambda system: get_settings_copy_dir(get_config_dir(system), system),
    "diagnostic_logs": lambda system: get_diagnostic_log_dir(system),
}


def _fill_dir(edition: Edition, name: str) -> str:
    """A folder WireTrace uses: on this computer (app), or on each system (web)."""
    folder = _DIRECTORIES[name]
    if edition is Edition.APP:
        return f"<code>{html.escape(folder(None))}</code>"
    items = "".join(
        f"<li>{html.escape(label)}: <code>{html.escape(folder(system))}</code></li>"
        for label, system in _WEB_SYSTEMS
    )
    return f'<ul class="paths">{items}</ul>'


def _fill_edition(edition: Edition, name: str) -> str:
    """A sentence that is true of one edition only (``_EDITION_TEXT``)."""
    return _EDITION_TEXT[name][edition]()


#: The documents on the website, by the name a placeholder gives them.
_WEB_DOCUMENT_URLS: Mapping[str, str] = {
    "guide": version.APP_GUIDE_URL,
    "formats": version.APP_FORMATS_URL,
}


def _fill_see(argument: str) -> str:
    """A link to a section of one of the documents on the website:
    ``{{see:formats#plot-values|the line format reference}}``."""
    target, separator, text = argument.partition("|")
    document, _, section = target.partition("#")
    if not separator or not text.strip():
        raise UserGuideError(f"the link to {target!r} has no text")
    url = _WEB_DOCUMENT_URLS[document] + (f"#{section}" if section else "")
    return f'<a href="{html.escape(url)}">{html.escape(text.strip())}</a>'


def _web_colour(value: str | tokens.Tint) -> str:
    """A colour as a web page writes it: #RRGGBB, or #RRGGBBAA with an opacity."""
    if isinstance(value, tokens.Tint):
        return f"{value.colour}{round(value.opacity * _CHANNEL_MAX):02X}"
    return value


def _css_name(token: str) -> str:
    return token.replace(".", "-").replace("_", "-")


def _css_colours(theme: ThemeID) -> str:
    """Every colour role of ``theme`` as a CSS custom property."""
    palette = tokens.PALETTES[theme]
    lines = []
    for item in fields(palette):
        value = getattr(palette, item.name)
        if isinstance(value, (str, tokens.Tint)):
            lines.append(f"--color-{_css_name(item.name)}: {_web_colour(value)};")
    return " ".join(lines)


def _css_family(families: tuple[str, ...], generic: str) -> str:
    return ", ".join(f'"{family}"' for family in families) + f", {generic}"


def _css_measures() -> str:
    """Every size, the font families and the type scale, as CSS custom properties."""
    typography = tokens.TYPOGRAPHY
    lines = [f"--{_css_name(token)}: {value}px;" for token, value in design_tokens.MEASURES.items()]
    lines.append(f"--font-sans: {_css_family(typography.interface_families, 'sans-serif')};")
    lines.append(f"--font-mono: {_css_family(typography.console_families, 'monospace')};")
    for name, style in tokens.TYPE_STYLES.items():
        lines.append(f"--text-{name}: {style.size}px;")
        lines.append(f"--leading-{name}: {style.line_height}px;")
        lines.append(f"--tracking-{name}: {style.letter_spacing:g}px;")
        lines.append(f"--weight-{name}: {style.weight};")
    return " ".join(lines)


#: The names OpenType gives the weights the language uses.
_WEIGHT_NAMES = {400: "Regular", 500: "Medium"}


def _css_fonts(fonts: Path) -> str:
    """The bundled fonts, embedded, so the guide is drawn in the program's
    own type on any computer and in any browser."""
    typography = tokens.TYPOGRAPHY
    rules = []
    for family in (typography.interface_family, typography.console_family):
        for weight in (typography.regular, typography.medium):
            path = fonts / f"{family.replace(' ', '')}-{_WEIGHT_NAMES[weight]}.ttf"
            try:
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            except OSError as exc:
                raise UserGuideError(f"a font of the user guide is missing: {path.name}") from exc
            rules.append(
                f'@font-face {{ font-family: "{family}"; font-weight: {weight}; '
                f"font-style: normal; font-display: block; "
                f'src: url("data:font/ttf;base64,{encoded}") format("truetype"); }}'
            )
    return "\n".join(rules)


#: A width below a breakpoint ends this much before it, as CSS frameworks
#: write it, so that a fractional width at the breakpoint matches one rule.
_BELOW_BREAKPOINT = 0.02


def _fill_below(breakpoint: str) -> str:
    """A media query for widths below one of the language's breakpoints."""
    width = design_tokens.MEASURES[f"web.breakpoint.{breakpoint}"] - _BELOW_BREAKPOINT
    return f"(max-width: {width:g}px)"


def _fill_css(fonts: Path, name: str) -> str:
    """Design tokens as CSS: ``light`` and ``dark`` colours, ``measures``, ``fonts``."""
    if name == "fonts":
        return _css_fonts(fonts)
    if name == "measures":
        return _css_measures()
    return _css_colours(ThemeID(name))


def _data_uri(path: Path) -> str:
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise UserGuideError(f"a picture of the user guide is missing: {path.name}") from exc
    return f"data:image/png;base64,{encoded}"


def _picture(images: Path, argument: str) -> str:
    """A picture of a screen, in the light or dark theme to match the page."""
    name, separator, description = argument.partition("|")
    if not separator or not description.strip():
        raise UserGuideError(f"the picture {name!r} has no description")
    light = _data_uri(images / (name + _IMAGE_SUFFIXES[ThemeID.LIGHT]))
    dark = _data_uri(images / (name + _IMAGE_SUFFIXES[ThemeID.DARK]))
    return (
        f'<picture><source srcset="{dark}" media="{_DARK_MEDIA}">'
        f'<img src="{light}" alt="{html.escape(description.strip())}"></picture>'
    )


def _fill_privacy(edition: Edition, _: str) -> str:
    """The privacy policy, from its one file (``app/privacy_policy.py``).

    The web edition links to the website's copy of the same policy
    instead of repeating it beside it.
    """
    if edition is Edition.WEB:
        url = html.escape(version.APP_PRIVACY_URL)
        return (
            f"<p>What {html.escape(version.APP_NAME)} sends and keeps is set out in the "
            f'<a href="{url}">privacy policy</a>.</p>'
        )
    try:
        return to_html(load_policy())
    except PolicyError as exc:
        raise UserGuideError(f"the privacy policy cannot be shown: {exc}") from exc


def render_user_guide(
    template: str, images: Path, fonts: Path, edition: Edition = Edition.APP,
) -> str:
    """Fill every placeholder of a template.

    Args:
        template: The text of a template of ``resources/help``.
        images: The folder that holds the pictures the template shows.
        fonts: The folder that holds the fonts the app edition is drawn in.
        edition: Who the filled text is for (see the module's docstring).
            The web edition fills only the document between the marks.

    Raises:
        UserGuideError: a placeholder is unknown or cannot be filled, or
            the web edition's marks are missing.
    """
    fillers: dict[str, Callable[[str], str]] = {
        "const": _fill_const,
        "count": _fill_count,
        "size": _fill_size,
        "seconds": _fill_seconds,
        "percent": _fill_percent,
        "levels": _fill_levels,
        "keywords": _fill_keywords,
        "key": lambda name: _fill_key(edition, name),
        "shortcuts": lambda argument: _fill_shortcuts(edition, argument),
        "dir": lambda name: _fill_dir(edition, name),
        "edition": lambda name: _fill_edition(edition, name),
        "see": _fill_see,
        "css": lambda name: _fill_css(fonts, name),
        "below": _fill_below,
        "image": lambda argument: _picture(images, argument),
        "privacy": lambda argument: _fill_privacy(edition, argument),
    }

    def fill(match: re.Match[str]) -> str:
        kind, argument = match.group(1), match.group(2) or ""
        try:
            return fillers[kind](argument)
        except UserGuideError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise UserGuideError(
                f"the user guide has a placeholder that cannot be filled: {match.group(0)}"
            ) from exc

    text = _document(template) if edition is Edition.WEB else template
    filled = _PLACEHOLDER.sub(fill, text)
    if "{{" in filled or "}}" in filled:
        raise UserGuideError("the user guide has an unfinished placeholder")
    return filled


def _document(template: str) -> str:
    """The part of a template between its document marks."""
    if template.count(_DOCUMENT_START) != 1 or template.count(_DOCUMENT_END) != 1:
        raise UserGuideError("a template marks its document once, with both marks")
    start = template.index(_DOCUMENT_START) + len(_DOCUMENT_START)
    end = template.index(_DOCUMENT_END)
    if end < start:
        raise UserGuideError("a template's document ends before it begins")
    return template[start:end]


def load_user_guide() -> str:
    """The guide with every placeholder filled, as text.

    Raises:
        UserGuideNotFoundError: the guide is missing from the installation.
        UserGuideError: the guide or one of its pictures is damaged.
    """
    source = resolve_user_guide_source()
    return render_user_guide(
        source.read_text(encoding=_ENCODING),
        source.parent / IMAGES_DIRECTORY,
        source.parent.parent / FONTS_DIRECTORY,
    )


def render_web_document(filename: str) -> str:
    """The web edition of a template of ``resources/help``.

    An ``<article>`` that names the document (``data-document``) and the
    version it describes (``data-version``), holding the filled document.

    Raises:
        KeyError: ``filename`` is not one of ``WEB_DOCUMENTS``.
        UserGuideNotFoundError: the template is missing.
        UserGuideError: the template or one of its pictures is damaged.
    """
    name = WEB_DOCUMENTS[filename]
    source = resolve_user_guide_source(filename)
    body = render_user_guide(
        source.read_text(encoding=_ENCODING),
        source.parent / IMAGES_DIRECTORY,
        source.parent.parent / FONTS_DIRECTORY,
        Edition.WEB,
    )
    return (
        f'<article data-document="{html.escape(name)}" '
        f'data-version="{html.escape(version.APP_VERSION)}">\n{body.strip()}\n</article>\n'
    )


# ── Making it ready for the browser ──────────────────────────────────────────


def _holds(path: Path, content: bytes) -> bool:
    """Whether ``path`` is a file with exactly ``content``. A page left
    in the cache is opened only if it is the page that would be written."""
    try:
        return path.is_file() and path.read_bytes() == content
    except OSError:
        return False


def prepare_user_guide() -> Path:
    """Write the filled guide to the cache folder and return its path.

    The file name carries the version and a digest of the page, so a
    page that is already there is reused, and a new version or a changed
    setting produces a new file. Pages left by earlier versions are
    removed.

    Raises:
        UserGuideNotFoundError: the guide is missing from the installation.
        UserGuideError: the guide or one of its pictures is damaged.
        OSError: the page could not be written.
    """
    page = load_user_guide().encode(_ENCODING)
    digest = hashlib.sha256(page).hexdigest()[:_DIGEST_LENGTH]
    directory = Path(get_cache_dir()) / _CACHE_SUBDIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / _CACHE_NAME_FORMAT.format(version=version.APP_VERSION, digest=digest)

    if not _holds(target, page):
        # Written beside its final name and moved into place, so a
        # half-written page is never opened.
        partial = target.with_suffix(target.suffix + _PARTIAL_SUFFIX)
        partial.write_bytes(page)
        os.replace(partial, target)

    for stale in directory.glob(_CACHE_NAME_PATTERN):
        if stale != target:
            try:
                stale.unlink()
            except OSError:
                logger.debug("An earlier copy of the user guide was left in place: %s", stale)
    return target
