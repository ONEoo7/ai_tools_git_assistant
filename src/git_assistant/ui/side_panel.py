"""The right-hand pane every run-and-read tab carries, and how panes fold away.

Three tabs do the same thing in the same shape: pick a repository, run the
configured provider over it, read what came back. So all three end in the same
right-hand pane -- **Previous Runs** first, because what a run produced is what
you come back for, and **View LLM Calls** behind it, because how it got there is
what you want only when the answer disappoints.

Each tab supplies its own history widget; the calls half is identical
everywhere, which is the point of sharing it.

**It starts folded, and the titles stay on screen when it is.** This pane is
worth a third of the window and is read after a run rather than during one, so
the space belongs to the work until it is asked for. Dragging it shut used to
take the tab titles with it, which left nothing on screen to say the pane
existed and no way back to it but the handle -- a feature that has to be
rediscovered by accident.

A `QTabWidget` cannot do this. It sizes itself from its pages whether they are
visible or not, so hiding the page it is showing collapses nothing: the widget's
size hint stays at the width of its widest page. So the two halves are separate
here -- a `QTabBar` that is always shown and a `QStackedWidget` that is not --
which is what lets the pane shrink to the width of the titles and no further.

The titles run vertically because folded is the resting state and a horizontal
title needs the width the whole exercise is about reclaiming.

That mechanism is `FoldingPane`, and the repository list on the other side of
the same tabs folds by it too (see `git_assistant.ui.repo_pane`): one way of
folding, mirrored, rather than two that drift apart.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QSplitter,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QWidget,
)

from git_assistant.ui.calls_pane import CallsPane

HISTORY_TAB = "Previous Runs"
CALLS_TAB = "View LLM Calls"

#: How wide the pane is when it is opened, and what a host's splitter gives it.
OPEN_WIDTH = 340

#: Dragged narrower than this, the pane folds rather than showing a sliver of a
#: tree. Comfortably above the strip's own width so the fold happens while there
#: is still a handle to grab, not at the very end of the travel.
FOLD_BELOW = 120


class Edge(Enum):
    """The side of a tab a pane folds against. Its titles sit on that edge."""

    LEFT = QTabBar.Shape.RoundedWest
    RIGHT = QTabBar.Shape.RoundedEast


class FoldingPane(QWidget):
    """Pages behind a strip of titles, folded down to the strip until one is clicked."""

    #: The pane opened or folded. Hosts listen so their splitter can follow;
    #: emitted for a fold the user dragged as well as one they clicked.
    toggled = pyqtSignal(bool)  # noqa: N815 - Qt signal naming

    def __init__(
        self,
        *,
        edge: Edge = Edge.RIGHT,
        margins: tuple[int, int, int, int] = (0, 0, 0, 0),
        parent=None,
    ) -> None:
        super().__init__(parent)

        self.stack = QStackedWidget()
        # The pages do not get to say how narrow the pane may be. A calls pane
        # asks for 279px, which is wider than the fold threshold -- so with its
        # minimum respected the handle stops before the pane is ever narrow
        # enough to fold, and dragging it shut becomes impossible. The strip's
        # own width is the floor instead; see `strip_width`.
        self.stack.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        self.tabs = QTabBar()
        self.tabs.setShape(edge.value)
        # A folding pane is the narrow one. Titles that do not fit are elided
        # rather than hidden behind scroll arrows, which is a tab bar nobody
        # recognises as one.
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.setUsesScrollButtons(False)
        self.tabs.setToolTip("Click to open; click the open one again to fold it away.")
        # `tabBarClicked`, not `currentChanged`: clicking the tab already showing
        # changes nothing to listen for, and that click is how it folds again.
        self.tabs.tabBarClicked.connect(self._on_tab_clicked)
        self.tabs.currentChanged.connect(self.stack.setCurrentIndex)

        box = QHBoxLayout(self)
        box.setContentsMargins(*margins)
        box.setSpacing(0)
        # The titles go on the edge the pane folds against, so that folding
        # leaves them where they were rather than sliding them across the tab.
        if edge is Edge.LEFT:
            box.addWidget(self.tabs, 0, Qt.AlignmentFlag.AlignTop)
            box.addWidget(self.stack, 1)
        else:
            box.addWidget(self.stack, 1)
            box.addWidget(self.tabs, 0, Qt.AlignmentFlag.AlignTop)

        # `Ignored` on the stack above buys the freedom to shrink, but it means
        # "ignore the hint and take as much as you can get" -- and a layout
        # inherits that greed from its child, so the pane grew to a third of the
        # window on the first layout however it had been sized. Saying what this
        # widget wants, here, stops the greed without giving the minimum back.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        self._open = False
        self.stack.setVisible(False)  # folded until asked for

    def add_page(self, page: QWidget, title: str) -> int:
        """Put ``page`` behind a new title, and return that title's index."""
        self.stack.addWidget(page)
        return self.tabs.addTab(title)

    # ---- folding -------------------------------------------------------------
    def is_open(self) -> bool:
        """Whether the pane is showing its pages.

        Kept as a flag rather than read back from the stack: `isVisible()` is
        false for every widget whose window has not been shown yet, so asking
        Qt would answer "folded" for a pane that is merely not on screen -- and
        a host that opened it during construction would be told it had not.
        """
        return self._open

    def set_open(self, opening: bool) -> None:
        """Open or fold the pane. Idempotent, so hosts can call it freely."""
        if opening == self._open:
            return
        self._open = opening
        self.stack.setVisible(opening)
        self.toggled.emit(opening)

    def _on_tab_clicked(self, index: int) -> None:
        """A click opens the pane on that tab, or folds the one already open."""
        if not self.is_open():
            self.tabs.setCurrentIndex(index)
            self.set_open(True)
            return
        if index == self.tabs.currentIndex():
            self.set_open(False)  # the way back out, on the control that opened it
            return
        self.tabs.setCurrentIndex(index)

    def strip_width(self) -> int:
        """How wide the pane is with nothing but its titles showing."""
        margins = self.layout().contentsMargins()
        return self.tabs.sizeHint().width() + margins.left() + margins.right()

    def widget(self, index: int) -> QWidget:
        """The page behind a tab. `QTabBar` holds titles, not widgets."""
        return self.stack.widget(index)


