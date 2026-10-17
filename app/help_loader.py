# SPDX-License-Identifier: GPL-3.0-or-later
"""The user guide: found, filled in and made ready for the browser.

The guide is one HTML page, ``resources/help/user_guide.html``. It is a
template: wherever it states something the program itself defines (a
shortcut, a limit, a colour, a folder, a picture of a screen), it holds
a placeholder instead of the value. The placeholders are filled here
from the same constants, shortcut table and design tokens the program
uses, so the guide cannot say something the program does not do.

The filled page is written to the user's cache folder, never into the
installation, which may be read-only, and its path is returned for the
caller to open. Pictures are embedded in the page, so it is one file
that can be opened, saved or sent on its own.

This module has no Qt dependency.
"""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import fields
from pathlib import Path

import version
from app import constants
from app.constants import ThemeID, get_cache_dir, get_config_dir, get_default_log_dir
from core import records
from ui import shortcuts
from ui.formatting import format_size
from ui.themes import design_tokens, tokens

logger = logging.getLogger(__name__)

USER_GUIDE_FILENAME = "user_guide.html"
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
_MS_PER_SECOND = 1000
_CHANNEL_MAX = 255

#: ``{{kind}}`` or ``{{kind:argument}}``.
_PLACEHOLDER = re.compile(r"\{\{([a-z_]+)(?::([^{}]*))?\}\}")

#: How the modifier keys of Qt's portable form are named on macOS.
_MAC_KEY_NAMES = {"Ctrl": "Cmd", "Alt": "Option"}
_KEY_SEPARATOR = "+"

_IMAGE_SUFFIXES = {ThemeID.LIGHT: "-light.png", ThemeID.DARK: "-dark.png"}
_DARK_MEDIA = "(prefers-color-scheme: dark)"


class UserGuideNotFoundError(FileNotFoundError):
    """The guide is missing from the installation."""


class UserGuideError(ValueError):
    """The guide holds a placeholder that cannot be filled."""


# ── Finding the guide ────────────────────────────────────────────────────────


def resolve_user_guide_source() -> Path:
    """Find the guide's template on disk.

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

    candidates = [root.joinpath(*_RESOURCE_PATH, USER_GUIDE_FILENAME) for root in roots]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise UserGuideNotFoundError(
        "The user guide was not found. Looked in:\n  - "
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


def _fill_seconds(name: str) -> str:
    """A number of milliseconds as seconds: ``30``."""
    return f"{int(_lookup(name)) / _MS_PER_SECOND:g}"


def key_names(keys: str, platform: str = sys.platform) -> list[str]:
    """The keys of a combination in Qt's portable form, named for a platform."""
    if keys.endswith(_KEY_SEPARATOR * 2):
        parts = [*keys[:-2].split(_KEY_SEPARATOR), _KEY_SEPARATOR]
    else:
        parts = keys.split(_KEY_SEPARATOR)
    if platform == "darwin":
        parts = [_MAC_KEY_NAMES.get(part, part) for part in parts]
    return parts


def _keys_html(keys: str) -> str:
    return _KEY_SEPARATOR.join(f"<kbd>{html.escape(part)}</kbd>" for part in key_names(keys))


def _fill_key(name: str) -> str:
    """The keys of one action of the shortcut table."""
    return _keys_html(shortcuts.SHORTCUTS[name].keys)


def _fill_shortcuts(_: str) -> str:
    """Every shortcut, as the rows of a table, one group after another."""
    rows: list[str] = []
    group = ""
    for name, shortcut in shortcuts.SHORTCUTS.items():
        if shortcut.group != group:
            group = shortcut.group
            rows.append(f'<tr><th colspan="2" scope="colgroup">{html.escape(group)}</th></tr>')
        alternates = "".join(
            f" or {_keys_html(keys)}" for keys in shortcuts.ALTERNATE_KEYS.get(name, ())
        )
        rows.append(
            f"<tr><td>{_keys_html(shortcut.keys)}{alternates}</td>"
            f"<td>{html.escape(shortcut.description)}</td></tr>"
        )
    return "\n".join(rows)


#: Each is called through its name here, so that a test can replace it.
_DIRECTORIES: Mapping[str, Callable[[], str]] = {
    "config": lambda: get_config_dir(),
    "logs": lambda: get_default_log_dir(),
    "cache": lambda: get_cache_dir(),
}


def _fill_dir(name: str) -> str:
    """A folder WireTrace uses, as it is on this computer."""
    return html.escape(_DIRECTORIES[name]())


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


def render_user_guide(template: str, images: Path, fonts: Path) -> str:
    """Fill every placeholder of the guide's template.

    Args:
        template: The text of ``user_guide.html``.
        images: The folder that holds the pictures the guide shows.
        fonts: The folder that holds the fonts the guide is drawn in.

    Raises:
        UserGuideError: a placeholder is unknown or cannot be filled.
    """
    fillers: dict[str, Callable[[str], str]] = {
        "const": _fill_const,
        "count": _fill_count,
        "size": _fill_size,
        "seconds": _fill_seconds,
        "key": _fill_key,
        "shortcuts": _fill_shortcuts,
        "dir": _fill_dir,
        "css": lambda name: _fill_css(fonts, name),
        "below": _fill_below,
        "image": lambda argument: _picture(images, argument),
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

    filled = _PLACEHOLDER.sub(fill, template)
    if "{{" in filled or "}}" in filled:
        raise UserGuideError("the user guide has an unfinished placeholder")
    return filled


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
