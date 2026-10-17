# SPDX-License-Identifier: GPL-3.0-or-later
"""The design tokens are the only place a colour or a size is written.

These tests are the lint rule for the interface: they fail the build
when a colour appears outside the tokens, when the generated tokens were
edited by hand, when the stylesheet template uses a literal where a token
belongs, or when a colour pair is too faint to read.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import fields
from pathlib import Path

import pytest

from app.constants import (
    LEGACY_THEME_VALUES,
    SEVERITY_KEYWORDS,
    TAG_COMMAND,
    TAG_DATA,
    THEME_FOLLOW_SYSTEM,
    ThemeID,
)
from ui.themes import design_tokens, icons, tokens
from ui.themes.theme_manager import (
    StylesheetError,
    font_sizes,
    font_weights,
    normalise_preference,
    render_stylesheet,
)

ROOT = Path(__file__).resolve().parent.parent
TOKENS_FILE = ROOT / "ui" / "themes" / "tokens.py"
GENERATED_FILE = ROOT / "ui" / "themes" / "design_tokens.py"
ICON_RECORD = ROOT / "resources" / "icons" / "icons.json"
TEMPLATE_FILE = ROOT / "ui" / "themes" / "wiretrace.qss.template"
ICONS_DIR = ROOT / "resources" / "icons"

#: Directories whose Python files draw the interface.
INTERFACE_DIRS = ("ui", "app", "updater")

#: A colour written out: #RGB, #RRGGBB, #RRGGBBAA, rgb(...) or rgba(...).
COLOUR_LITERAL = re.compile(r"#[0-9A-Fa-f]{3,8}\b|\brgba?\(")

#: WCAG 2.x AA for normal text, and for marks that are not text.
TEXT_CONTRAST = 4.5
MARK_CONTRAST = 3.0

PALETTE_IDS = [theme.value for theme in tokens.PALETTES]
PALETTE_VALUES = list(tokens.PALETTES.values())


def _interface_files() -> list[Path]:
    files: list[Path] = []
    for directory in INTERFACE_DIRS:
        files.extend(sorted((ROOT / directory).rglob("*.py")))
    return [path for path in files if path not in (TOKENS_FILE, GENERATED_FILE)]


@pytest.fixture
def icon_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Send coloured icon copies to the test's temp directory."""
    monkeypatch.setattr(icons, "get_cache_dir", lambda: str(tmp_path))
    return tmp_path


# ── Colours live in one place ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path", _interface_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_no_colour_is_written_outside_the_tokens(path: Path) -> None:
    offending = [
        f"line {number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if COLOUR_LITERAL.search(line)
    ]
    assert not offending, (
        f"{path.relative_to(ROOT)} writes a colour; use ui.themes.tokens instead:\n"
        + "\n".join(offending)
    )


def test_template_has_no_literal_colour() -> None:
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    assert not COLOUR_LITERAL.findall(template)


@pytest.mark.parametrize("declaration", ["border-radius", "font-size", "font-weight"])
def test_template_takes_radius_and_text_from_tokens(declaration: str) -> None:
    template = TEMPLATE_FILE.read_text(encoding="utf-8")
    values = re.findall(rf"{declaration}:\s*([^;]+);", template)
    assert values, f"the template sets no {declaration}"
    literal = [v for v in values if "{{" not in v and v.strip() != "0px"]
    assert not literal, f"{declaration} written as a literal: {literal}"


@pytest.mark.parametrize("path", sorted(ICONS_DIR.glob("*.svg")), ids=lambda p: p.name)
def test_icons_carry_no_colour_of_their_own(path: Path) -> None:
    drawing = path.read_text(encoding="utf-8")
    assert "currentColor" in drawing
    assert not COLOUR_LITERAL.search(drawing)


# ── The palettes ─────────────────────────────────────────────────────────


def test_every_theme_has_a_palette() -> None:
    assert set(tokens.PALETTES) == set(ThemeID)


@pytest.mark.parametrize("palette", PALETTE_VALUES, ids=PALETTE_IDS)
def test_every_colour_is_a_full_hex_value(palette: tokens.Palette) -> None:
    for item in fields(palette):
        value = getattr(palette, item.name)
        colours = value if isinstance(value, tuple) else (value,)
        for colour in colours:
            if isinstance(colour, tokens.Tint):
                assert 0 < colour.opacity < 1, item.name
                colour = colour.colour
            assert re.fullmatch(r"#[0-9A-F]{6}", colour), f"{item.name} = {colour!r}"


