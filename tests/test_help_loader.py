# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the user guide: finding it, filling its placeholders from the
program's own constants, and writing the filled page to the cache.

The guide shipped in ``resources/help`` is checked as well, so a
placeholder that cannot be filled, a link that leads nowhere or a colour
written by hand fails here and not in front of a user.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import app.help_loader as help_loader
from app import constants
from app.constants import ThemeID
from app.help_loader import (
    FONTS_DIRECTORY,
    IMAGES_DIRECTORY,
    USER_GUIDE_FILENAME,
    UserGuideError,
    UserGuideNotFoundError,
    key_names,
    load_user_guide,
    prepare_user_guide,
    render_user_guide,
    resolve_user_guide_source,
)
from core.records import COMMAND_PREFIX
from ui import shortcuts
from ui.themes import design_tokens, tokens
from version import APP_NAME, APP_VERSION

GUIDE = resolve_user_guide_source()
TEMPLATE = GUIDE.read_text(encoding="utf-8")
IMAGES = GUIDE.parent / IMAGES_DIRECTORY
FONTS = GUIDE.parent.parent / FONTS_DIRECTORY

#: The smallest file a browser accepts as a PNG is not needed here: the
#: loader embeds the bytes as they are.
_PICTURE_BYTES = b"picture"


@pytest.fixture
def images(tmp_path: Path) -> Path:
    """A folder with one picture, "screen", in both themes."""
    for theme in ("light", "dark"):
        (tmp_path / f"screen-{theme}.png").write_bytes(_PICTURE_BYTES)
    return tmp_path


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Send the filled guide to the test's own folder."""
    directory = tmp_path / "cache"
    monkeypatch.setattr(help_loader, "get_cache_dir", lambda: str(directory))
    return directory


# ── Finding the guide ────────────────────────────────────────────────────────


def test_guide_is_found_beside_the_source_tree() -> None:
    assert GUIDE.name == USER_GUIDE_FILENAME
    assert GUIDE.is_file()


def test_missing_guide_names_every_place_looked_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(help_loader, "__file__", str(tmp_path / "app" / "help_loader.py"))
    monkeypatch.setattr(help_loader.sys, "executable", str(tmp_path / "bin" / "python"))
    monkeypatch.delattr(help_loader.sys, "_MEIPASS", raising=False)

    with pytest.raises(UserGuideNotFoundError) as raised:
        resolve_user_guide_source()

    assert str(tmp_path / "resources" / "help" / USER_GUIDE_FILENAME) in str(raised.value)
    assert str(tmp_path / "bin" / "resources" / "help" / USER_GUIDE_FILENAME) in str(raised.value)


def test_guide_is_found_where_a_one_file_build_unpacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    unpacked = tmp_path / "unpacked"
    target = unpacked / "resources" / "help" / USER_GUIDE_FILENAME
    target.parent.mkdir(parents=True)
    target.write_text("guide", encoding="utf-8")
    monkeypatch.setattr(help_loader, "__file__", str(tmp_path / "app" / "help_loader.py"))
    monkeypatch.setattr(help_loader.sys, "executable", str(tmp_path / "bin" / "python"))
    monkeypatch.setattr(help_loader.sys, "_MEIPASS", str(unpacked), raising=False)

    assert resolve_user_guide_source() == target


# ── Filling the placeholders ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("placeholder", "expected"),
    [
        ("{{const:APP_NAME}}", APP_NAME),
        ("{{const:APP_VERSION}}", APP_VERSION),
        ("{{const:DEFAULT_BAUD_RATE}}", str(constants.DEFAULT_BAUD_RATE)),
        ("{{const:BAUD_RATES}}", ", ".join(str(rate) for rate in constants.BAUD_RATES)),
        ("{{const:PLOT_REGEX_TIMEOUT_SECONDS}}", f"{constants.PLOT_REGEX_TIMEOUT_SECONDS:g}"),
        (
            "{{const:SEVERITY_KEYWORDS.ERROR}}",
            ", ".join(constants.SEVERITY_KEYWORDS["ERROR"]),
        ),
        ("{{const:COMMAND_PREFIX}}", COMMAND_PREFIX.replace(">", "&gt;")),
        ("{{count:MAX_CONSOLE_LINES}}", f"{constants.MAX_CONSOLE_LINES:,}"),
        ("{{seconds:LOG_FSYNC_INTERVAL_MS}}", f"{constants.LOG_FSYNC_INTERVAL_MS // 1000}"),
        ("{{below:lg}}", f"(max-width: {design_tokens.MEASURES['web.breakpoint.lg'] - 0.02:g}px)"),
    ],
)
def test_placeholder_is_filled_from_the_program(placeholder: str, expected: str, images: Path) -> None:
    assert render_user_guide(f"[{placeholder}]", images, FONTS) == f"[{expected}]"


