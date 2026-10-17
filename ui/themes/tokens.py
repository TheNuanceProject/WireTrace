# SPDX-License-Identifier: GPL-3.0-or-later
"""WireTrace design tokens: colours, sizes and type, as the interface reads them.

The values are the Nuance Design Language's, shared with
thenuanceproject.com. They are decided in one place, the token files of
the website repository, and generated into ``design_tokens.py``. This
module gives them the shape the interface uses: a palette per theme,
the sizes in logical pixels and the type styles. Nothing here is a value
of its own; every number and colour comes from ``design_tokens``, and a
test fails the build when a colour or a size is written anywhere else.

This module has no Qt dependency, so it can be imported and tested
anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

from app.constants import ThemeID
from ui.themes import design_tokens

# ── Colour helpers ───────────────────────────────────────────────────────────

_HEX_DIGITS = 6
_CHANNEL_MAX = 255


def _channels(colour: str) -> tuple[int, int, int]:
    """Split ``#RRGGBB`` into its three channel values."""
    digits = colour.lstrip("#")
    if len(digits) != _HEX_DIGITS:
        raise ValueError(f"expected #RRGGBB, got {colour!r}")
    return int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16)


def with_alpha(colour: str, opacity: float) -> str:
    """Return ``colour`` as a stylesheet ``rgba(...)`` value."""
    if not 0.0 <= opacity <= 1.0:
        raise ValueError(f"opacity must be between 0 and 1, got {opacity!r}")
    red, green, blue = _channels(colour)
    return f"rgba({red}, {green}, {blue}, {round(opacity * _CHANNEL_MAX)})"


def _linear(channel: int) -> float:
    value = channel / _CHANNEL_MAX
    if value <= 0.03928:
        return value / 12.92
    return ((value + 0.055) / 1.055) ** 2.4