@pytest.mark.parametrize("palette", PALETTE_VALUES, ids=PALETTE_IDS)
def test_the_dot_grid_is_ink_and_its_glow_the_accent(palette: tokens.Palette) -> None:
    assert palette.dot.colour == palette.text
    assert palette.dot_glow.colour == palette.accent


def test_palettes_define_the_same_number_of_plot_traces() -> None:
    assert len({len(p.plot_traces) for p in PALETTE_VALUES}) == 1


@pytest.mark.parametrize("palette", PALETTE_VALUES, ids=PALETTE_IDS)
@pytest.mark.parametrize(
    "role",
    [
        "text", "text_secondary", "text_tertiary", "accent_strong",
        "error", "warning", "info", "command", "success",
    ],
)
def test_text_is_readable_on_every_plain_background(
    palette: tokens.Palette, role: str
) -> None:
    for background in (palette.paper, palette.surface):
        ratio = tokens.contrast_ratio(getattr(palette, role), background)
        assert ratio >= TEXT_CONTRAST, f"{role} on {background}: {ratio:.2f}"


@pytest.mark.parametrize("palette", PALETTE_VALUES, ids=PALETTE_IDS)
@pytest.mark.parametrize("role", ["edge", "accent_strong"])
def test_control_edges_and_the_focus_ring_stand_out(palette: tokens.Palette, role: str) -> None:
    """A control's boundary and the focus ring need 3:1 (WCAG 2.2, 1.4.11)."""
    for background in (palette.paper, palette.surface):
        ratio = tokens.contrast_ratio(getattr(palette, role), background)
        assert ratio >= MARK_CONTRAST, f"{role} on {background}: {ratio:.2f}"


@pytest.mark.parametrize("palette", PALETTE_VALUES, ids=PALETTE_IDS)
def test_selected_text_stays_readable(palette: tokens.Palette) -> None:
    """Selected text, in the console too, is drawn in the text colour."""
    assert tokens.contrast_ratio(palette.text, palette.selection) >= TEXT_CONTRAST


@pytest.mark.parametrize("palette", PALETTE_VALUES, ids=PALETTE_IDS)
@pytest.mark.parametrize("role", ["text", "text_secondary", "error", "warning", "info"])
def test_text_is_readable_on_the_hover_and_chosen_fill(palette: tokens.Palette, role: str) -> None:
    """Menu items, chosen rows and checked level buttons sit on the divider fill."""
    ratio = tokens.contrast_ratio(getattr(palette, role), palette.divider)
    assert ratio >= TEXT_CONTRAST, f"{role} on divider: {ratio:.2f}"


@pytest.mark.parametrize("palette", PALETTE_VALUES, ids=PALETTE_IDS)
def test_severity_text_is_readable_on_its_row_tint(palette: tokens.Palette) -> None:
    assert tokens.contrast_ratio(palette.error, palette.error_row) >= TEXT_CONTRAST
    assert tokens.contrast_ratio(palette.warning, palette.warning_row) >= TEXT_CONTRAST
    for row in (palette.error_row, palette.warning_row):
        assert tokens.contrast_ratio(palette.text, row) >= TEXT_CONTRAST


@pytest.mark.parametrize("palette", PALETTE_VALUES, ids=PALETTE_IDS)
def test_filled_button_label_is_readable_in_every_state(palette: tokens.Palette) -> None:
    for fill in (palette.text, palette.primary_hover, palette.primary_pressed):
        assert tokens.contrast_ratio(palette.paper, fill) >= TEXT_CONTRAST


@pytest.mark.parametrize("palette", PALETTE_VALUES, ids=PALETTE_IDS)
def test_search_highlights_keep_their_text_readable(palette: tokens.Palette) -> None:
    assert tokens.contrast_ratio(palette.text, palette.search_hit) >= TEXT_CONTRAST