class SidePanel(FoldingPane):
    """``Previous Runs`` over ``View LLM Calls``, folded to a strip of titles."""

    def __init__(
        self,
        history: QWidget,
        *,
        repo_name: Callable[[], str] = lambda: "repo",
        margins: tuple[int, int, int, int] = (0, 0, 0, 0),
        parent=None,
    ) -> None:
        super().__init__(edge=Edge.RIGHT, margins=margins, parent=parent)

        self.history = history
        self.add_page(history, HISTORY_TAB)

        self.calls = CallsPane(repo_name=repo_name, show_title=False)
        self._calls_index = self.add_page(self.calls, CALLS_TAB)
        self.tabs.setCurrentIndex(0)

        # The count lives in the tab title: the calls arrive while the other tab
        # is showing, and a silent tab is indistinguishable from a stalled run.
        self.calls.countChanged.connect(self._on_count)

    # ---- what the host reaches for ------------------------------------------
    def _on_count(self, count: int) -> None:
        title = f"{CALLS_TAB} ({count})" if count else CALLS_TAB
        self.tabs.setTabText(self._calls_index, title)

    def show_history(self) -> None:
        """Bring the runs forward, opening the pane if it was folded.

        Opening is the point: this is called when something has been recorded
        for the user to look at, and switching a folded pane's tab would be
        silent.
        """
        self.tabs.setCurrentIndex(0)
        self.set_open(True)

    def show_calls(self) -> None:
        self.tabs.setCurrentIndex(self._calls_index)
        self.set_open(True)


