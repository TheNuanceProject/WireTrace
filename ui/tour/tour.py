# SPDX-License-Identifier: GPL-3.0-or-later
"""The tour: takes the user through chapters one step at a time.

The first chapter is shown on a tab's first screen; the others on a
session, the built-in sample board's (``core/sample_device.py``), which
the tour opens in a tab of its own and closes when it ends. A step is
shown where its control is (``Place``): the tour opens the plot, the
menu that holds an entry with the entry chosen, a page of Preferences,
or the session report, as the user would, and closes it again when the
step is left. Preferences opened by the tour change nothing: the tour
takes every click and key while it is shown, and closes it as Cancel
does. Whatever the tour opened, it closes; it leaves the user's own
tabs as they were.

The screen stays dimmed from the first card to the last: over the
window the card is on, and over the main window behind a window the
tour opened in front of it.

A chapter is counted as taken once its last step has been reached; the
chapters taken are kept in the preferences, for the list in Help.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QPoint, QRect, QTimer, Signal
from PySide6.QtWidgets import QScrollArea

from app.config import ConfigManager
from app.modules import ModuleSettings
from app.system_motion import reduced_motion
from ui.themes import tokens
from ui.tour.chapters import SAMPLE_SESSION, Chapter, Place, Step, page_hidden, switched_off, words
from ui.tour.menu_opener import MenuOpener, opens_in_window, where_words
from ui.tour.overlay import Backdrop, CardAction, Locator, TourOverlay
from ui.tour.targets import Target, find, find_action

if TYPE_CHECKING:
    from PySide6.QtWidgets import QDialog, QWidget

    from ui.device_tab import DeviceTab
    from ui.dialogs.preferences_dialog import PreferencesDialog
    from ui.dialogs.session_report_dialog import SessionReportDialog
    from ui.main_window import MainWindow

logger = logging.getLogger(__name__)

#: The buttons of a tab's toolbar, which move into its More menu when the
#: window is too narrow for them.
TOOLBAR_TARGETS = frozenset({
    Target.START_LOG, Target.PAUSE_LOG, Target.CLEAR, Target.EXPORT, Target.PLOT,
    Target.CONTROL_LINES, Target.TEXT_HEX, Target.FIND,
})
#: Said when a step's button is in the More menu.
IN_MORE_MENU = "In a window this narrow, it is in the More menu at the right end of the toolbar."


@dataclass(frozen=True)
class _Card:
    """A step as it is shown: in which chapter, and where in it."""

    chapter: Chapter
    step: Step
    number: int
    of: int


def _targets(target: Target | tuple[Target, ...] | None) -> tuple[Target, ...]:
    if target is None:
        return ()
    return target if isinstance(target, tuple) else (target,)


def _shown_part(widget: QWidget, host: QWidget) -> QRect:
    """The part of ``widget`` not cut off by the widgets it is in (a page
    that scrolls), in ``host``'s coordinates."""
    part = QRect(widget.mapTo(host, QPoint(0, 0)), widget.size())
    holder = widget.parentWidget()
    while holder is not None and holder is not host:
        part = part.intersected(QRect(holder.mapTo(host, QPoint(0, 0)), holder.size()))
        holder = holder.parentWidget()
    return part


def _bring_into_view(widgets: list[QWidget]) -> None:
    """Scroll whatever ``widgets`` are in so that they are shown: the
    first of them at least, when they do not fit together."""
    margin = tokens.METRICS.space_4
    for widget in (*reversed(widgets[1:]), *widgets[:1]):
        holder = widget.parentWidget()
        while holder is not None:
            if isinstance(holder, QScrollArea) and holder.widget() is not None and holder.widget().isAncestorOf(widget):
                holder.ensureWidgetVisible(widget, margin, margin)
            holder = holder.parentWidget()