@pytest.mark.parametrize("palette", PALETTE_VALUES, ids=PALETTE_IDS)
@pytest.mark.parametrize("role", ["connected", "logging"])
def test_status_marks_stand_out(palette: tokens.Palette, role: str) -> None:
    for background in (palette.paper, palette.surface):
        ratio = tokens.contrast_ratio(getattr(palette, role), background)
        assert ratio >= MARK_CONTRAST, f"{role} on {background}: {ratio:.2f}"


def test_every_severity_has_a_console_style() -> None:
    assert set(tokens.TAG_STYLES) == {*SEVERITY_KEYWORDS, TAG_COMMAND, TAG_DATA}
    assert tokens.tag_style("NO_SUCH_TAG") is tokens.TAG_STYLES[TAG_DATA]


@pytest.mark.parametrize("palette", PALETTE_VALUES, ids=PALETTE_IDS)
@pytest.mark.parametrize("tag", sorted(tokens.TAG_STYLES))
def test_console_tag_is_readable_on_its_row(palette: tokens.Palette, tag: str) -> None:
    style = tokens.tag_style(tag)
    background = getattr(palette, style.row_role) if style.row_role else palette.paper
    for role in (style.label_role, style.text_role):
        ratio = tokens.contrast_ratio(getattr(palette, role), background)
        assert ratio >= TEXT_CONTRAST, f"{tag} {role}: {ratio:.2f}"
    assert len(style.label) <= 3


# ── Colour helpers ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("colour", ["#FFF", "FFFFFFF", "", "#GGGGGG"])
def test_malformed_colour_is_rejected(colour: str) -> None:
    with pytest.raises(ValueError):
        tokens.relative_luminance(colour)


def test_contrast_of_black_on_white_is_the_maximum() -> None:
    assert tokens.contrast_ratio("#000000", "#FFFFFF") == pytest.approx(21.0)
    assert tokens.contrast_ratio("#FFFFFF", "#000000") == pytest.approx(21.0)
    assert tokens.contrast_ratio("#777777", "#777777") == pytest.approx(1.0)


def test_with_alpha_writes_a_stylesheet_colour() -> None:
    assert tokens.with_alpha("#141413", 1.0) == "rgba(20, 20, 19, 255)"
    assert tokens.with_alpha("#141413", 0.0) == "rgba(20, 20, 19, 0)"


def test_active_theme_selects_the_palette() -> None:
    before = tokens.active_theme()
    try:
        for theme, palette in tokens.PALETTES.items():
            tokens.set_active_theme(theme)
            assert tokens.active_theme() is theme
            assert tokens.palette() is palette
    finally:
        tokens.set_active_theme(before)


def test_unknown_theme_is_rejected() -> None:
    with pytest.raises(ValueError):
        tokens.set_active_theme("sepia")  # type: ignore[arg-type]


# ── The stylesheet template ──────────────────────────────────────────────────


@pytest.mark.parametrize("theme", list(tokens.PALETTES), ids=PALETTE_IDS)
def test_template_fills_completely(theme: ThemeID, icon_cache: Path) -> None:
    before = tokens.active_theme()
    tokens.set_active_theme(theme)
    try:
        stylesheet = render_stylesheet(
            TEMPLATE_FILE.read_text(encoding="utf-8"), tokens.PALETTES[theme]
        )
    finally:
        tokens.set_active_theme(before)

    assert "{{" not in stylesheet and "}}" not in stylesheet
    assert "/*" not in stylesheet
    assert stylesheet.count("{") == stylesheet.count("}")
    assert tokens.PALETTES[theme].paper in stylesheet
    urls = re.findall(r'url\("([^"]+)"\)', stylesheet)
    assert urls and len(urls) == stylesheet.count("url(")
    for url in urls:
        assert Path(url).is_file(), url


@pytest.mark.parametrize(
    "template",
    [
        "QWidget { color: {{no_such_colour}}; }",
        "QWidget { padding: {{m.no_such_size}}px; }",
        "QWidget { font-size: {{f.no_such_size}}px; }",
        "QWidget { font-weight: {{w.bold}}; }",
        "QWidget { image: url({{icon:no_such_icon:text}}); }",
        "QWidget { image: url({{icon:checkmark:no_such_role}}); }",
        "QWidget { image: url({{icon:checkmark}}); }",
        "QWidget { color: {{}}; }",
        "QWidget { color: {{text; }",
    ],
)
def test_unknown_placeholder_is_an_error(template: str, icon_cache: Path) -> None:
    with pytest.raises(StylesheetError):
        render_stylesheet(template, tokens.LIGHT)


