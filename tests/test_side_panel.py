"""The right-hand pane the run-and-read tabs share."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

from git_assistant.llm_log import LlmCall  # noqa: E402
from git_assistant.ui.side_panel import CALLS_TAB, HISTORY_TAB, SidePanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qapp):
    return SidePanel(QLabel("the previous runs go here"))


def _call(index=1):
    return LlmCall(index, "reviewing a file", "m", "sys", "user", 512, response="ok")


def test_previous_runs_comes_first_and_is_what_is_shown(panel):
    assert panel.tabs.tabText(0) == HISTORY_TAB
    assert panel.tabs.tabText(1) == CALLS_TAB
    assert panel.tabs.currentIndex() == 0


def test_the_history_widget_is_the_one_the_tab_supplied(panel):
    assert panel.widget(0) is panel.history


def test_the_calls_tab_counts_them_while_the_other_tab_is_showing(panel):
    """A silent tab is indistinguishable from a stalled run."""
    panel.calls.add_call(_call(1))
    panel.calls.add_call(_call(2))

    assert panel.tabs.tabText(1) == f"{CALLS_TAB} (2)"
    assert panel.tabs.currentIndex() == 0, "the run must not steal the view"


def test_resetting_puts_the_title_back(panel):
    panel.calls.add_call(_call())
    panel.calls.reset()
    assert panel.tabs.tabText(1) == CALLS_TAB


def test_the_pane_does_not_show_its_own_title_twice(panel):
    """The tab already says what this is."""
    assert not panel.calls.calls_label.isVisibleTo(panel)


def test_either_half_can_be_brought_forward(panel):
    panel.show_calls()
    assert panel.tabs.currentIndex() == 1
    panel.show_history()
    assert panel.tabs.currentIndex() == 0


# ---- folding -------------------------------------------------------------------------
def test_it_starts_folded_with_its_titles_still_on_screen(panel):
    """The complaint this answers: dragging it shut took the titles with it,
    leaving nothing to say the pane was there and no way back but the handle."""
    assert panel.is_open() is False
    assert panel.tabs.isVisibleTo(panel)
    assert panel.tabs.count() == 2
    assert not panel.stack.isVisibleTo(panel)


def test_the_titles_read_vertically_so_the_strip_can_be_narrow(panel):
    from PyQt6.QtWidgets import QTabBar

    assert panel.tabs.shape() == QTabBar.Shape.RoundedEast


def test_the_strip_is_far_narrower_than_the_open_pane(panel):
    from git_assistant.ui.side_panel import OPEN_WIDTH

    assert panel.strip_width() < OPEN_WIDTH / 3


def test_clicking_a_tab_opens_the_pane_on_it(panel):
    panel.tabs.tabBarClicked.emit(1)
    assert panel.is_open() is True
    assert panel.tabs.currentIndex() == 1
    assert panel.stack.currentWidget() is panel.calls


def test_clicking_the_open_tab_again_folds_it(panel):
    """The way back out, on the control that opened it."""
    panel.tabs.tabBarClicked.emit(0)
    assert panel.is_open() is True

    panel.tabs.tabBarClicked.emit(0)

    assert panel.is_open() is False


def test_clicking_the_other_tab_switches_rather_than_folding(panel):
    panel.tabs.tabBarClicked.emit(0)
    panel.tabs.tabBarClicked.emit(1)
    assert panel.is_open() is True
    assert panel.tabs.currentIndex() == 1


def test_opening_and_folding_are_announced(panel):
    seen = []
    panel.toggled.connect(seen.append)

    panel.set_open(True)
    panel.set_open(True)  # already open: nothing to announce
    panel.set_open(False)

    assert seen == [True, False]


def test_bringing_a_half_forward_opens_a_folded_pane(panel):
    """`show_calls` is called when there is something to look at.

    Switching a folded pane's tab would be silent, which is the same as the
    call having done nothing.
    """
    assert panel.is_open() is False
    panel.show_calls()
    assert panel.is_open() is True
    assert panel.tabs.currentIndex() == 1

    panel.set_open(False)
    panel.show_history()
    assert panel.is_open() is True
    assert panel.tabs.currentIndex() == 0


# ---- inside a splitter ----------------------------------------------------------------
@pytest.fixture
def in_splitter(qapp):
    """A pane in a splitter, wired the way the three tabs wire theirs."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QSplitter, QTreeWidget

    from git_assistant.ui import side_panel as mod

    panel = SidePanel(QLabel("runs"))
    splitter = QSplitter(Qt.Orientation.Horizontal)
    splitter.addWidget(QTreeWidget())
    splitter.addWidget(panel)
    # The host declares the OPEN layout; `attach` applies the folded one.
    mod.attach(splitter, panel, open_sizes=[700, mod.OPEN_WIDTH])
    splitter.resize(1000, 600)
    splitter.show()
    qapp.processEvents()
    return splitter, panel, mod


