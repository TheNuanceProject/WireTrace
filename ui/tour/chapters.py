# SPDX-License-Identifier: GPL-3.0-or-later
"""What the tour says, chapter by chapter.

Every step points at one control (a ``Target``), or at nothing, and says
in a sentence or two what it does, in the words of the user guide. Each
step says where its control is shown (a ``Place``): on the window, on
the plot, in its menu, opened, on a page of Preferences, opened, or on
the session report, opened. A step about a feature that can be switched
off says which (``feature``); while that feature is off, the tour shows
its switch instead (``switched_off``), or, for a page of Preferences,
the page with a word on how to add it to the list (``page_hidden``).

Keys are written as ``{key:name}`` and figures as ``{const:NAME}``,
filled from the shortcut table and the constants, so the tour never
names a key or a figure that is not the real one.

The chapters after the first need an open session: the tour shows them
on the built-in sample board (``core/sample_device.py``).
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass

import app.constants as constants
from app.modules import ALERTS, HIGHLIGHTS, REPORT, RESTARTS, ModuleSpec
from app.modules import PLOT as PLOT_FEATURE
from core.records import COMMAND_PREFIX
from ui import shortcuts
from ui.dialogs import preference_pages as pages
from ui.tour.targets import FEATURE_TARGETS, Target
from version import APP_NAME


class Place(enum.Enum):
    """Where a step's control is shown."""

    #: On the window: a tab, the menu bar or the status bar.
    WINDOW = "window"
    #: On the plot, which the tour opens.
    PLOT = "plot"
    #: In the menu that holds the entry, which the tour opens.
    MENU = "menu"
    #: On a page of Preferences, which the tour opens.
    PREFERENCES = "preferences"
    #: On the session report, which the tour opens.
    REPORT = "report"


@dataclass(frozen=True)
class Step:
    """One card of the tour."""

    title: str
    text: str
    #: What the step points at; several targets are pointed at together.
    target: Target | tuple[Target, ...] | None = None
    place: Place = Place.WINDOW
    #: The page of Preferences, for a step there.
    page: str = ""
    #: The feature the step is about. While it is switched off, a step in
    #: Preferences shows its page all the same and says how to add it to
    #: the list (``page_hidden``); any other step gives way to the
    #: feature's switch (``switched_off``).
    feature: ModuleSpec | None = None

    def __post_init__(self) -> None:
        if (self.place is Place.PREFERENCES) != (self.page in pages.PAGES):
            raise ValueError(f"{self.title}: a page is given for, and only for, a step in Preferences")
        if self.place is Place.MENU and not isinstance(self.target, Target):
            raise ValueError(f"{self.title}: a step in a menu points at one entry")


@dataclass(frozen=True)
class Chapter:
    """Steps about one part of WireTrace, taken in order."""

    id: str
    title: str
    summary: str
    steps: tuple[Step, ...]
    #: Whether the chapter is shown on the sample board's session.
    session: bool = True


def page_hidden(feature: ModuleSpec) -> str:
    """Said on a page of Preferences that is not in its list while
    ``feature`` is switched off."""
    return f"Switching on {feature.title}, on the Features page, adds this page to the list."


def switched_off(feature: ModuleSpec) -> Step:
    """Shown in place of the steps about ``feature`` while it is switched off."""
    return Step(
        "This feature is switched off",
        f"Switch on {feature.title} here, on the Features page of Preferences, then take this "
        "part of the tour again.",
        FEATURE_TARGETS[feature.id], Place.PREFERENCES, pages.FEATURES,
    )


#: Shown when the tour opens the sample board's tab, before the first
#: step on it.
SAMPLE_SESSION = Step(
    "A sample session",
    f"This tab reads a sample board built into {APP_NAME}, so every tool can be shown with no "
    "board plugged in. Nothing about it is kept, and it closes when the tour ends.",
)

CONNECTING = Chapter(
    "connecting", "Connecting a board",
    "The list of ports, the baud rate, port settings and tabs.",
    (
        Step(
            "Your ports",
            "Every serial port is listed, and the list updates by itself: plug a board in and "
            "it appears, marked New. A port you have used says what it was last used with.",
            Target.PORTS,
        ),
        Step(
            "Baud rate",
            "It must match the rate the board uses. Choose one from the list, or type any rate.",
            Target.BAUD,
        ),
        Step(
            "Port settings",
            "Most boards use 8N1 with no flow control, where every port starts. Change data bits, "
            "parity, stop bits, flow control and the lines set at open here; each port remembers "
            "its own.",
            Target.PORT_SETTINGS,
        ),
        Step(
            "Connect",
            "Opens the port in this tab. Double-click a port, or press Enter on it, to connect at once.",
            Target.CONNECT,
        ),
        Step(
            "One tab per board",
            "Each tab has its own connection, console, log and plot. {key:new_tab} opens another; "
            "{key:rename_tab}, or a double-click on the tab, gives it a name.",
            Target.NEW_TAB,
        ),
    ),
    session=False,
)

