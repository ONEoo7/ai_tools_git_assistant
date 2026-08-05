"""The gaps around a splitter handle, which all three tabs get the same way.

A pane with a handle on both sides needs a margin on both. With one only, its
content sits flush against one divider and inset from the other, which reads as
a misalignment rather than as a margin -- and it is the kind of thing that comes
back every time a pane is added.
"""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QSplitter  # noqa: E402

from git_assistant import commit_history  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.review import history as review_history  # noqa: E402
from git_assistant.review import rules as review_rules  # noqa: E402
from git_assistant.ui.agents_panel import AgentsPanel  # noqa: E402
from git_assistant.ui.preview_dialog import SECTION_GAP, CommitPanel  # noqa: E402
from git_assistant.ui.review_panel import ReviewPanel  # noqa: E402
from git_assistant.ui.usage_pane import UsagePane  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    for module in (commit_history, review_history, review_rules):
        monkeypatch.setattr(module, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None
    s.repos = [RepoEntry("/x/demo")]
    s.active_repo = "/x/demo"
    return s


def _panes(panel):
    """The widgets of the tab's own left-to-right splitter."""
    outer = [
        s
        for s in panel.findChildren(QSplitter)
        if s.orientation() == Qt.Orientation.Horizontal and s.count() >= 3
    ]
    assert outer, "the tab is expected to lay its panes out side by side"
    splitter = outer[0]
    return [splitter.widget(i) for i in range(splitter.count())]


def _gaps(pane):
    margins = pane.layout().contentsMargins()
    return margins.left(), margins.right()


def _vertical_gaps(pane):
    margins = pane.layout().contentsMargins()
    return margins.top(), margins.bottom()


@pytest.mark.parametrize("build", [CommitPanel, AgentsPanel, ReviewPanel])
def test_every_pane_is_inset_from_the_handles_beside_it(qapp, settings, build):
    panel = build(settings) if build is not CommitPanel else build(settings, auto_start=False)
    panes = _panes(panel)

    for index, pane in enumerate(panes):
        left, right = _gaps(pane)
        # A handle on that side means a gap on that side, and the outer edges
        # keep none: the tab's own layout already provides those.
        assert left == (SECTION_GAP if index else 0), f"pane {index} left"
        assert right == (SECTION_GAP if index < len(panes) - 1 else 0), f"pane {index} right"


@pytest.mark.parametrize("build", [ReviewPanel, UsagePane])
def test_a_pane_stacked_on_another_is_inset_from_the_handle_too(qapp, settings, build):
    """The same rule, turned ninety degrees: a handle above or below is still one."""
    panel = build() if build is UsagePane else build(settings)
    stacked = [
        s
        for s in panel.findChildren(QSplitter)
        if s.orientation() == Qt.Orientation.Vertical
    ]
    assert stacked, "this tab is expected to stack two panes"

    for splitter in stacked:
        panes = [splitter.widget(i) for i in range(splitter.count())]
        for index, pane in enumerate(panes):
            top, bottom = _vertical_gaps(pane)
            assert top == (SECTION_GAP if index else 0), f"pane {index} top"
            assert bottom == (
                SECTION_GAP if index < len(panes) - 1 else 0
            ), f"pane {index} bottom"
