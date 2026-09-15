"""The Compare tab: two repositories' submodules side by side.

Against real repositories sharing real submodules: what is on screen is what git
says each submodule is at, and a stub would only be this file's idea of that.
"""

import subprocess
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu  # noqa: E402

from git_assistant import submodule_compare as compare  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui import compare_panel as panel_module  # noqa: E402
from git_assistant.ui.compare_panel import (  # noqa: E402
    COMMIT,
    DATE,
    MESSAGE,
    PATH,
    TAG,
    ComparePanel,
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _git_home(folder, patch):
    """An identity, a fixed clock, and local folders allowed as submodule sources."""
    config = folder / "global.gitconfig"
    config.write_text(
        '[protocol "file"]\n\tallow = always\n'
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    patch.setenv("GIT_CONFIG_GLOBAL", str(config))
    patch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    patch.setenv("GIT_COMMITTER_DATE", "2026-01-05T06:07:08+02:00")


@pytest.fixture(autouse=True)
def git_home(tmp_path, monkeypatch):
    _git_home(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def inline(monkeypatch):
    """Read on the test's own thread, so the rows are there when the call returns."""
    monkeypatch.setattr(panel_module, "run_worker", lambda worker: worker.run())


def _library(path, *messages):
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    for number, message in enumerate(messages):
        (path / "f.txt").write_text(f"{number}\n", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", message)
    return path


def _add(top, source, path):
    _git(top, "submodule", "add", "-q", str(source), path)
    _git(top, "commit", "-q", "-m", f"add {path}")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """Made once for the module: its forty git commands were most of every test's time."""
    tmp_path = tmp_path_factory.mktemp("compare")
    with pytest.MonkeyPatch.context() as patch:
        _git_home(tmp_path, patch)
        return _build(tmp_path)


@pytest.fixture
def projects(built):
    """As built, whatever the test before it did to beta's checkout of can."""
    yield built
    _git(built["beta"] / "libs" / "can", "checkout", "-q", built["pinned"])


def _build(tmp_path):
    """alpha has can at its newest, ccp, and a submodule of its own; beta pins an older can."""
    can = _library(tmp_path / "sources" / "can", "first frames", "second frames")
    _git(can, "tag", "-a", "v2.0", "-m", "release", "HEAD")
    ccp = _library(tmp_path / "sources" / "ccp", "ccp")
    only = _library(tmp_path / "sources" / "only", "only in alpha")

    alpha = _library(tmp_path / "work" / "alpha", "start")
    for source, path in ((can, "libs/can"), (ccp, "libs/ccp"), (only, "libs/only")):
        _add(alpha, source, path)

    beta = _library(tmp_path / "work" / "beta", "start")
    _add(beta, can, "libs/can")
    _git(beta / "libs" / "can", "checkout", "-q", "HEAD~1")
    _git(beta, "add", "libs/can")
    _git(beta, "commit", "-q", "-m", "pin can")
    _add(beta, ccp, "libs/ccp")
    pinned = _git(beta / "libs" / "can", "rev-parse", "HEAD").stdout.strip()
    return {"alpha": alpha, "beta": beta, "can": can, "pinned": pinned}


@pytest.fixture
def settings(projects):
    s = Settings()
    s.save = lambda: None  # never the real file
    s.repos = [RepoEntry(str(projects["alpha"])), RepoEntry(str(projects["beta"]))]
    s.active_repo = str(projects["alpha"])
    s.compare_repo = str(projects["beta"])
    return s


def _short(repo, rev="HEAD"):
    return _git(repo, "rev-parse", "--short", rev).stdout.strip()


def _row(tree, index):
    item = tree.topLevelItem(index)
    return [item.text(column) for column in (PATH, COMMIT, TAG, DATE, MESSAGE)]


def _paths(tree):
    return [tree.topLevelItem(i).text(PATH) for i in range(tree.topLevelItemCount())]


def _shown(tree):
    return [
        tree.topLevelItem(i).text(PATH)
        for i in range(tree.topLevelItemCount())
        if not tree.topLevelItem(i).isHidden()
    ]


def test_each_side_lists_every_submodule_of_either_beside_the_same_one(qapp, settings, projects):
    panel = ComparePanel(settings)

    panel.refresh_repos()

    can = projects["can"]
    assert _paths(panel.left_tree) == _paths(panel.right_tree) == [
        "libs/can",
        "libs/ccp",
        "libs/only",
    ]
    assert _row(panel.left_tree, 0) == [
        "libs/can", _short(can, "HEAD"), "v2.0", "2026-01-05 06:07", "second frames"
    ]
    assert _row(panel.right_tree, 0) == [
        "libs/can", _short(can, "HEAD~1"), "", "2026-01-05 06:07", "first frames"
    ]
    assert _row(panel.left_tree, 1)[1:] == _row(panel.right_tree, 1)[1:]
    assert _row(panel.right_tree, 2) == ["libs/only", "", "", "", "(not in this repository)"]


def test_the_summary_counts_what_is_the_same_and_what_is_not(qapp, settings, projects):
    panel = ComparePanel(settings)

    panel.refresh_repos()

    alpha = RepoEntry(str(projects["alpha"])).display()
    assert panel.summary.text() == f"3 submodules: 1 the same, 1 different, 1 only in {alpha}."
    assert "alpha" in panel.left_title.text() and "beta" in panel.right_title.text()


def test_only_differences_hides_what_both_sides_have_at_one_commit(qapp, settings):
    panel = ComparePanel(settings)
    panel.refresh_repos()

    panel.differences_check.setChecked(True)

    assert _shown(panel.left_tree) == _shown(panel.right_tree) == ["libs/can", "libs/only"]
    panel.differences_check.setChecked(False)
    assert len(_shown(panel.left_tree)) == 3


def test_the_commits_recorded_can_be_compared_instead_of_the_ones_checked_out(
    qapp, settings, projects
):
    """beta checks out the newest can again, without committing to it."""
    _git(projects["beta"] / "libs" / "can", "checkout", "-q", "main")
    panel = ComparePanel(settings)
    panel.refresh_repos()
    can = projects["can"]
    alpha = RepoEntry(str(projects["alpha"])).display()

    assert panel.summary.text() == f"3 submodules: 2 the same, 1 only in {alpha}."
    assert _row(panel.right_tree, 0)[1] == (
        f"{_short(can, 'HEAD')} (recorded: {_short(can, 'HEAD~1')})"
    )

    panel.showing_combo.setCurrentIndex(panel.showing_combo.findData(compare.Showing.RECORDED))

    assert panel.summary.text() == f"3 submodules: 1 the same, 1 different, 1 only in {alpha}."
    assert _row(panel.right_tree, 0)[1:] == [
        f"{_short(can, 'HEAD~1')} (checked out: {_short(can, 'HEAD')})",
        "",
        "2026-01-05 06:07",
        "first frames",
    ]


def test_choosing_what_to_compare_with_changes_nothing_else(qapp, settings, projects):
    settings.compare_repo = ""
    settings.recent_repos = []
    panel = ComparePanel(settings)
    panel.refresh_repos()
    assert panel.summary.text().startswith("Choose a repository to compare it with")
    assert panel.repo_pane.is_open()
    assert panel.repo_pane.tabs.currentIndex() == panel.other_page

    assert panel.other_picker.select(str(projects["beta"]))

    assert settings.compare_repo == str(projects["beta"])
    assert settings.active_repo == str(projects["alpha"])
    assert settings.recent_repos == []
    assert _paths(panel.right_tree) == ["libs/can", "libs/ccp", "libs/only"]


def test_the_two_sides_select_as_one(qapp, settings):
    panel = ComparePanel(settings)
    panel.refresh_repos()

    panel.right_tree.setCurrentItem(panel.right_tree.topLevelItem(2))
    assert panel.left_tree.indexOfTopLevelItem(panel.left_tree.currentItem()) == 2

    panel.left_tree.setCurrentItem(panel.left_tree.topLevelItem(1))
    assert panel.right_tree.indexOfTopLevelItem(panel.right_tree.currentItem()) == 1


def test_nothing_is_read_until_the_tab_is_looked_at(qapp, settings, monkeypatch):
    """Built with the window, and a git command a submodule for a tab maybe never opened."""
    read = []
    real = compare.read_both
    monkeypatch.setattr(compare, "read_both", lambda *a: read.append(a) or real(*a))
    panel = ComparePanel(settings)
    qapp.processEvents()
    assert read == []

    panel.show()
    qapp.processEvents()
    panel.hide()
    panel.show()
    qapp.processEvents()

    assert len(read) == 1
    panel.close()


def test_an_answer_about_repositories_no_longer_chosen_is_dropped(qapp, settings):
    panel = ComparePanel(settings)
    panel.refresh_repos()
    before = _paths(panel.left_tree)

    panel._on_read((panel._asked - 1, "elsewhere", "entirely", ([], []), ""))

    assert _paths(panel.left_tree) == before


def test_a_read_that_fails_says_why_and_keeps_what_is_on_screen(qapp, settings, monkeypatch):
    panel = ComparePanel(settings)
    panel.refresh_repos()

    def gone(*_args):
        raise OSError("the drive went away")

    monkeypatch.setattr(compare, "read_both", gone)
    panel.refresh_btn.click()

    assert panel.status.text() == "Could not read the submodules: the drive went away"
    assert len(_paths(panel.left_tree)) == 3


def test_a_commit_hash_can_be_copied_from_either_side(qapp, settings, projects, monkeypatch):
    panel = ComparePanel(settings)
    panel.refresh_repos()
    item = panel.right_tree.topLevelItem(0)
    monkeypatch.setattr(QMenu, "exec", lambda menu, *a, **k: menu.actions()[0])

    panel._on_menu(panel.right_tree, panel.right_tree.visualItemRect(item).center())

    full = _git(projects["can"], "rev-parse", "HEAD~1").stdout.strip()
    assert QGuiApplication.clipboard().text() == full


# ---- in the window ------------------------------------------------------------------------
def test_the_window_has_a_compare_tab_after_branches_and_tags(qapp, settings, monkeypatch):
    from git_assistant.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(settings)
    labels = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    assert labels[labels.index("Branches && Tags") + 1] == "Compare"

    refreshed = []
    monkeypatch.setattr(dialog.compare_panel, "refresh_repos", lambda: refreshed.append(True))
    dialog.tabs.setCurrentIndex(labels.index("Compare"))

    assert refreshed == [True]
    dialog.close()


def test_the_first_visit_in_the_window_reads_both_sides_once(qapp, settings, monkeypatch):
    """A tab is shown before the window hears it was switched to, and both used to read."""
    from git_assistant.ui.settings_dialog import SettingsDialog

    read = []
    real = compare.read_both
    monkeypatch.setattr(compare, "read_both", lambda *a: read.append(a) or real(*a))
    dialog = SettingsDialog(settings)
    dialog.show()
    qapp.processEvents()
    labels = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]

    dialog.tabs.setCurrentIndex(labels.index("Compare"))
    for _ in range(5):
        qapp.processEvents()

    assert len(read) == 1
    assert len(_paths(dialog.compare_panel.left_tree)) == 3
    dialog.close()