def test_placeholder_inside_a_comment_is_left_alone(icon_cache: Path) -> None:
    template = "/* example: {{not_a_token}} */ QWidget { color: {{text}}; }"
    assert render_stylesheet(template, tokens.LIGHT).strip() == (
        f"QWidget {{ color: {tokens.LIGHT.text}; }}"
    )


def test_every_object_name_in_the_template_exists_in_the_code() -> None:
    template = re.sub(r"/\*.*?\*/", "", TEMPLATE_FILE.read_text(encoding="utf-8"), flags=re.S)
    names = set(re.findall(r"#([A-Za-z_]+)", template))
    names.discard("qt_scrollarea_viewport")  # named by Qt itself
    code = "\n".join(path.read_text(encoding="utf-8") for path in _interface_files())
    # A name is given with setObjectName("name"), or handed as "name" to
    # a helper that does so.
    missing = sorted(n for n in names if f'"{n}"' not in code)
    assert not missing, f"the stylesheet styles names no widget has: {missing}"


def test_text_sizes_are_the_type_scale_in_whole_pixels() -> None:
    sizes = font_sizes()
    assert set(sizes) == set(tokens.TYPE_STYLES)
    assert all(re.fullmatch(r"\d+", size) for size in sizes.values())
    assert min(int(size) for size in sizes.values()) >= 12


def test_type_styles_sit_on_the_grid() -> None:
    for name, style in tokens.TYPE_STYLES.items():
        assert style.line_height % 4 == 0, name
        assert style.line_height >= style.size, name
        assert style.family in design_tokens.FONT_FAMILIES, name


def test_only_the_bundled_weights_are_used() -> None:
    """Poppins ships in Regular and Medium only; no other weight exists."""
    assert set(font_weights()) == {"regular", "medium"}
    used = {style.weight for style in tokens.TYPE_STYLES.values()}
    assert used <= {tokens.TYPOGRAPHY.regular, tokens.TYPOGRAPHY.medium}


def test_metrics_are_positive_whole_numbers() -> None:
    for field in fields(tokens.METRICS):
        value = getattr(tokens.METRICS, field.name)
        assert isinstance(value, int) and value > 0, field.name
    for name, value in tokens.METRICS.content_sizes().items():
        assert isinstance(value, int) and value > 0, name


def test_every_size_is_a_token_of_the_language() -> None:
    for field in fields(tokens.METRICS):
        token = field.metadata["token"]
        known = {**design_tokens.MEASURES, **design_tokens.NUMBERS}
        assert getattr(tokens.METRICS, field.name) == known[token], field.name


def test_every_wiretrace_size_of_the_language_is_used() -> None:
    """A size the language gives WireTrace that no part of it reads is a
    decision that was not carried out. It is read by the interface, as a
    field of ``Metrics``, or by the user guide, as a CSS property."""
    used = {field.metadata["token"] for field in fields(tokens.METRICS)}
    guide = (ROOT / "resources" / "help" / "user_guide.html").read_text(encoding="utf-8")
    used |= {
        name for name in design_tokens.MEASURES
        if f"var(--{name.replace('.', '-')})" in guide
    }
    own = {
        name for name in (*design_tokens.MEASURES, *design_tokens.NUMBERS)
        if name.startswith("wiretrace.")
    }
    assert own <= used, sorted(own - used)


# ── The generated tokens ─────────────────────────────────────────────────────

_PLACEHOLDER = "@@BODY_SHA256@@"


def test_the_generated_tokens_were_not_edited_by_hand() -> None:
    """``design_tokens.py`` is written by the website repository's
    ``design/generate.mjs``; it records the SHA-256 of its own body."""
    text = GENERATED_FILE.read_text(encoding="utf-8")
    body = text.replace(design_tokens.BODY_SHA256, _PLACEHOLDER, 1)
    assert hashlib.sha256(body.encode("utf-8")).hexdigest() == design_tokens.BODY_SHA256
    assert re.fullmatch(r"[0-9a-f]{64}", design_tokens.SOURCES_SHA256)


# ── Icons ────────────────────────────────────────────────────────────────────