def test_the_handle_cannot_drag_the_titles_away(in_splitter):
    splitter, panel, _ = in_splitter
    assert splitter.isCollapsible(splitter.indexOf(panel)) is False


def test_opening_it_widens_the_splitter_rather_than_squeezing_a_sliver(in_splitter):
    splitter, panel, mod = in_splitter

    panel.set_open(True)

    assert splitter.sizes()[splitter.indexOf(panel)] == mod.OPEN_WIDTH


def test_it_starts_folded_in_the_splitter_too(in_splitter):
    """The declared layout is the OPEN one, so the folded one has to be applied.

    Before a splitter is shown, `sizes()` reports the geometry it has rather
    than the widths it was given -- so a fold computed at wiring time is thrown
    away by the first real layout, and the pane arrives hundreds of pixels wide
    with its strip adrift in the middle of it.
    """
    splitter, panel, _ = in_splitter
    assert panel.is_open() is False
    assert splitter.sizes()[splitter.indexOf(panel)] == panel.strip_width()


def test_folding_it_gives_the_width_back(in_splitter):
    splitter, panel, _ = in_splitter
    panel.set_open(True)
    before = splitter.sizes()

    panel.set_open(False)

    after = splitter.sizes()
    index = splitter.indexOf(panel)
    assert after[index] == panel.strip_width()
    assert after[0] > before[0]  # the neighbour got it back


def test_the_width_comes_from_the_widest_neighbour_not_from_all_of_them(qapp):
    """Spreading it moves panes nobody touched."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QSplitter, QTreeWidget

    from git_assistant.ui import side_panel as mod

    panel = SidePanel(QLabel("runs"))
    splitter = QSplitter(Qt.Orientation.Horizontal)
    narrow, wide = QTreeWidget(), QTreeWidget()
    for one in (narrow, wide):
        one.setMinimumWidth(0)
    splitter.addWidget(narrow)
    splitter.addWidget(wide)
    splitter.addWidget(panel)
    mod.attach(splitter, panel, open_sizes=[150, 700, mod.OPEN_WIDTH])
    splitter.resize(1200, 600)
    splitter.show()
    qapp.processEvents()
    before = splitter.sizes()

    panel.set_open(True)

    after = splitter.sizes()
    assert after[0] == before[0], "the narrow pane must not move"
    assert after[1] < before[1]


def test_dragging_the_handle_shut_folds_it(in_splitter):
    splitter, panel, mod = in_splitter
    panel.set_open(True)

    index = splitter.indexOf(panel)
    sizes = splitter.sizes()
    sizes[0] += sizes[index] - panel.strip_width()
    sizes[index] = panel.strip_width()
    splitter.setSizes(sizes)
    splitter.splitterMoved.emit(0, 1)

    assert panel.is_open() is False
    assert panel.tabs.isVisibleTo(panel), "the titles stay, which is the whole point"


def test_dragging_the_handle_open_again_unfolds_it(in_splitter):
    splitter, panel, mod = in_splitter
    index = splitter.indexOf(panel)

    sizes = splitter.sizes()
    sizes[0] -= mod.OPEN_WIDTH - sizes[index]
    sizes[index] = mod.OPEN_WIDTH
    splitter.setSizes(sizes)
    splitter.splitterMoved.emit(0, 1)

    assert panel.is_open() is True


def test_a_drag_that_does_not_cross_the_threshold_changes_nothing(in_splitter):
    splitter, panel, mod = in_splitter
    panel.set_open(True)
    index = splitter.indexOf(panel)

    sizes = splitter.sizes()
    sizes[0] += 40
    sizes[index] -= 40  # still comfortably open
    splitter.setSizes(sizes)
    splitter.splitterMoved.emit(0, 1)

    assert panel.is_open() is True
