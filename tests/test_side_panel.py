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
    assert panel.tabs.widget(0) is panel.history


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