def test_a_size_is_written_in_its_largest_unit(images: Path) -> None:
    assert render_user_guide("{{size:SPOOL_MAX_BYTES}}", images, FONTS) == "1 GB"


@pytest.mark.parametrize("theme", list(ThemeID), ids=[theme.value for theme in ThemeID])
def test_every_colour_role_of_a_theme_is_a_css_property(theme: ThemeID, images: Path) -> None:
    filled = render_user_guide(f"{{{{css:{theme.value}}}}}", images, FONTS)
    palette = tokens.PALETTES[theme]

    assert f"--color-paper: {palette.paper};" in filled
    assert f"--color-accent-strong: {palette.accent_strong};" in filled
    dot = re.search(r"--color-dot: (#[0-9A-F]{8});", filled)
    assert dot and dot.group(1).startswith(palette.dot.colour)
    assert int(dot.group(1)[-2:], 16) == round(palette.dot.opacity * 255)


def test_every_size_and_style_of_the_language_is_a_css_property(images: Path) -> None:
    filled = render_user_guide("{{css:measures}}", images, FONTS)

    assert f"--space-4: {tokens.METRICS.space_4}px;" in filled
    assert f"--radius-sm: {tokens.METRICS.radius_sm}px;" in filled
    for name, style in tokens.TYPE_STYLES.items():
        assert f"--text-{name}: {style.size}px;" in filled
        assert f"--leading-{name}: {style.line_height}px;" in filled
    mono = re.search(r"--font-mono: ([^;]+);", filled)
    assert mono and mono.group(1).startswith(f'"{tokens.TYPOGRAPHY.console_family}"')
    assert mono.group(1).endswith("monospace")


def test_the_bundled_fonts_are_embedded(images: Path) -> None:
    filled = render_user_guide("{{css:fonts}}", images, FONTS)
    typography = tokens.TYPOGRAPHY

    faces = re.findall(r'font-family: "([^"]+)"; font-weight: (\d+);', filled)
    assert sorted(faces) == sorted(
        (family, str(weight))
        for family in (typography.interface_family, typography.console_family)
        for weight in (typography.regular, typography.medium)
    )
    assert filled.count("data:font/ttf;base64,") == len(faces)