def test_every_icon_is_recorded_and_unchanged() -> None:
    """The icons are Lucide's, brought in by ``tools/import_icons.py``."""
    record = json.loads(ICON_RECORD.read_text(encoding="utf-8"))
    shipped = {path.stem: path for path in ICONS_DIR.glob("*.svg")}
    assert set(record["icons"]) == set(shipped)
    for name, entry in record["icons"].items():
        digest = hashlib.sha256(shipped[name].read_bytes()).hexdigest()
        assert digest == entry["sha256"], name
    licence = ICONS_DIR / record["license"]
    assert hashlib.sha256(licence.read_bytes()).hexdigest() == record["license_sha256"]


@pytest.mark.parametrize("path", sorted(ICONS_DIR.glob("*.svg")), ids=lambda p: p.name)
def test_icons_are_drawn_on_one_grid_with_the_language_strokes(path: Path) -> None:
    """Pictures are drawn with a one-pixel line at the icon size; a
    control's mark (the tick of a check box) with the two-pixel mark at
    the indicator's size."""
    record = json.loads(ICON_RECORD.read_text(encoding="utf-8"))
    metrics = tokens.METRICS
    assert float(record["stroke_width"]) == metrics.border * 24 / metrics.icon
    assert float(record["mark_stroke_width"]) == metrics.mark * 24 / metrics.indicator
    drawing = path.read_text(encoding="utf-8")
    assert 'viewBox="0 0 24 24"' in drawing
    strokes = set(re.findall(r'stroke-width="([^"]+)"', drawing))
    expected = record["icons"][path.stem].get("stroke_width")
    assert strokes == ({expected} if expected else set())
    assert expected in (None, record["stroke_width"], record["mark_stroke_width"])


# ── One rule for the size of a button ────────────────────────────────────────

#: The only stylesheet rules that may size a button.
BUTTON_SIZE_RULES = {
    "QPushButton",
    'QPushButton[compact="true"]',
    # A focused button's outline is a pixel wider; its padding gives it back.
    "QPushButton:focus",
    'QPushButton[compact="true"]:focus',
    "QPushButton#iconButton",
    "QPushButton#iconButton:focus",
    'QPushButton#iconButton[compact="true"]',
    'QPushButton#iconButton[compact="true"]:focus',
    "QPushButton#flatIconButton",
    'QPushButton#flatIconButton[compact="true"]',
    "QPushButton#flatIconButton:focus",
    'QPushButton#flatIconButton[compact="true"]:focus',
}

_SIZE_DECLARATION = re.compile(r"(?:^|[\s;{])(?:padding|(?:min-|max-)?(?:width|height))[a-z-]*\s*:")

BUTTON_FILE = ROOT / "ui" / "widgets" / "button.py"
MESSAGES_FILE = ROOT / "ui" / "dialogs" / "messages.py"

#: A size set on a button outside the rule.
_SIZE_CALL = re.compile(
    r"(\w+)\.set(?:Fixed|Minimum|Maximum)(?:Width|Height|Size)\("
)
_BUTTON_MADE = re.compile(
    r"(\w+)(?:: [\w\[\] |,]+)? = (?:Button|action_button|self\._button|self\._icon_button)\("
)


def test_only_the_button_rules_size_a_button() -> None:
    template = re.sub(r"/\*.*?\*/", "", TEMPLATE_FILE.read_text(encoding="utf-8"), flags=re.S)
    template = re.sub(r"\{\{.*?\}\}", "token", template)
    sized = {
        " ".join(selector.split())
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", template)
        if "QPushButton" in selector and "::" not in selector and _SIZE_DECLARATION.search(body)
    }
    assert sized == BUTTON_SIZE_RULES