READING = Chapter(
    "reading", "Reading the console",
    "Levels, the filter, Find, timestamps, Following, Text and HEX, copying.",
    (
        Step(
            "Levels",
            "Each line gets a level from what it says: Error, Warning, Info, Debug or Data. "
            "The counts show how many; choose one to see only those lines.",
            Target.LEVELS,
        ),
        Step(
            "Filter lines",
            "Type here, or press {key:filter}, to show only the lines that hold the text. "
            "Lines that are hidden are still logged and exported.",
            Target.FILTER,
        ),
        Step(
            "Find",
            "Highlights every match and leaves every line in place. {key:find_next} and "
            "{key:find_previous} move from one match to the next.",
            Target.FIND,
        ),
        Step(
            "Timestamps",
            "Each line carries the time it arrived. Show the time of day, the date and time, or "
            "the time since the line before. Logs always hold the full date and time.",
            Target.TIMESTAMPS,
        ),
        Step(
            "Following",
            "The console stays on the newest line. Scroll up to read and it holds still; "
            "choose this to return to the newest line.",
            Target.FOLLOWING,
        ),
        Step(
            "Text and HEX",
            "Show the lines as text, or the bytes themselves in hexadecimal: for a binary "
            "protocol, or to see exactly which bytes end a line.",
            Target.TEXT_HEX,
        ),
        Step(
            "The console",
            "Select text and press {key:copy} to copy it with its times. Right-click for Find, "
            "Show only lines with the selected text, and Export selection. {key:text_larger} and "
            "{key:text_smaller} change the text size.",
            Target.CONSOLE,
        ),
    ),
)

LOGGING = Chapter(
    "logging", "Logs and exports",
    "Starting and pausing a log, where logs go, and exporting a session.",
    (
        Step(
            "Start a log",
            "From the moment you start, every line received is written to a plain text file, "
            "with a CSV file beside it if you choose. You give it a name and a folder. "
            "{key:start_log} starts one too.",
            Target.START_LOG,
        ),
        Step(
            "Pause",
            "Stops writing for a while: lines are still received and shown. The log notes the "
            "gap, and Resume carries on in the same file.",
            Target.PAUSE_LOG,
        ),
        Step(
            "The log in the status bar",
            "While a log runs, the status bar shows its file name, how long it has run and how "
            "large it is.",
            Target.LOG_STATE,
        ),
        Step(
            "Export",
            "No need to start a log first: Export saves everything received since you connected, "
            "as Text, CSV or both, even after the board is gone. {key:export} does the same.",
            Target.EXPORT,
        ),
        Step(
            "Your log files",
            "Opens the folder your logs are written to. A new log is offered the folder of the "
            "log before it.",
            Target.MENU_LOG_FOLDER, Place.MENU,
        ),
        Step(
            "Log settings",
            "Where new logs are written, Text or Text and CSV, and the size at which a long log "
            "carries on in a new file: {const:LOG_SPLIT_DEFAULT_MB} MB to start with. A line is "
            "never cut in two.",
            Target.LOG_SETTINGS, Place.PREFERENCES, pages.LOGGING,
        ),
    ),
)

COMMANDS = Chapter(
    "commands", "Sending commands",
    "The command box, the line ending, saved commands and the control lines.",
    (
        Step(
            "Send a command",
            f"Type here and press Enter. The command appears in the console and the log after "
            f"{COMMAND_PREFIX}, and the arrow at the end lists the commands sent in this session.",
            Target.COMMAND,
        ),
        Step(
            "Line ending",
            "What is sent after the command: None, LF, CR or CR LF. If a board does not react, "
            "check this first.",
            Target.LINE_ENDING,
        ),
        Step(
            "Saved commands",
            "Keep the commands you send often. Choosing one puts it in the box, to change before "
            "sending. The first {const:SAVED_COMMAND_SHORTCUTS} have shortcuts, from "
            "{key:saved_command_1}.",
            Target.SAVED_COMMANDS,
        ),
        Step(
            "Control lines",
            "Set DTR and RTS, or send a break. Many boards reset when DTR or RTS changes; "
            "each change is noted in the session.",
            Target.CONTROL_LINES,
        ),
        Step(
            "Input lines",
            "CTS, DSR, DCD and RI, as the port reports them: a line that is on is drawn brighter.",
            Target.INPUT_LINES,
        ),
    ),
)

