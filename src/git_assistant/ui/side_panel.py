"""The right-hand pane every run-and-read tab carries.

Three tabs do the same thing in the same shape: pick a repository, run the
configured provider over it, read what came back. So all three end in the same
right-hand pane -- **Previous Runs** first, because what a run produced is what
you come back for, and **View LLM Calls** behind it, because how it got there is
what you want only when the answer disappoints.

Each tab supplies its own history widget; the calls half is identical
everywhere, which is the point of sharing it.
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from git_assistant.ui.calls_pane import CallsPane

HISTORY_TAB = "Previous Runs"
CALLS_TAB = "View LLM Calls"


class SidePanel(QWidget):
    """``Previous Runs`` (the default) over ``View LLM Calls``."""

    def __init__(
        self,
        history: QWidget,
        *,
        repo_name: Callable[[], str] = lambda: "repo",
        margins: tuple[int, int, int, int] = (0, 0, 0, 0),
        parent=None,
    ) -> None:
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(*margins)

        self.tabs = QTabWidget()
        # This pane is the narrow one. Two titles that do not fit are elided
        # rather than hidden behind scroll arrows, which is a tab bar nobody
        # recognises as one.
        self.tabs.setElideMode(Qt.TextElideMode.ElideRight)
        self.tabs.setUsesScrollButtons(False)
        self.history = history
        self.tabs.addTab(history, HISTORY_TAB)

        self.calls = CallsPane(repo_name=repo_name, show_title=False)
        self._calls_index = self.tabs.addTab(self.calls, CALLS_TAB)
        # The count lives in the tab title: the calls arrive while the other tab
        # is showing, and a silent tab is indistinguishable from a stalled run.
        self.calls.countChanged.connect(self._on_count)
        self.tabs.setCurrentIndex(0)
        box.addWidget(self.tabs, 1)

    def _on_count(self, count: int) -> None:
        title = f"{CALLS_TAB} ({count})" if count else CALLS_TAB
        self.tabs.setTabText(self._calls_index, title)

    # ---- what the host reaches for ------------------------------------------
    def show_history(self) -> None:
        self.tabs.setCurrentIndex(0)

    def show_calls(self) -> None:
        self.tabs.setCurrentIndex(self._calls_index)