@pytest.mark.parametrize(
    "path", _interface_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_no_button_is_sized_in_the_code(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    buttons = set(_BUTTON_MADE.findall(source)) | {"button"}
    sized = [name for name in _SIZE_CALL.findall(source) if name in buttons]
    assert not sized, f"sized outside the rule: {sorted(set(sized))}"


_FIELD_MADE = re.compile(
    r"(\w+)(?:: [\w\[\] |,]+)? = (?:QLineEdit|QComboBox|QSpinBox|PortCombo|self\._combo|self\._number)\("
)
_HEIGHT_CALL = re.compile(r"(\w+)\.set(?:Fixed|Minimum|Maximum)(?:Height|Size)\(")


@pytest.mark.parametrize(
    "path", _interface_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_no_one_line_field_is_given_a_height_in_the_code(path: Path) -> None:
    """The stylesheet gives every field its height; the code gives widths only."""
    source = path.read_text(encoding="utf-8")
    fields_made = set(_FIELD_MADE.findall(source))
    sized = [name for name in _HEIGHT_CALL.findall(source) if name in fields_made]
    assert not sized, f"height set outside the stylesheet: {sorted(set(sized))}"


#: A gap or a margin worked out on the spot instead of taken from the scale.
_SPACING_SUM = re.compile(
    r"\.(?:setSpacing|setHorizontalSpacing|setVerticalSpacing|addSpacing|setContentsMargins)"
    r"\([^)]*(?://|[-+*])"
)


@pytest.mark.parametrize(
    "path", _interface_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_no_gap_is_worked_out_on_the_spot(path: Path) -> None:
    found = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _SPACING_SUM.search(line)
    ]
    assert not found, "use a step of the spacing scale:\n" + "\n".join(found)


def test_template_has_no_size_written_as_a_number() -> None:
    """Every size in the stylesheet is a token; only zero is written out."""
    template = re.sub(r"/\*.*?\*/", "", TEMPLATE_FILE.read_text(encoding="utf-8"), flags=re.S)
    found = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(template.splitlines(), start=1)
        if re.search(r"(?<![\w}.])(?!0(?:px|pt))\d+(?:\.\d+)?(?:px|pt)\b", line)
    ]
    assert not found, "\n".join(found)


@pytest.mark.parametrize(
    "path", _interface_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_every_button_is_the_shared_button(path: Path) -> None:
    if path == BUTTON_FILE:
        return
    assert "QPushButton" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path", _interface_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_small_dialogs_are_built_in_one_place(path: Path) -> None:
    """The toolkit's own boxes make buttons that do not follow the rule."""
    if path == MESSAGES_FILE:
        return
    source = path.read_text(encoding="utf-8")
    assert "QMessageBox" not in source
    assert "QInputDialog" not in source


# ── Coloured icons ───────────────────────────────────────────────────────────


def test_coloured_icon_is_written_once_and_reused(icon_cache: Path) -> None:
    first = icons.coloured_icon_path("checkmark", "#123456")
    assert first is not None
    drawing = Path(first).read_text(encoding="utf-8")
    assert "#123456" in drawing and "currentColor" not in drawing

    modified = Path(first).stat().st_mtime_ns
    assert icons.coloured_icon_path("checkmark", "#123456") == first
    assert Path(first).stat().st_mtime_ns == modified
    assert icons.coloured_icon_path("checkmark", "#654321") != first


def test_stale_coloured_icon_is_rewritten(icon_cache: Path) -> None:
    path = icons.coloured_icon_path("checkmark", "#123456")
    assert path is not None
    Path(path).write_text("stale", encoding="utf-8")
    assert icons.coloured_icon_path("checkmark", "#123456") == path
    assert "#123456" in Path(path).read_text(encoding="utf-8")


def test_unknown_icon_has_no_path(icon_cache: Path) -> None:
    assert icons.coloured_icon_path("no_such_icon", "#123456") is None
    with pytest.raises(LookupError):
        icons.stylesheet_url("no_such_icon", "text")


def test_unwritable_cache_falls_back_to_the_original_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    monkeypatch.setattr(icons, "get_cache_dir", lambda: str(blocker))
    assert icons.coloured_icon_path("checkmark", "#123456") == str(
        ICONS_DIR / "checkmark.svg"
    )


def test_stylesheet_path_escapes_what_a_quoted_string_cannot_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Windows does not allow a double quote in a folder name, so the
    # path is supplied directly instead of being created on disk.
    monkeypatch.setattr(
        icons, "coloured_icon_path", lambda name, colour: '/cache/a "quoted" name/icon.svg'
    )
    assert icons.stylesheet_url("checkmark", "text") == (
        '/cache/a \\"quoted\\" name/icon.svg'
    )


def test_stylesheet_path_uses_forward_slashes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(icons.os, "sep", "\\")
    monkeypatch.setattr(
        icons, "coloured_icon_path", lambda name, colour: "C:\\Users\\First Last\\icon.svg"
    )
    assert icons.stylesheet_url("checkmark", "text") == "C:/Users/First Last/icon.svg"


def test_every_shipped_icon_is_used() -> None:
    sources = "".join(
        path.read_text(encoding="utf-8")
        for path in [*_interface_files(), TEMPLATE_FILE]
    )
    unused = [
        name for name in icons.icon_names()
        if f'"{name}"' not in sources and f"icon:{name}:" not in sources
    ]
    assert not unused, f"icons nothing draws: {unused}"


def test_icon_names_lists_the_shipped_icons() -> None:
    names = icons.icon_names()
    assert names == sorted(names)
    assert {"checkmark", "chevron_down", "chevron_up", "radio_dot", "tab_close"} <= set(names)


# ── Sizes live in one place ──────────────────────────────────────────────────

#: A size written out where a token belongs: a fixed, least or greatest
#: size, a margin, a spacing or a text size given as a number.
SIZE_LITERAL = re.compile(
    r"\.set(?:Fixed|Minimum|Maximum)(?:Size|Width|Height)\(\s*[1-9]"
    r"|\.setContentsMargins\(\s*[^)]*\b[1-9]\d*\b"
    r"|\.setSpacing\(\s*[1-9]"
    r"|\.addSpacing\(\s*[1-9]"
    r"|\.setPointSize(?:F)?\(\s*[1-9]"
)

#: Files still allowed a literal size. Every file has been moved onto
#: the tokens, so the list is empty; a new file must not be added to it.
SIZE_LITERAL_ALLOWED: frozenset[str] = frozenset()


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


@pytest.mark.parametrize("path", _interface_files(), ids=_relative)
def test_no_size_is_written_outside_the_tokens(path: Path) -> None:
    if _relative(path) in SIZE_LITERAL_ALLOWED:
        pytest.skip("not yet moved onto the tokens")
    found = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if SIZE_LITERAL.search(line)
    ]
    assert not found, "sizes belong in ui/themes/tokens.py:\n" + "\n".join(found)


def test_every_file_allowed_a_literal_size_still_has_one() -> None:
    stale = [
        name for name in sorted(SIZE_LITERAL_ALLOWED)
        if not SIZE_LITERAL.search((ROOT / name).read_text(encoding="utf-8"))
    ]
    assert not stale, f"remove from SIZE_LITERAL_ALLOWED: {stale}"


# ── Theme preference ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        (None, THEME_FOLLOW_SYSTEM),
        ("", THEME_FOLLOW_SYSTEM),
        ("system", THEME_FOLLOW_SYSTEM),
        ("light", "light"),
        ("dark", "dark"),
        ("studio_light", "light"),
        ("midnight_dark", "dark"),
        ("solarized", THEME_FOLLOW_SYSTEM),
        ("LIGHT", THEME_FOLLOW_SYSTEM),
    ],
)
def test_stored_preference_is_normalised(stored: str | None, expected: str) -> None:
    assert normalise_preference(stored) == expected