class Tour(QObject):
    """One run of the tour over ``chapters``.

    Signals:
        finished():  The tour has ended and put away what it opened.
    """

    finished = Signal()

    def __init__(self, window: MainWindow, config: ConfigManager, chapters: Sequence[Chapter]) -> None:
        super().__init__(window)
        self._window = window
        self._config = config
        self._chapters = tuple(chapters)
        self._cards: list[_Card] = []
        self._index = -1
        self._overlay: TourOverlay | None = None
        #: The window dimmed while the card is over a window in front of it.
        self._backdrop: Backdrop | None = None
        self._reduced_motion = reduced_motion()
        self._users_tab: DeviceTab | None = None
        #: The tab the first chapter is shown on, and whether the tour
        #: opened it.
        self._first_screen_tab: DeviceTab | None = None
        self._opened_first_screen = False
        self._sample_tab: DeviceTab | None = None
        self._opened_plot = False
        #: What the tour has open over the window for the step shown.
        self._menu: MenuOpener | None = None
        self._preferences: PreferencesDialog | None = None
        self._report: SessionReportDialog | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Show the first step."""
        self._running = True
        self._users_tab = self._window.current_tab()
        self._cards = self._lay_out()
        self._index = -1
        self._go(1)

    def stop(self) -> None:
        """End the tour where it is and put away what it opened."""
        if not self._running:
            return
        self._running = False
        self._close_menu()
        self._close_preferences()
        self._close_report()
        if self._overlay is not None:
            self._overlay.close_overlay()
            self._overlay = None
        self._set_backdrop(on=False)
        sample = self._sample_tab
        if sample is not None and self._opened_plot and sample.is_plot_visible():
            sample.toggle_plot()
        self._close_tab(sample)
        first = self._first_screen_tab
        if self._opened_first_screen and first is not None and not first.has_session:
            self._close_tab(first)
        self._sample_tab = self._first_screen_tab = None
        if self._users_tab is not None and self._users_tab in self._window.tabs():
            self._window.show_tab(self._users_tab)
        self.finished.emit()

    # ── Steps ────────────────────────────────────────────────────────────

    def _lay_out(self) -> list[_Card]:
        """The cards of the chapters. Outside Preferences, the steps about a
        feature switched off are shown as one card at its switch."""
        cards: list[_Card] = []
        for chapter in self._chapters:
            steps: list[Step] = []
            for step in chapter.steps:
                if self._is_off(step) and step.place is not Place.PREFERENCES:
                    step = switched_off(step.feature)
                    if steps and steps[-1] == step:
                        continue
                steps.append(step)
            for number, step in enumerate(steps, 1):
                cards.append(_Card(chapter, step, number, len(steps)))
        return cards

    def _is_off(self, step: Step) -> bool:
        """Whether the feature ``step`` is about is switched off."""
        return step.feature is not None and not ModuleSettings(self._config).is_enabled(step.feature)

    def _go(self, change: int) -> None:
        index = self._index + change
        if index < 0 or not self._running:
            return
        if index >= len(self._cards):
            self.stop()
            return
        self._index = index
        card = self._cards[index]
        if card.number == card.of:
            self._note_taken(card.chapter)
        self._close_menu()
        if card.chapter.session:
            self._show_sample()
        else:
            self._show_first_screen()
        if card.chapter.session and not self._sample_tab.is_connected:
            # The sample board opens on the reader's thread; the step is
            # shown once it has, and until then no card can be answered.
            # The window stays dimmed meanwhile.
            self._close_preferences()
            self._close_report()
            if self._overlay is not None:
                self._overlay.close_overlay()
                self._overlay = None
            self._set_backdrop(on=True)
            self._sample_tab.connection_changed.connect(self._show_when_connected)
            return
        self._show(card)

    def _show_when_connected(self, connected: bool) -> None:
        tab = self._sample_tab
        if tab is None or not connected:
            return
        tab.connection_changed.disconnect(self._show_when_connected)
        # The first card on the sample session says what it is.
        overlay = self._overlay_on(self._window)
        overlay.show_step(SAMPLE_SESSION.title, words(SAMPLE_SESSION.text), "", [
            CardAction("Close tour", self.stop, leading=True),
            CardAction("Next", lambda: self._show(self._cards[self._index]), primary=True),
        ], lambda: None)

    def _show(self, card: _Card) -> None:
        step = card.step
        tab = self._sample_tab if card.chapter.session else self._first_screen_tab
        place = step.place
        self._close_menu()
        if place is not Place.PREFERENCES:
            self._close_preferences()
        if place is not Place.REPORT:
            self._close_report()
        if place is not Place.PLOT and self._opened_plot and tab is self._sample_tab and tab.is_plot_visible():
            # Past the plot's steps, the plot the tour opened is closed again.
            tab.toggle_plot()
            self._opened_plot = False
        if place is Place.PLOT:
            self._show_on_plot(card, tab)
        elif place is Place.MENU:
            self._show_in_menu(card)
        elif place is Place.PREFERENCES:
            dialog = self._open_preferences(step.page)
            extra = page_hidden(step.feature) if self._is_off(step) else ""
            self._present_card(card, dialog, dialog, extra=extra)
        elif place is Place.REPORT:
            dialog = self._open_report(tab)
            if dialog is None:
                self._present_card(card, None, self._window)
            else:
                self._present_card(card, dialog, dialog)
        else:
            self._present_card(card, tab, self._window)

    def _show_on_plot(self, card: _Card, tab: DeviceTab) -> None:
        plot = tab.plot
        if plot is None:
            # Switched on but not loaded: the step is told without the plot.
            self._present_card(card, None, self._window)
            return
        if not tab.is_plot_visible():
            tab.toggle_plot()
            self._opened_plot = True
        view = plot.view
        if view is None:
            # The view is made as the plot is shown; it is there by the
            # next turn of the event loop.
            QTimer.singleShot(0, lambda: self._running and self._show(card))
            return
        host = view.window()
        host.raise_()
        self._present_card(card, view, host)

    def _show_in_menu(self, card: _Card) -> None:
        step = card.step
        bar = self._window.menuBar()
        action = find_action(self._window, step.target)
        if action is None or not opens_in_window(bar):
            extra = where_words(bar, action) if action is not None else ""
            self._present_card(card, None, self._window, extra=extra)
            return
        opener = MenuOpener(self._window, bar, action)
        self._menu = opener
        overlay = self._overlay_on(self._window)
        opener.next_requested.connect(lambda: self._go(1))
        opener.back_requested.connect(lambda: self._go(-1))
        opener.close_requested.connect(self.stop)
        opener.pressed.connect(overlay.press_at)
        opener.released.connect(overlay.release_at)
        opener.shown.connect(overlay.settle)
        self._present_card(
            card, None, self._window,
            locate=lambda: opener.title_rect(overlay.host),
            keep_clear=lambda: opener.area(overlay.host),
        )
        if not opener.open():
            logger.warning("The %s menu could not be opened for the tour", step.target.value)
        overlay.settle()

    def _present_card(
        self,
        card: _Card,
        root: QWidget | None,
        host: QWidget,
        *,
        extra: str = "",
        locate: Locator | None = None,
        keep_clear: Locator | None = None,
    ) -> None:
        """Show ``card`` over ``host``, pointing at its target under ``root``
        (or at what ``locate`` finds), with ``extra`` after its words."""
        step = card.step
        last = self._index == len(self._cards) - 1
        actions = [CardAction("Close tour", self.stop, leading=True)]
        if self._index > 0:
            actions.append(CardAction("Back", lambda: self._go(-1)))
        actions.append(CardAction("Done" if last else "Next", lambda: self._go(1), primary=True))
        count = f"{card.chapter.title}, {card.number} of {card.of}"
        overlay = self._overlay_on(host)
        targets = _targets(step.target)
        if locate is None and root is not None and targets:
            _bring_into_view([w for t in targets for w in find(root, t)])
            locate = self._locator(targets, root, overlay)
        text = words(step.text)
        if root is not None and any(self._in_more_menu(t, root, overlay) for t in targets):
            text = f"{text} {IN_MORE_MENU}"
        if extra:
            text = f"{text} {extra}"
        overlay.show_step(
            step.title, text, count, actions, locate or (lambda: None),
            has_target=locate is not None, keep_clear=keep_clear,
        )

    def _overlay_on(self, host: QWidget) -> TourOverlay:
        if self._overlay is not None and self._overlay.host is not host:
            self._overlay.close_overlay()
            self._overlay = None
        self._set_backdrop(on=host is not self._window)
        if self._overlay is None:
            self._overlay = TourOverlay(host, self._reduced_motion)
            self._overlay.close_requested.connect(self.stop)
            self._overlay.back_requested.connect(lambda: self._go(-1))
            self._overlay.next_requested.connect(lambda: self._go(1))
        return self._overlay

    def _set_backdrop(self, *, on: bool) -> None:
        if on and self._backdrop is None:
            self._backdrop = Backdrop(self._window)
            self._backdrop.close_requested.connect(self.stop)
        elif not on and self._backdrop is not None:
            self._backdrop.close_backdrop()
            self._backdrop = None

    def _locator(self, targets: tuple[Target, ...], root: QWidget, overlay: TourOverlay) -> Locator:
        return lambda: self._locate(targets, root, overlay)

    def _locate(self, targets: tuple[Target, ...], root: QWidget, overlay: TourOverlay) -> QRect | None:
        """Where ``targets`` are, together, in the overlay's coordinates."""
        area = QRect()
        for target in targets:
            widgets: list[QWidget] = []
            for where in (root, self._window):
                widgets = [w for w in find(where, target) if overlay.host.isAncestorOf(w)]
                if widgets:
                    break
            if not widgets and target in TOOLBAR_TARGETS:
                # A toolbar button that does not fit is in the More menu.
                widgets = [w for w in find(root, Target.MORE) if overlay.host.isAncestorOf(w)]
            for widget in widgets:
                area = area.united(_shown_part(widget, overlay.host))
        return area if area.isValid() else None

    def _in_more_menu(self, target: Target, root: QWidget, overlay: TourOverlay) -> bool:
        """Whether ``target`` is a toolbar button moved into the More menu."""
        if target not in TOOLBAR_TARGETS:
            return False
        shown = [w for w in find(root, target) if overlay.host.isAncestorOf(w)]
        return not shown and bool(find(root, Target.MORE))

    # ── What the tour opens over the window ──────────────────────────────

    def _close_menu(self) -> None:
        if self._menu is not None:
            self._menu.close()
            self._menu = None

    def _open_preferences(self, page: str) -> PreferencesDialog:
        """Preferences, opened at ``page``: once, for the steps there in a row."""
        dialog = self._preferences
        if dialog is None:
            dialog = self._window.preferences_dialog()
            self._make_room(dialog)
            dialog.finished.connect(self._closed_by_user)
            dialog.open()
            self._preferences = dialog
        dialog.show_page(page)
        return dialog

    def _close_preferences(self) -> None:
        dialog, self._preferences = self._preferences, None
        self._put_away(dialog)

    def _open_report(self, tab: DeviceTab) -> SessionReportDialog | None:
        """The session report of ``tab``, opened: None if it has none."""
        if self._report is None:
            feature = tab.report
            dialog = feature.report_dialog() if feature is not None else None
            if dialog is None:
                return None
            dialog.finished.connect(self._closed_by_user)
            dialog.open()
            self._report = dialog
        return self._report

    def _close_report(self) -> None:
        dialog, self._report = self._report, None
        self._put_away(dialog)

    def _put_away(self, dialog: QDialog | None) -> None:
        """Close ``dialog`` as Cancel does, so that nothing in it is kept."""
        if dialog is None:
            return
        if self._overlay is not None and self._overlay.host is dialog:
            self._overlay.close_overlay()
            self._overlay = None
        dialog.finished.disconnect(self._closed_by_user)
        dialog.reject()
        dialog.deleteLater()

    def _closed_by_user(self) -> None:
        """A window the tour opened was closed from its title bar: the tour
        ends, as with Close tour."""
        self.stop()

    def _make_room(self, dialog: QDialog) -> None:
        """Widen ``dialog`` by the card's width, as far as the screen allows,
        so that the card can stand beside its settings rather than over them."""
        metrics = tokens.METRICS
        screen = dialog.screen() or self._window.screen()
        room = screen.availableGeometry().width()
        wanted = dialog.width() + metrics.dialog_width + 2 * metrics.space_4
        dialog.resize(max(dialog.width(), min(wanted, room)), dialog.height())

    # ── Tabs the tour opens ──────────────────────────────────────────────

    def _show_first_screen(self) -> None:
        """Bring a tab on its first screen to the front: the user's own if
        it is there, otherwise one the tour opens."""
        tabs = self._window.tabs()
        shown = self._first_screen_tab
        if shown is not None and shown in tabs and not shown.has_session:
            self._window.show_tab(shown)
            return
        users = self._users_tab
        if users is not None and users in tabs and not users.has_session and not users.is_sample:
            self._first_screen_tab, self._opened_first_screen = users, False
        else:
            self._first_screen_tab, self._opened_first_screen = self._window.new_tab(), True
        self._window.show_tab(self._first_screen_tab)

    def _show_sample(self) -> None:
        if self._sample_tab is None or self._sample_tab not in self._window.tabs():
            self._sample_tab = self._window.new_tab(sample=True)
            self._sample_tab.connect_sample()
        self._window.show_tab(self._sample_tab)

    def _close_tab(self, tab: DeviceTab | None) -> None:
        if tab is not None and tab in self._window.tabs():
            self._window.close_tab(self._window.tab_index(tab))

    # ── What has been taken ──────────────────────────────────────────────

    def _note_taken(self, chapter: Chapter) -> None:
        taken = self._config.tour_chapters_taken
        if chapter.id not in taken:
            self._config.tour_chapters_taken = (*taken, chapter.id)
            self._config.save()