PLOT = Chapter(
    "plot", "The plot",
    "Values, reading the chart, Measure, the time shown, Configure, saving.",
    (
        Step(
            "Open the plot",
            "Draws the numbers in the lines as they arrive, in a window of its own. "
            "{key:plot} opens and closes it.",
            Target.PLOT, feature=PLOT_FEATURE,
        ),
        Step(
            "Values",
            "Each value found, with its latest figure and the least, greatest and average of "
            "what is shown. Untick one to hide its trace.",
            Target.PLOT_VALUES, Place.PLOT, feature=PLOT_FEATURE,
        ),
        Step(
            "Reading the chart",
            "Point at it to read every value at that moment. Scroll to show less or more time; "
            "drag to look back.",
            Target.PLOT_CHART, Place.PLOT, feature=PLOT_FEATURE,
        ),
        Step(
            "One chart or panels",
            "Panels by scale keep a small value from being flattened by a large one.",
            Target.PLOT_LAYOUT, Place.PLOT, feature=PLOT_FEATURE,
        ),
        Step(
            "Measure",
            "Two cursors, A and B: the time between them and how much each value changed.",
            Target.PLOT_MEASURE, Place.PLOT, feature=PLOT_FEATURE,
        ),
        Step(
            "Time shown",
            "How much recent time is on screen. Nothing is discarded by changing it.",
            Target.PLOT_TIME, Place.PLOT, feature=PLOT_FEATURE,
        ),
        Step(
            "Pause and Fit",
            "Pause holds the picture still while values keep being collected. Fit undoes any "
            "zoom and shows the newest data.",
            Target.PLOT_HOLD, Place.PLOT, feature=PLOT_FEATURE,
        ),
        Step(
            "Configure",
            "Needed only when values are not found by themselves: capture a pattern from a "
            "line, and keep it as a profile.",
            Target.PLOT_CONFIGURE, Place.PLOT, feature=PLOT_FEATURE,
        ),
        Step(
            "Save and pop out",
            "Save the chart as PNG or SVG, or the values as CSV. Dock puts the plot under the "
            "console; Pop out gives it a window of its own again.",
            Target.PLOT_SAVE, Place.PLOT, feature=PLOT_FEATURE,
        ),
    ),
)

WATCHING = Chapter(
    "watching", "Watching the board",
    "Reconnecting, restarts and the session report.",
    (
        Step(
            "A board that comes back",
            "If a board is unplugged or resets its USB connection, the tab waits for the same "
            "board and carries on in the same log. This switches it on or off for the tab in "
            "front only.",
            Target.MENU_RECONNECT, Place.MENU,
        ),
        Step(
            "Reconnect settings",
            "Whether a tab waits for a lost board, how long it waits, and whether a port with no "
            "USB identity is taken back by its name. Another board is never taken in its place.",
            Target.RECONNECT_SETTINGS, Place.PREFERENCES, pages.CONNECTION,
        ),
        Step(
            "Restarts",
            "When an ESP32 or Zephyr board restarts, a notice follows its boot line, in the "
            "console, the log and exports. The sample board's first line was one.",
            Target.CONSOLE, feature=RESTARTS,
        ),
        Step(
            "Session report",
            "The report of the session in front, while it runs or after it ends.",
            Target.MENU_SESSION_REPORT, Place.MENU, feature=REPORT,
        ),
        Step(
            "The report",
            "This is the sample session's: running time, lines, errors and warnings, each lost "
            "connection and each restart. Copy puts it on the clipboard; Save writes it to a "
            "text file.",
            Target.REPORT_ACTIONS, Place.REPORT, feature=REPORT,
        ),
    ),
)

