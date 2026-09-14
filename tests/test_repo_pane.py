"""The repository list, folded against the left edge of every repo-driven tab."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel, QSplitter, QTabBar  # noqa: E402

from git_assistant import commit_history  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui import side_panel as side_panel_mod  # noqa: E402
from git_assistant.ui.preview_dialog import CommitPanel  # noqa: E402
from git_assistant.ui.repo_pane import REPO_TAB, RepoPane  # noqa: E402
from git_assistant.ui.repo_picker import RepoPicker  # noqa: E402
from git_assistant.ui.side_panel import SidePanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        commit_history, "user_config_dir", lambda *a, **k: str(tmp_path)
    )


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [RepoEntry("/x/alpha"), RepoEntry("/x/beta")]
    s.active_repo = "/x/alpha"
    return s


@pytest.fixture
def pane(qapp, settings):
    return RepoPane(RepoPicker(settings))


# ---- on its own -------------------------------------------------------------------
def test_it_starts_folded_with_its_title_on_screen(pane):
    assert pane.is_open() is False
    assert pane.tabs.isVisibleTo(pane)
    assert pane.tabs.tabText(0) == REPO_TAB
    assert not pane.stack.isVisibleTo(pane)


def test_its_title_runs_up_the_left_edge(pane):
    assert pane.tabs.shape() == QTabBar.Shape.RoundedWest
    layout = pane.layout()
    assert layout.itemAt(0).widget() is pane.tabs, "the strip comes first, on the left"


def test_the_list_does_not_show_its_own_title_twice(pane):
    """The strip already says what this is."""
    assert not pane.picker.title_label.isVisibleTo(pane)


def test_a_picker_on_its_own_still_has_its_title(qapp, settings):
    picker = RepoPicker(settings)
    assert picker.title_label.isVisibleTo(picker)


def test_clicking_the_title_opens_it_and_clicking_again_folds_it(pane):
    pane.tabs.tabBarClicked.emit(0)
    assert pane.is_open() is True
    assert pane.stack.currentWidget() is pane.picker

    pane.tabs.tabBarClicked.emit(0)

    assert pane.is_open() is False


def test_it_starts_open_when_nothing_is_selected(qapp):
    """Folded is for when the bar above names the selection. With none, folding
    would hide the only thing there is to do."""
    empty = Settings()
    empty.save = lambda: None

    assert RepoPane(RepoPicker(empty)).is_open() is True


# ---- beside another folding pane in one splitter -------------------------------------
def _splitter(qapp, first_attached):
    """Repository pane, a middle pane, and the right-hand pane, attached in turn."""
    settings = Settings()
    settings.save = lambda: None
    settings.repos = [RepoEntry("/x/alpha")]
    settings.active_repo = "/x/alpha"
    repo = RepoPane(RepoPicker(settings))
    side = SidePanel(QLabel("runs"))
    middle = QLabel("the work")
    middle.setMinimumWidth(0)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    for widget in (repo, middle, side):
        splitter.addWidget(widget)
    sizes = [240, 600, side_panel_mod.OPEN_WIDTH]
    order = [repo, side] if first_attached == "repo" else [side, repo]
    for one in order:
        side_panel_mod.attach(splitter, one, open_sizes=sizes)
    splitter.resize(1200, 600)
    splitter.show()
    qapp.processEvents()
    return splitter, repo, side


@pytest.mark.parametrize("first_attached", ["repo", "side"])
def test_both_panes_start_folded_whichever_is_attached_first(qapp, first_attached):
    """Attaching the second pane must not open the first one back up."""
    splitter, repo, side = _splitter(qapp, first_attached)

    sizes = splitter.sizes()
    assert sizes[0] == repo.strip_width()
    assert sizes[2] == side.strip_width()
    splitter.close()


def test_opening_one_leaves_the_other_folded(qapp):
    splitter, repo, side = _splitter(qapp, "repo")

    repo.set_open(True)

    sizes = splitter.sizes()
    assert sizes[0] == 240
    assert sizes[2] == side.strip_width()
    assert side.is_open() is False
    splitter.close()


# ---- in the tabs ---------------------------------------------------------------------
def test_the_commit_tab_keeps_its_run_settings_on_screen_with_the_list_folded(
    qapp, settings
):
    panel = CommitPanel(settings, auto_start=False)

    assert panel.repo_pane.is_open() is False
    for control in (panel.branch_combo, panel.template_combo, panel.provider_combo):
        assert control.isVisibleTo(panel)


@pytest.mark.parametrize(
    "tab", ["AgentsPanel", "ReviewPanel", "BranchesTagsPanel", "CloneCreatePanel"]
)
def test_every_repo_driven_tab_folds_its_repository_list(qapp, settings, tab):
    from git_assistant.ui.agents_panel import AgentsPanel
    from git_assistant.ui.branches_tags_panel import BranchesTagsPanel
    from git_assistant.ui.clone_create_panel import CloneCreatePanel
    from git_assistant.ui.review_panel import ReviewPanel

    build = {
        "AgentsPanel": AgentsPanel,
        "ReviewPanel": ReviewPanel,
        "BranchesTagsPanel": BranchesTagsPanel,
        "CloneCreatePanel": CloneCreatePanel,
    }[tab]
    panel = build(settings)

    assert panel.repo_pane.picker is panel.repo_picker
    assert panel.repo_pane.is_open() is False
