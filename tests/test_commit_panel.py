"""Regression tests for the commit panel's repository state."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui.preview_dialog import NO_REPOS_MESSAGE, CommitPanel  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    return s


def test_warns_when_no_repositories(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)
    assert panel.status.text() == NO_REPOS_MESSAGE
    assert not panel.regen_btn.isEnabled()


def test_warning_clears_once_repositories_are_added(qapp, settings):
    """A fresh install starts with no repos; adding some must clear the notice.

    Reproduces: install clean, add repos via 'Add folder', return to the tab and
    the stale "No repositories configured" text was still shown above a
    perfectly populated repository selector.
    """
    panel = CommitPanel(settings, auto_start=False)
    assert panel.status.text() == NO_REPOS_MESSAGE

    settings.repos = [RepoEntry("/x/a", owner="ONEoo7")]
    settings.active_repo = "/x/a"
    panel.refresh_repos()

    assert panel.repo_picker.count() == 1
    assert panel.status.text() == ""
    assert panel.regen_btn.isEnabled()


def test_refresh_keeps_a_generation_result(qapp, settings):
    """Switching tabs refreshes the panel; that must not wipe the last result."""
    settings.repos = [RepoEntry("/x/a", owner="ONEoo7")]
    settings.active_repo = "/x/a"
    panel = CommitPanel(settings, auto_start=False)

    panel.status.setText("Strategy: single-shot - ~500 input tokens")
    panel.refresh_repos()
    assert panel.status.text() == "Strategy: single-shot - ~500 input tokens"


# ---- the Refresh button -----------------------------------------------------
# Staging happens outside this window, and nothing here notices it. Refresh
# re-reads it and drops the message written for the previous set of changes.


def _repo(tmp_path):
    """A real repo, since Refresh re-runs `git diff` against one."""
    import subprocess
    import sys

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    d = tmp_path / "repo"
    d.mkdir()
    for args in (["init"], ["config", "user.email", "t@e.example"], ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(d), *args], capture_output=True, creationflags=flags)
    return d


def test_refresh_clears_the_generated_message(qapp, settings, tmp_path):
    repo = _repo(tmp_path)
    settings.repos = [RepoEntry(str(repo))]
    settings.active_repo = str(repo)
    panel = CommitPanel(settings, auto_start=False)

    panel.editor.setPlainText("feat: a message about changes that are no longer staged")
    panel.regen_btn.setText("Regenerate")

    panel.refresh_btn.click()

    assert panel.editor.toPlainText() == ""
    # Nothing left to regenerate, so the button says what it now does.
    assert panel.regen_btn.text() == "Generate"


def test_refresh_picks_up_files_staged_outside_the_window(qapp, settings, tmp_path):
    import subprocess
    import sys

    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    repo = _repo(tmp_path)
    settings.repos = [RepoEntry(str(repo))]
    settings.active_repo = str(repo)
    panel = CommitPanel(settings, auto_start=False)
    assert panel.file_list.count() == 0

    (repo / "new.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "new.py"],
        capture_output=True,
        creationflags=flags,
    )

    panel.refresh_btn.click()

    assert panel.file_list.count() == 1


def test_refresh_is_disabled_while_generating(qapp, settings, tmp_path):
    """It clears the widgets a running generation is about to fill."""
    repo = _repo(tmp_path)
    settings.repos = [RepoEntry(str(repo))]
    settings.active_repo = str(repo)
    panel = CommitPanel(settings, auto_start=False)

    panel._set_busy(True)
    assert not panel.refresh_btn.isEnabled()
    panel._set_busy(False)
    assert panel.refresh_btn.isEnabled()


# ---- submodules are nested under the repository that contains them ----------
def _rows(picker):
    """(label, depth) for every row of the picker, top to bottom."""
    out = []

    def rec(item, depth):
        out.append((item.text(0), depth))
        for i in range(item.childCount()):
            rec(item.child(i), depth + 1)

    tree = picker.repo_list
    for i in range(tree.topLevelItemCount()):
        rec(tree.topLevelItem(i), 0)
    return out


def test_picker_nests_submodules_under_their_repo(qapp, settings):
    settings.repos = [
        RepoEntry("/x/alpha"),
        RepoEntry("/x/alpha/libs/inner"),
        RepoEntry("/x/beta"),
    ]
    settings.active_repo = "/x/alpha"
    panel = CommitPanel(settings, auto_start=False)

    assert _rows(panel.repo_picker) == [("alpha", 0), ("inner", 1), ("beta", 0)]
    # A submodule is a repository in its own right, so it counts as one.
    assert panel.repo_picker.count() == 3


def test_picker_can_select_a_submodule(qapp, settings):
    settings.repos = [RepoEntry("/x/alpha"), RepoEntry("/x/alpha/libs/inner")]
    settings.active_repo = "/x/alpha/libs/inner"
    panel = CommitPanel(settings, auto_start=False)

    assert panel.repo_picker.current_path() == "/x/alpha/libs/inner"


def test_picker_filter_keeps_the_parent_of_a_matching_submodule(qapp, settings):
    settings.repos = [
        RepoEntry("/x/alpha"),
        RepoEntry("/x/alpha/libs/inner"),
        RepoEntry("/x/beta"),
    ]
    settings.active_repo = "/x/alpha"
    panel = CommitPanel(settings, auto_start=False)
    tree = panel.repo_picker.repo_list

    panel.repo_picker.filter_edit.setText("inner")

    alpha = tree.topLevelItem(0)
    assert not alpha.isHidden()  # a match must not be stranded out of its tree
    assert not alpha.child(0).isHidden()
    assert tree.topLevelItem(1).isHidden()  # beta matches nothing