ALERTS_AND_HIGHLIGHTS = Chapter(
    "alerts", "Alerts and highlights",
    "Being told what happened, and making lines stand out.",
    (
        Step(
            "Alerts",
            "They tell you when an error line arrives, the connection is lost, or the board goes "
            "quiet. Switch them on here, on the Features page of Preferences.",
            Target.FEATURE_ALERTS, Place.PREFERENCES, pages.FEATURES,
        ),
        Step(
            "What alerts tell you",
            "Choose what to be told about, and whether a sound or a desktop notification comes "
            "too. A notification names the tab and what happened, never the text of a line.",
            Target.ALERT_SETTINGS, Place.PREFERENCES, pages.ALERTS, feature=ALERTS,
        ),
        Step(
            "Alerts for one tab",
            "Switches alerts on or off for the tab in front only, whatever the Features page "
            "says. Each tab follows Preferences until it is switched this way.",
            Target.MENU_ALERTS, Place.MENU,
        ),
        Step(
            "Highlight rules",
            "A text to look for and a colour: a line that holds the text is drawn in that "
            "colour, with a mark beside it. On screen only; logs keep every line as received.",
            Target.HIGHLIGHT_RULES, Place.PREFERENCES, pages.HIGHLIGHTS, feature=HIGHLIGHTS,
        ),
        Step(
            "ANSI colours",
            "Shows the colours firmware sets with ANSI codes. The codes are always removed from "
            "what is shown, logged and exported.",
            Target.FEATURE_ANSI, Place.PREFERENCES, pages.FEATURES,
        ),
    ),
)

SETTINGS = Chapter(
    "settings", "Settings, updates and help",
    "Preferences, features, the theme, updates, and the Help menu.",
    (
        Step(
            "Preferences",
            "Holds every setting. {key:preferences} opens it too.",
            Target.MENU_PREFERENCES, Place.MENU,
        ),
        Step(
            "One page per subject",
            "Choose a page here. Each setting says what it does; changes take effect when you "
            "choose Save.",
            Target.PREFERENCE_PAGES, Place.PREFERENCES, pages.APPEARANCE,
        ),
        Step(
            "Features",
            "Switch off what you do not use. A feature that is off is not loaded, takes no "
            "memory and adds nothing to the window.",
            tuple(FEATURE_TARGETS.values()), Place.PREFERENCES, pages.FEATURES,
        ),
        Step(
            "Theme",
            "Follow the system, or stay light or dark.",
            Target.MENU_THEME, Place.MENU,
        ),
        Step(
            "Updates",
            "When a new version is ready, a button at the right of the status bar offers it "
            "until you install it, skip it or put it off.",
            Target.STATUS_BAR,
        ),
        Step(
            "Help",
            "The user guide ({key:user_guide}), the keyboard shortcuts, checking for updates, "
            "returning to the previous version, and the diagnostic logs. This tour is here too.",
            Target.MENU_HELP, Place.MENU,
        ),
    ),
)

#: The whole tour, in order.
CHAPTERS: tuple[Chapter, ...] = (
    CONNECTING, READING, LOGGING, COMMANDS, PLOT, WATCHING, ALERTS_AND_HIGHLIGHTS, SETTINGS,
)

#: What each release adds, shown as a chapter of its own, by its release
#: line (``release_line``), so a release that only fixes keeps the chapter.
WHATS_NEW: dict[str, Chapter] = {
    "2.2": Chapter(
        "whats-new-2.2", "What's new in 2.2",
        "Updates you will not miss, returning to the previous version, the diagnostic logs.",
        (
            Step(
                "Updates you will not miss",
                "When a new version is ready, a button at the right of the status bar offers it "
                "until you install it, skip it or put it off.",
                Target.STATUS_BAR,
            ),
            Step(
                "Return to the previous version",
                "If an update does not suit you, this returns you to the version before, "
                "downloaded and verified as an update is.",
                Target.MENU_RETURN, Place.MENU,
            ),
            Step(
                "Diagnostic logs",
                f"{APP_NAME} keeps a log of its own events on this computer. This opens its "
                "folder; Copy diagnostic summary, below it, copies a summary to include in a report.",
                Target.MENU_DIAGNOSTIC_LOGS, Place.MENU,
            ),
        ),
    ),
}

_PLACEHOLDER = re.compile(r"\{(key|const):([A-Za-z0-9_]+)\}")


def words(text: str) -> str:
    """``text`` with its keys and figures filled in."""

    def fill(match: re.Match[str]) -> str:
        kind, name = match.groups()
        if kind == "key":
            return shortcuts.native_keys(name)
        return str(getattr(constants, name))

    return _PLACEHOLDER.sub(fill, text)


def release_line(version: str) -> str:
    """The release line of ``version``, its major and minor numbers:
    "2.2" for 2.2.0 and 2.2.1."""
    return ".".join(version.split(".")[:2])


def line_order(version: str) -> tuple[int, ...]:
    """The release line of ``version`` as numbers, to compare lines: (2, 2)
    for 2.2.1; a version that is not written in numbers comes first."""
    try:
        return tuple(int(part) for part in release_line(version).split("."))
    except ValueError:
        return ()


def whats_new(version: str) -> Chapter | None:
    """The chapter of what the release line of ``version`` adds, if any."""
    return WHATS_NEW.get(release_line(version))