def relative_luminance(colour: str) -> float:
    """Relative luminance as defined by WCAG 2.x."""
    red, green, blue = (_linear(c) for c in _channels(colour))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def contrast_ratio(colour: str, other: str) -> float:
    """Contrast ratio between two colours as defined by WCAG 2.x (1 to 21)."""
    lighter, darker = sorted(
        (relative_luminance(colour), relative_luminance(other)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


# ── Palette ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Tint:
    """A colour drawn at an opacity."""

    colour: str
    opacity: float

    @property
    def rgba(self) -> str:
        """As a stylesheet ``rgba(...)`` value."""
        return with_alpha(self.colour, self.opacity)


@dataclass(frozen=True)
class Palette:
    """Every colour role of one theme. Colours are ``#RRGGBB``.

    The fields are the language's colour tokens, named as in the token
    files (``text-secondary`` is ``text_secondary``; WireTrace's own
    roles drop their ``wiretrace.`` prefix). A token without a field, or a
    field without a token, stops the module from loading.
    """

    # Surfaces
    paper: str              # window, console, fields
    surface: str            # toolbars, status bar, dialog footers
    divider: str            # lines between areas; hover fill
    edge: str               # boundary of fields, buttons, check boxes: 3:1

    # Text
    text: str
    text_secondary: str
    text_tertiary: str      # meta text: hints, counts
    muted: str              # disabled text and decorative marks only

    # Accent
    accent: str             # fills and large marks
    accent_strong: str      # links, focus, thin active marks
    accent_hover: str

    # A filled (primary) button's states, and selected text
    primary_hover: str
    primary_pressed: str
    selection: str

    # Dot grid, and its dots lit near the pointer
    dot: Tint
    dot_glow: Tint

    # Severity, readable as text on paper, surface and the row tints
    error: str
    warning: str
    info: str
    command: str
    success: str
    error_row: str
    warning_row: str

    # Status marks (dots, not text)
    connected: str
    logging: str

    # Search: every match. The current match is drawn like a filled button.
    search_hit: str

    # Plot traces, in order
    plot_traces: tuple[str, ...]

    def colours(self) -> dict[str, str]:
        """Every role by name, as the stylesheet writes it."""
        result: dict[str, str] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if isinstance(value, str):
                result[item.name] = value
            elif isinstance(value, Tint):
                result[item.name] = value.rgba
        return result


_OWN_PREFIX = "wiretrace."
_PLOT_PREFIX = "wiretrace.plot."
_OPAQUE = 1


def _role(token: str) -> str:
    return token.removeprefix(_OWN_PREFIX).replace("-", "_")


def _palette(theme: ThemeID) -> Palette:
    """The palette of ``theme``, built from its colour tokens."""
    roles: dict[str, Any] = {}
    traces: dict[int, str] = {}
    for token, (colour, opacity) in design_tokens.COLOURS[theme.value].items():
        if token.startswith(_PLOT_PREFIX):
            if opacity != _OPAQUE:
                raise ValueError(f"the plot trace {token} must be opaque")
            traces[int(token.removeprefix(_PLOT_PREFIX))] = colour
        elif opacity == _OPAQUE:
            roles[_role(token)] = colour
        else:
            roles[_role(token)] = Tint(colour, opacity)
    if sorted(traces) != list(range(1, len(traces) + 1)):
        raise ValueError(f"the plot traces of {theme.value} are not numbered 1 to {len(traces)}")
    return Palette(**roles, plot_traces=tuple(traces[n] for n in sorted(traces)))


PALETTES: dict[ThemeID, Palette] = {theme: _palette(theme) for theme in ThemeID}
LIGHT = PALETTES[ThemeID.LIGHT]
DARK = PALETTES[ThemeID.DARK]


# ── Sizes ────────────────────────────────────────────────────────────────


def _measure(token: str) -> Any:
    """A field whose value is the token ``token``, in logical pixels."""
    return field(default=design_tokens.MEASURES[token], metadata={"token": token})


def _count(token: str) -> Any:
    """A field whose value is the number token ``token``."""
    return field(default=design_tokens.NUMBERS[token], metadata={"token": token})


@dataclass(frozen=True)
class Metrics:
    """Sizes in logical pixels. They follow the system's display scaling.

    Every field is a token of the language; ``content_sizes`` gives the
    sizes the stylesheet needs that are composed from them.
    """

    # Space, on the 4-pixel grid
    space_half: int = _measure("space.half")   # two lines of text that belong together
    space_1: int = _measure("space.1")
    space_2: int = _measure("space.2")
    space_3: int = _measure("space.3")
    space_4: int = _measure("space.4")
    space_6: int = _measure("space.6")

    # Lines and corners
    border: int = _measure("border.hairline")  # every outline and divider
    mark: int = _measure("border.mark")        # "this one": focus, active tab, selection bar
    radius_xs: int = _measure("radius.xs")     # progress bars
    radius_sm: int = _measure("radius.sm")     # controls
    radius_md: int = _measure("radius.md")     # things that float: menus, lists, toasts

    # Controls
    control_height: int = _measure("size.control.md")
    control_height_compact: int = _measure("size.control.sm")
    control_padding: int = _measure("size.control-padding.md")
    control_padding_compact: int = _measure("size.control-padding.sm")
    button_min_width: int = _measure("size.control-min-width.md")
    button_min_width_compact: int = _measure("size.control-min-width.sm")
    icon: int = _measure("size.icon.sm")         # inside controls, rows and menus
    indicator: int = _measure("size.indicator")  # check boxes and radio buttons
    target_min: int = _measure("size.target-min")  # the least a clickable target is
    status_dot: int = _measure("size.status-dot")
    scrollbar: int = _measure("size.scrollbar")
    scrollbar_handle_min: int = _measure("size.scrollbar-handle-min")
    progress_height: int = _measure("size.progress")
    splitter_handle: int = _measure("size.splitter")

    # Dot grid
    grid_pitch: int = _measure("grid.pitch")
    grid_dot_radius: int = _measure("grid.dot")
    grid_glow_radius: int = _measure("grid.glow-radius")  # dots this near the pointer light up

    # Rows of the main window
    tab_bar_height: int = _measure("wiretrace.region.tab-bar")
    toolbar_height: int = _measure("wiretrace.region.toolbar")
    header_height: int = _measure("wiretrace.region.panel-header")
    status_height: int = _measure("wiretrace.region.status-bar")
    row_height: int = _measure("wiretrace.region.row")
    port_row_height: int = _measure("wiretrace.region.port-row")
    port_rows_visible: int = _count("wiretrace.port-rows-visible")  # more ports than this scroll

    # Floors: the least a window may be; it grows to fit its content
    window_min_width: int = _measure("wiretrace.floor.window.width")
    window_min_height: int = _measure("wiretrace.floor.window.height")
    pane_min_height: int = _measure("wiretrace.floor.pane-height")
    dialog_prompt_width: int = _measure("wiretrace.floor.dialog-prompt-width")
    dialog_width: int = _measure("wiretrace.floor.dialog-form-width")
    dialog_medium_width: int = _measure("wiretrace.floor.dialog-medium-width")
    dialog_wide_width: int = _measure("wiretrace.floor.dialog-wide-width")
    dialog_short_height: int = _measure("wiretrace.floor.dialog-short-height")
    dialog_tall_height: int = _measure("wiretrace.floor.dialog-tall-height")
    dialog_wide_height: int = _measure("wiretrace.floor.dialog-wide-height")
    toast_min_width: int = _measure("wiretrace.floor.toast-width")
    plot_window_min_width: int = _measure("wiretrace.floor.plot-window.width")
    plot_window_min_height: int = _measure("wiretrace.floor.plot-window.height")

    # Widths of fields and columns that should not stretch
    toast_max_width: int = _measure("wiretrace.width.toast-max")
    port_field_width: int = _measure("wiretrace.width.port-field")
    port_field_min_width: int = _measure("wiretrace.width.port-field-min")
    baud_field_width: int = _measure("wiretrace.width.baud-field")
    filter_field_width: int = _measure("wiretrace.width.filter-field")
    filter_field_min_width: int = _measure("wiretrace.width.filter-field-min")
    tab_min_width: int = _measure("wiretrace.width.tab-min")
    line_ending_field_width: int = _measure("wiretrace.width.line-ending-field")
    page_column_width: int = _measure("wiretrace.width.page-column")
    short_label_width: int = _measure("wiretrace.width.short-label")  # "Mode:", "Profile:"
    plot_values_width: int = _measure("wiretrace.width.plot-values")
    plot_swatch_width: int = _measure("wiretrace.width.plot-swatch")
    plot_axis_width: int = _measure("wiretrace.width.plot-axis")  # stacked panels line up
    sidebar_width: int = _measure("wiretrace.width.sidebar")
    form_label_width: int = _measure("wiretrace.width.form-label")
    form_field_width: int = _measure("wiretrace.width.form-field")
    number_field_width: int = _measure("wiretrace.width.number-field")

    # Heights of content areas
    sample_height: int = _measure("wiretrace.height.sample")
    notes_height: int = _measure("wiretrace.height.notes")

    # Windows, logo and splash
    plot_window_width: int = _measure("wiretrace.plot-window.width")
    plot_window_height: int = _measure("wiretrace.plot-window.height")
    logo_large: int = _measure("wiretrace.logo.large")
    logo_medium: int = _measure("wiretrace.logo.medium")
    splash_width: int = _measure("wiretrace.splash.width")
    splash_height: int = _measure("wiretrace.splash.height")

    def content_sizes(self) -> dict[str, int]:
        """Sizes the stylesheet needs, composed from the tokens in one place.

        A stylesheet sizes what is inside a box's outline and padding,
        so the outer sizes of the language are reduced by them here.
        """
        outline = 2 * self.border
        focus_outline = 2 * self.mark
        wider = self.mark - self.border
        ui_line = TYPE_STYLES["ui"].line_height
        return {
            # Inside a control's 1-pixel outline.
            "control_content_height": self.control_height - outline,
            "control_content_height_compact": self.control_height_compact - outline,
            # Inside the 2-pixel outline of a focused control: the box keeps
            # its size, so nothing moves when the focus arrives.
            "control_focus_height": self.control_height - focus_outline,
            "control_focus_height_compact": self.control_height_compact - focus_outline,
            "control_padding_focus": self.control_padding - wider,
            "control_padding_compact_focus": self.control_padding_compact - wider,
            "field_padding": self.space_2,
            "field_padding_focus": self.space_2 - wider,
            # A tab is the tab bar's height, less its 2-pixel marker line.
            "tab_content_height": self.tab_bar_height - self.mark,
            # A dense row: one ``ui`` line, centred in the row's height.
            "row_padding": (self.row_height - ui_line) // 2,
            # Inside a focused indicator's 2-pixel ring: it keeps its size.
            "indicator_focus": self.indicator - 2 * wider,
            # A radio button's indicator, with its outline, as a circle.
            "indicator_round": self.indicator // 2 + self.border,
            # A scroll bar's handle is a pill.
            "scrollbar_round": self.scrollbar // 2,
            # A menu holds its rows inset by one step, concentric with its corners.
            "menu_inset": self.radius_md - self.radius_sm,
        }


METRICS = Metrics()


#: How brightly the dot grid is lit around the pointer: (share of the glow
#: radius, strength from 0 to 1), from the pointer outwards. The same as
#: the website's.
GLOW_STOPS: tuple[tuple[float, float], ...] = (
    (0.0, 1.0),
    (design_tokens.NUMBERS["grid.glow-middle"], design_tokens.NUMBERS["grid.glow-middle-strength"]),
    (design_tokens.NUMBERS["grid.glow-end"], 0.0),
)


# ── Type ─────────────────────────────────────────────────────────────────

#: Family names the operating system's font matching knows as generic
#: names; Qt finds them by style hint instead, so they are not listed.
_GENERIC_FAMILIES = frozenset({"sans-serif", "serif", "monospace"})


@dataclass(frozen=True)
class TypeStyle:
    """One step of the type scale. Sizes are in logical pixels."""

    family: str              # "sans", "serif" or "mono"
    size: int
    line_height: int
    weight: int
    letter_spacing: float


def _families(role: str) -> tuple[str, ...]:
    return tuple(f for f in design_tokens.FONT_FAMILIES[role] if f not in _GENERIC_FAMILIES)


TYPE_STYLES: dict[str, TypeStyle] = {
    name: TypeStyle(*style) for name, style in design_tokens.TYPE_STYLES.items()
}


@dataclass(frozen=True)
class Typography:
    """Font families and weights.

    Interface text is Poppins, shipped with the application, at the
    sizes of the type scale: the same on every computer. The console is
    JetBrains Mono, also shipped, at a size the user chooses.
    """

    interface_families: tuple[str, ...] = _families("sans")
    console_families: tuple[str, ...] = _families("mono")
    regular: int = design_tokens.FONT_WEIGHTS["regular"]
    medium: int = design_tokens.FONT_WEIGHTS["medium"]

    @property
    def interface_family(self) -> str:
        return self.interface_families[0]

    @property
    def console_family(self) -> str:
        return self.console_families[0]

    @property
    def console_fallbacks(self) -> tuple[str, ...]:
        return self.console_families[1:]


TYPOGRAPHY = Typography()


# ── Console tags ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TagStyle:
    """How the console draws lines with a given tag."""

    label: str              # shown in the level column
    label_role: str         # palette role of the label
    text_role: str          # palette role of the message
    row_role: str | None    # palette role of the row tint, if any


TAG_STYLES: dict[str, TagStyle] = {
    "CRITICAL": TagStyle("CRT", "error", "error", "error_row"),
    "ERROR": TagStyle("ERR", "error", "error", "error_row"),
    "WARNING": TagStyle("WRN", "warning", "warning", "warning_row"),
    "INFO": TagStyle("INF", "info", "text", None),
    "DEBUG": TagStyle("DBG", "text_secondary", "text_secondary", None),
    # A command is echoed with its own ">>>" marker, so it needs no label.
    "COMMAND": TagStyle("", "command", "command", None),
    "DATA": TagStyle("", "text", "text", None),
}

_PLAIN_TAG_STYLE = TAG_STYLES["DATA"]


def tag_style(tag: str) -> TagStyle:
    """Style for a tag; an unknown tag is drawn as plain data."""
    return TAG_STYLES.get(tag, _PLAIN_TAG_STYLE)


# ── Active palette ───────────────────────────────────────────────────────────

_active_theme: ThemeID = ThemeID.LIGHT


def set_active_theme(theme: ThemeID) -> None:
    """Record the theme in use. Called by the theme manager only."""
    global _active_theme
    if theme not in PALETTES:
        raise ValueError(f"no palette for theme {theme!r}")
    _active_theme = theme


def active_theme() -> ThemeID:
    return _active_theme


def palette() -> Palette:
    """The palette of the theme in use."""
    return PALETTES[_active_theme]