def attach(
    splitter: QSplitter,
    panel: FoldingPane,
    *,
    open_sizes: list[int],
) -> None:
    """Let `panel` fold inside `splitter` without disappearing from it.

    Two directions to keep in step. Clicking a tab has to widen the splitter, or
    the pane opens into the strip's width and shows a two-pixel sliver of a
    tree; and dragging the handle shut has to fold the pane, or the titles end
    up squeezed against a page nobody can read.

    The pane is made non-collapsible so the handle cannot take the titles with
    it -- which is the whole complaint this answers.

    `open_sizes` is the layout the host wants with its panes OPEN, this pane's
    width included; the folded version of it is what gets applied. The host
    must not call `setSizes` itself: before a splitter is shown, `sizes()`
    reports the geometry it has rather than the widths it has been given, so
    anything computed from it at wiring time is discarded by the first real
    layout -- which is exactly how a pane that starts folded ended up three
    hundred pixels wide with its strip adrift in the middle.

    A tab may attach a pane on each side. Pass both calls the same
    `open_sizes`; either order works.
    """
    index = splitter.indexOf(panel)
    if index < 0:
        return
    splitter.setCollapsible(index, False)
    # No stretch, whatever the host asked for. A stretch factor is a claim on
    # space the window gains, and this pane's width is decided by whether it is
    # folded -- with one, the first layout hands a folded pane a few hundred
    # pixels of nothing and the strip floats in the middle of it.
    splitter.setStretchFactor(index, 0)

    #: Set while a fold is being driven by the handle. The resize below would
    #: otherwise answer the user's own drag by snapping the pane to a width
    #: they did not choose.
    from_drag = {"busy": False}

    def resize(opening: bool) -> None:
        if from_drag["busy"]:
            return
        sizes = splitter.sizes()
        if index >= len(sizes):
            return
        target = open_sizes[index] if opening else panel.strip_width()
        delta = target - sizes[index]
        if not delta:
            return
        # Taken from, or given back to, the widest neighbour. Spreading it over
        # all of them moves panes nobody touched, and the widest is the one with
        # room to give.
        others = [i for i in range(len(sizes)) if i != index]
        if not others:
            return
        donor = max(others, key=lambda i: sizes[i])
        sizes[index] = target
        sizes[donor] = max(0, sizes[donor] - delta)
        splitter.setSizes(sizes)

    panel.toggled.connect(resize)

    # The declared layout, folded, and applied here rather than by the host so
    # that this is the last word on the pane's starting width.
    splitter.setSizes(_folded(splitter, open_sizes))

    def dragged(_pos: int, _handle: int) -> None:
        sizes = splitter.sizes()
        if index >= len(sizes):
            return
        wanted = sizes[index] >= FOLD_BELOW
        if wanted == panel.is_open():
            return
        # Dragging wider opens it as well as narrower folding it: a widening
        # drag on a folded pane is a request to see what is in it.
        from_drag["busy"] = True
        try:
            panel.set_open(wanted)
        finally:
            from_drag["busy"] = False

    splitter.splitterMoved.connect(dragged)


def _folded(splitter: QSplitter, open_sizes: list[int]) -> list[int]:
    """`open_sizes` with every folded pane in `splitter` down to its strip.

    Every folding pane in the splitter, not only the one being attached: a tab
    with a pane on each side attaches them one after the other, and a layout
    that folded only its own pane would open the other one back up. What the
    folded panes give up goes to the widest pane that does not fold.
    """
    sizes = list(open_sizes)
    folded = [
        index
        for index in range(min(splitter.count(), len(sizes)))
        if isinstance(pane := splitter.widget(index), FoldingPane)
        and not pane.is_open()
    ]
    spare = 0
    for index in folded:
        strip = splitter.widget(index).strip_width()
        spare += sizes[index] - strip
        sizes[index] = strip
    others = [index for index in range(len(sizes)) if index not in folded]
    if others:
        sizes[max(others, key=lambda index: sizes[index])] += spare
    return sizes