def test_folders_are_those_of_this_computer(
    images: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(help_loader, "get_config_dir", lambda: "/settings & more")
    monkeypatch.setattr(help_loader, "get_default_log_dir", lambda: "/logs")
    monkeypatch.setattr(help_loader, "get_cache_dir", lambda: "/cache")

    filled = render_user_guide("{{dir:config}}|{{dir:logs}}|{{dir:cache}}", images, FONTS)

    assert filled == "/settings &amp; more|/logs|/cache"


@pytest.mark.parametrize(
    ("keys", "platform", "expected"),
    [
        ("Ctrl+T", "linux", ["Ctrl", "T"]),
        ("Ctrl+T", "win32", ["Ctrl", "T"]),
        ("Ctrl+T", "darwin", ["Cmd", "T"]),
        ("Ctrl+Shift+C", "darwin", ["Cmd", "Shift", "C"]),
        ("Ctrl++", "linux", ["Ctrl", "+"]),
        ("Ctrl+,", "linux", ["Ctrl", ","]),
        ("F3", "darwin", ["F3"]),
    ],
)
def test_keys_are_named_as_the_platform_names_them(
    keys: str, platform: str, expected: list[str],
) -> None:
    assert key_names(keys, platform) == expected


def test_a_shortcut_is_written_as_keys(images: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(help_loader.sys, "platform", "linux")

    filled = render_user_guide("{{key:copy_messages}}", images, FONTS)

    assert filled == "<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>C</kbd>"


def test_the_shortcut_table_holds_every_shortcut_under_its_group(images: Path) -> None:
    filled = render_user_guide("{{shortcuts}}", images, FONTS)

    assert filled.count("<tr><td>") == len(shortcuts.SHORTCUTS)
    for shortcut in shortcuts.SHORTCUTS.values():
        assert f"<td>{shortcut.description}</td>" in filled
    groups = list(dict.fromkeys(shortcut.group for shortcut in shortcuts.SHORTCUTS.values()))
    assert re.findall(r'scope="colgroup">([^<]+)<', filled) == groups
    for name, alternates in shortcuts.ALTERNATE_KEYS.items():
        row = next(
            line for line in filled.splitlines()
            if f"<td>{shortcuts.SHORTCUTS[name].description}</td>" in line
        )
        assert row.count(" or ") == len(alternates)


def test_a_picture_carries_both_themes_and_a_description(images: Path) -> None:
    filled = render_user_guide("{{image:screen|The window & its parts}}", images, FONTS)

    assert filled.startswith("<picture>")
    assert filled.count("data:image/png;base64,") == 2
    assert 'media="(prefers-color-scheme: dark)"' in filled
    assert 'alt="The window &amp; its parts"' in filled


@pytest.mark.parametrize(
    "template",
    [
        "{{const:NO_SUCH_CONSTANT}}",
        "{{const:SEVERITY_KEYWORDS.NO_SUCH_LEVEL}}",
        "{{count:APP_NAME}}",
        "{{key:no_such_shortcut}}",
        "{{dir:nowhere}}",
        "{{css:sepia}}",
        "{{below:huge}}",
        "{{nonsense}}",
        "{{image:missing|A picture that is not there}}",
        "{{image:screen}}",
        "{{image:screen| }}",
        "{{ const:APP_NAME }}",
        "{{const:APP_NAME",
    ],
)
def test_a_placeholder_that_cannot_be_filled_is_an_error(template: str, images: Path) -> None:
    with pytest.raises(UserGuideError):
        render_user_guide(template, images, FONTS)


# ── The guide that is shipped ────────────────────────────────────────────────


@pytest.fixture(scope="module")
def guide() -> str:
    return load_user_guide()


def _without_pictures(page: str) -> str:
    """The page without its embedded pictures and fonts."""
    page = re.sub(r'url\("data:font/ttf;base64,[A-Za-z0-9+/=]+"\)', "", page)
    return re.sub(r"data:image/png;base64,[A-Za-z0-9+/=]+", "", page)


def test_the_guide_fills_completely(guide: str) -> None:
    assert "{{" not in guide
    assert "}}" not in guide
    assert f"{APP_NAME} {APP_VERSION} user guide" in guide


def test_every_picture_of_the_guide_exists_in_both_themes() -> None:
    names = re.findall(r"\{\{image:([^|}]+)\|", TEMPLATE)

    assert names
    used = set()
    for name in names:
        for theme in ThemeID:
            picture = IMAGES / f"{name}-{theme.value}.png"
            assert picture.is_file(), picture.name
            used.add(picture.name)
    assert used == {path.name for path in IMAGES.iterdir()}


def test_every_link_inside_the_guide_leads_somewhere(guide: str) -> None:
    page = _without_pictures(guide)
    targets = re.findall(r'\bid="([^"]+)"', page)
    links = re.findall(r'href="#([^"]+)"', page)

    assert len(targets) == len(set(targets))
    assert links
    assert set(links) <= set(targets)


def test_the_contents_list_every_section(guide: str) -> None:
    page = _without_pictures(guide)
    contents = page[page.index("<nav"):page.index("</nav>")]
    sections = re.findall(r'<h2 id="([^"]+)"', page)

    assert re.findall(r'href="#([^"]+)"', contents) == sections


def test_the_guide_writes_no_colour_size_or_font_of_its_own() -> None:
    """Colours, sizes and fonts come from the design tokens, as everywhere else."""
    style = TEMPLATE[TEMPLATE.index("<style>"):TEMPLATE.index("</style>")]
    style = re.sub(r"/\*.*?\*/", "", style, flags=re.S)

    assert not re.findall(r"#[0-9A-Fa-f]{3,8}\b|\brgba?\(|\bhsla?\(", style)
    assert not re.findall(r"\d+(?:\.\d+)?(?:px|rem|em|pt)\b", style)
    assert "font-family:" not in style.replace("font-family: var(", "")
    for part in ("{{css:fonts}}", "{{css:measures}}", "{{css:light}}", "{{css:dark}}"):
        assert part in style


def test_the_guide_is_one_page_that_needs_nothing_else(guide: str) -> None:
    """No script, and nothing fetched: it works with no network."""
    page = _without_pictures(guide)

    assert "<script" not in page.lower()
    assert "<link" not in page.lower()
    assert "@import" not in page
    assert not re.findall(r'\b(?:src|srcset)="(?!")[^"]+"', page)
    assert not re.findall(r"url\(", page)


def test_the_guide_lists_every_shortcut(guide: str) -> None:
    for shortcut in shortcuts.SHORTCUTS.values():
        assert f"<td>{shortcut.description}</td>" in guide


def test_the_guide_names_every_level(guide: str) -> None:
    for keywords in constants.SEVERITY_KEYWORDS.values():
        assert ", ".join(keywords) in guide


# ── Making it ready for the browser ──────────────────────────────────────────


def test_filled_guide_is_written_to_the_cache_and_not_beside_the_source(cache: Path) -> None:
    path = prepare_user_guide()

    assert path.parent == cache / "help"
    assert APP_VERSION in path.name
    assert path.read_text(encoding="utf-8") == load_user_guide()
    assert "{{" not in path.read_text(encoding="utf-8")


def test_a_page_that_is_already_there_is_reused(cache: Path) -> None:
    first = prepare_user_guide()
    written = first.stat().st_mtime_ns

    second = prepare_user_guide()

    assert second == first
    assert second.stat().st_mtime_ns == written


def test_a_changed_guide_gets_a_new_page_and_the_old_one_is_removed(
    cache: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = prepare_user_guide()
    monkeypatch.setattr(help_loader, "get_default_log_dir", lambda: "/another/log/folder")

    second = prepare_user_guide()

    assert second != first
    assert "/another/log/folder" in second.read_text(encoding="utf-8")
    assert [path.name for path in (cache / "help").iterdir()] == [second.name]


def test_a_page_in_the_cache_that_was_changed_is_written_again(cache: Path) -> None:
    path = prepare_user_guide()
    path.write_text("<script>changed</script>", encoding="utf-8")

    again = prepare_user_guide()

    assert again == path
    assert again.read_text(encoding="utf-8") == load_user_guide()


def test_no_half_written_page_is_left_behind(cache: Path) -> None:
    prepare_user_guide()

    assert not list((cache / "help").glob("*.tmp"))


def test_other_files_in_the_cache_are_left_alone(cache: Path) -> None:
    folder = cache / "help"
    folder.mkdir(parents=True)
    other = folder / "notes.txt"
    other.write_text("mine", encoding="utf-8")

    prepare_user_guide()

    assert other.read_text(encoding="utf-8") == "mine"


def test_the_hex_example_is_what_the_hex_view_shows() -> None:
    from core.framing import HexFramer

    section = TEMPLATE.split('<h2 id="hex">', 1)[1].split("<h2", 1)[0]
    shown = re.search(r"<pre><code>(.*?)</code></pre>", section, re.DOTALL).group(1)
    line = re.search(r"the line <code>(.*?)</code> takes", section).group(1)
    framer = HexFramer()
    rows = framer.feed(line.encode("ascii") + b"\r\n") + framer.flush()

    assert shown.split("\n") == rows
