"""The active repository, named in the bar above the tabs.

The repository list folds away now, so this is where the selected repository and
its branch are read -- and it has to stay right through a checkout, which changes
the branch without changing which repository is selected.
"""

import subprocess
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant import git_ops  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.identities import IdentityStore  # noqa: E402
from git_assistant.ui import theme  # noqa: E402
from git_assistant.ui.identity_bar import IdentityBar  # noqa: E402

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


def _repo(path, branch):
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "f.txt").write_bytes(b"f\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    _git(path, "branch", "-M", branch)
    return path


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def app(qapp):
    yield qapp
    theme.apply(qapp, theme.SYSTEM)  # never leave it on the next test


@pytest.fixture
def repos(tmp_path):
    return {
        "alpha": _repo(tmp_path / "ONEoo7" / "alpha", "main"),
        "beta": _repo(tmp_path / "forks" / "beta", "feature/login"),
    }


@pytest.fixture
def settings(repos):
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [RepoEntry(str(path)) for path in repos.values()]
    s.active_repo = str(repos["alpha"])
    return s


@pytest.fixture
def bar(qapp, settings):
    return IdentityBar(settings, IdentityStore([]))


# ---- what it says ----------------------------------------------------------------
def test_it_names_the_active_repository_and_its_branch(bar, repos):
    assert bar.repo_name.text() == "ONEoo7\\alpha"
    assert bar.repo_name.toolTip() == str(repos["alpha"])
    assert bar.repo_branch.text() == "main"


def test_it_follows_the_selection(bar, repos):
    bar.set_repo(str(repos["beta"]))

    assert bar.repo_name.text() == "forks\\beta"
    assert bar.repo_branch.text() == "feature/login"


def test_a_repository_with_a_label_goes_by_it(qapp, settings, repos):
    settings.repos[0].label = "Work"

    bar = IdentityBar(settings, IdentityStore([]))

    assert bar.repo_name.text() == "Work"


def test_with_no_repository_it_says_so(qapp):
    empty = Settings()
    empty.save = lambda: None

    bar = IdentityBar(empty, IdentityStore([]))

    assert bar.repo_name.text() == "(none)"
    assert bar.repo_branch.text() == ""


def test_a_detached_head_names_no_branch(qapp, settings, repos):
    _git(repos["alpha"], "checkout", "-q", "--detach", "HEAD")

    bar = IdentityBar(settings, IdentityStore([]))

    assert bar.repo_branch.text() == ""


def test_a_checkout_is_shown_without_looking_the_identity_up_again(
    bar, repos, monkeypatch
):
    """`show_active_repository` is what runs on every checkout, on any tab."""
    asked = []
    monkeypatch.setattr(git_ops, "get_identity", lambda *a: asked.append(a) or ("", ""))
    _git(repos["alpha"], "checkout", "-q", "-b", "fix/typo")

    bar.show_active_repository()

    assert bar.repo_branch.text() == "fix/typo"
    assert asked == []


def test_the_branch_is_the_green_the_repository_list_uses(app, settings):
    theme.apply(app, theme.DARK)
    bar = IdentityBar(settings, IdentityStore([]))
    dark = bar.repo_branch.styleSheet()

    theme.apply(app, theme.LIGHT)
    app.processEvents()

    assert "#5fd39a" in dark
    assert "#1a7f4b" in bar.repo_branch.styleSheet()


# ---- in the window ------------------------------------------------------------------
def test_clone_and_create_sits_just_left_of_commit(qapp, settings):
    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)
    commit = dlg.tabs.indexOf(dlg.commit_panel)

    assert dlg.tabs.tabText(commit) == "Commit"
    assert dlg.tabs.widget(commit - 1) is dlg.clone_panel
    assert dlg.tabs.tabText(commit - 1) == "Clone && Create"


def test_the_window_still_opens_on_commit(qapp, settings):
    """It is the tab used every day; a tab to its left does not change that."""
    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)

    assert dlg.tabs.currentWidget() is dlg.commit_panel


def test_choosing_a_repository_on_clone_and_create_updates_the_bar(
    qapp, settings, repos
):
    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)
    dlg.clone_panel.repo_picker.select(str(repos["beta"]))

    assert dlg.identity_bar.repo_name.text() == "forks\\beta"
    assert dlg.identity_bar.repo_branch.text() == "feature/login"


def test_switching_branch_on_the_commit_tab_updates_the_bar(qapp, settings, repos):
    from git_assistant.ui.settings_dialog import SettingsDialog

    _git(repos["alpha"], "branch", "release/1.0")
    dlg = SettingsDialog(settings)
    assert dlg.identity_bar.repo_branch.text() == "main"
    combo = dlg.commit_panel.branch_combo

    combo.setCurrentIndex(combo.findData("release/1.0"))

    assert git_ops.current_branch(repos["alpha"]) == "release/1.0"
    assert dlg.identity_bar.repo_branch.text() == "release/1.0"