def test_legacy_names_map_to_current_themes() -> None:
    assert set(LEGACY_THEME_VALUES.values()) == {theme.value for theme in ThemeID}


SCROLL_FILE = ROOT / "ui" / "widgets" / "vertical_scroll.py"


@pytest.mark.parametrize(
    "path", _interface_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_every_scrolled_page_is_the_shared_scroll_area(path: Path) -> None:
    """A page scrolls up and down only, and its area asks for the page's
    width, so no row is cut at the side."""
    if path == SCROLL_FILE:
        return
    assert "QScrollArea(" not in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "path", _interface_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_rich_text_is_never_bold(path: Path) -> None:
    """Poppins ships in Regular and Medium; emphasis is the medium weight."""
    source = path.read_text(encoding="utf-8").lower()
    assert "<b>" not in source and "<strong>" not in source and "font-weight: bold" not in source


def test_template_writes_a_state_after_its_subcontrol() -> None:
    """Qt reads ``QCheckBox:focus::indicator`` as a rule for the whole check
    box; the state of a part is written after it: ``::indicator:focus``."""
    template = re.sub(r"/\*.*?\*/", "", TEMPLATE_FILE.read_text(encoding="utf-8"), flags=re.S)
    assert not re.findall(r"[\w\]]:[\w!-]+::", template)
