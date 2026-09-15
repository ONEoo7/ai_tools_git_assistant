"""The Commit tab's remotes: listing them, adding and removing, choosing what is tracked."""

import subprocess
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from git_assistant import git_ops  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui.remotes_page import TRACKED_MARK, RemotesPage  # noqa: E402

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

ORIGIN = "https://ONEoo7@github.com/ONEoo7/thing.git"
WORK = "git@gitlab.example.com:team/thing.git"


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """main and feature, two remotes, and nothing from the machine's git config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-system"))
    work = tmp_path / "ONEoo7" / "thing"
    work.mkdir(parents=True)
    _git(work, "init", "-q", "--initial-branch=main")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "initial")
    _git(work, "branch", "feature")
    _git(work, "remote", "add", "origin", ORIGIN)
    _git(work, "remote", "add", "work", WORK)
    git_ops.set_tracking_remote(work, "main", "origin")
    return work


@pytest.fixture
def page(qapp, repo):
    page = RemotesPage()
    page.show_repo(str(repo))
    return page


def _rows(page):
    return [page.remote_list.item(i).text() for i in range(page.remote_list.count())]


def _select(page, name):
    page.remote_list.setCurrentRow(page.remotes().index(name))


def _heard(page):
    heard = []
    page.remotesChanged.connect(lambda: heard.append(True))
    return heard


# ---- what it shows -------------------------------------------------------------------
# ---- what it shows -------------------------------------------------------------------
def test_showing_a_repository_is_one_git_command(page, repo, monkeypatch):
    """The remotes and the one tracked come from one read; it runs on every tab switch."""
    launched = []
    real = subprocess.run

    def run(argv, *args, **kwargs):
        if "-C" in argv:
            launched.append(argv[argv.index("-C") + 2 :])
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(git_ops.subprocess, "run", run)

    page.show_repo(str(repo))

    assert launched == [["config", "--list", "-z", "--show-scope"]]
    assert page.tracked_remote() == "origin"


def test_it_lists_the_remotes_with_the_tracked_one_marked(page):
    assert page.remotes() == ["origin", "work"]
    assert _rows(page) == [f"origin  ({TRACKED_MARK})", "work"]
    assert page.remote_list.item(0).font().bold()
    assert not page.remote_list.item(1).font().bold()
    assert page.tracking_label.text() == "'main' tracks origin."


def test_the_selected_remote_says_where_it_is(page):
    _select(page, "work")

    assert page.url_label.text() == WORK
    assert page.remote_list.item(1).toolTip() == WORK


def test_the_tracked_one_is_selected_to_begin_with_and_needs_no_tracking(page):
    assert page.selected_remote() == "origin"
    assert not page.track_btn.isEnabled()
    assert page.remove_btn.isEnabled()


def test_a_branch_tracking_nothing_says_so(qapp, repo):
    _git(repo, "switch", "-q", "feature")
    page = RemotesPage()

    page.show_repo(str(repo))

    assert page.tracked_remote() == ""
    assert "'feature' tracks no remote" in page.tracking_label.text()
    assert page.track_btn.isEnabled()


def test_on_a_detached_head_there_is_no_branch_to_track_a_remote(qapp, repo):
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    page = RemotesPage()

    page.show_repo(str(repo))

    assert "Not on a branch" in page.tracking_label.text()
    assert not page.track_btn.isEnabled()


def test_with_no_repository_there_is_nothing_to_change(qapp):
    page = RemotesPage()

    assert page.tracking_label.text() == "No repository selected."
    assert not page.isEnabled()


# ---- choosing what is tracked --------------------------------------------------------
def test_track_makes_the_branch_track_the_selected_remote(page, repo):
    heard = _heard(page)
    _select(page, "work")

    page.track_btn.click()

    assert git_ops.tracking_remote(repo, "main") == "work"
    assert _rows(page) == ["origin", f"work  ({TRACKED_MARK})"]
    assert page.tracking_label.text() == "'main' tracks work."
    assert page.selected_remote() == "work"
    assert not page.track_btn.isEnabled()
    assert heard == [True]


# ---- adding ------------------------------------------------------------------------
def test_a_remote_is_added_listed_selected_and_the_boxes_emptied(page, repo):
    heard = _heard(page)
    page.name_edit.setText("upstream")
    page.url_edit.setText("https://github.com/someone/thing.git")

    page.add_btn.click()

    assert git_ops.Remote(
        "upstream", "https://github.com/someone/thing.git"
    ) in git_ops.list_remotes(repo)
    assert page.remotes() == ["origin", "upstream", "work"]
    assert page.selected_remote() == "upstream"
    assert (page.name_edit.text(), page.url_edit.text()) == ("", "")
    assert page.problem_label.text() == ""
    assert page.problem_label.isHidden()  # and leaves no gap
    assert heard == [True]


def test_enter_in_either_box_adds_it_too(page, repo):
    page.name_edit.setText("upstream")
    page.url_edit.setText("https://github.com/someone/thing.git")

    page.url_edit.returnPressed.emit()

    assert "upstream" in page.remotes()


@pytest.mark.parametrize(
    "name, url, said",
    [
        ("", "https://example.com/x.git", "Give the remote a name."),
        ("origin", "https://example.com/x.git", "There is already a remote called 'origin'."),
        ("two words", "https://example.com/x.git", "'two words' is not a name git accepts"),
        ("upstream", "", "Give the remote's URL."),
    ],
)
def test_what_cannot_be_added_is_explained_and_nothing_changes(page, repo, name, url, said):
    heard = _heard(page)
    page.name_edit.setText(name)
    page.url_edit.setText(url)

    page.add_btn.click()

    assert page.problem_label.text().startswith(said)
    assert not page.problem_label.isHidden()
    assert [r.name for r in git_ops.list_remotes(repo)] == ["origin", "work"]
    assert page.name_edit.text() == name  # kept, to be corrected
    assert heard == []


# ---- removing ----------------------------------------------------------------------
def test_removing_asks_first_and_cancelling_keeps_it(page, repo, monkeypatch):
    asked = []

    def cancel(*args):
        asked.append(args[2])
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", cancel)
    heard = _heard(page)
    _select(page, "work")

    page.remove_btn.click()

    assert "Remove the remote 'work'" in asked[0]
    assert "Nothing on the server is changed" in asked[0]
    assert [r.name for r in git_ops.list_remotes(repo)] == ["origin", "work"]
    assert heard == []


def test_removing_the_tracked_remote_leaves_the_branch_tracking_nothing(
    page, repo, monkeypatch
):
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes
    )
    heard = _heard(page)
    _select(page, "origin")

    page.remove_btn.click()

    assert page.remotes() == ["work"]
    assert git_ops.tracking_remote(repo, "main") == ""
    assert "'main' tracks no remote" in page.tracking_label.text()
    assert heard == [True]


# ---- in the Commit tab ---------------------------------------------------------------
@pytest.fixture
def settings(repo):
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [RepoEntry(str(repo))]
    s.active_repo = str(repo)
    return s


def test_the_commit_tab_shows_the_selected_repositorys_remotes(qapp, settings):
    from git_assistant.ui.preview_dialog import CommitPanel

    panel = CommitPanel(settings, auto_start=False)

    assert panel.remotes_page.remotes() == ["origin", "work"]
    assert panel.remotes_page.tracked_remote() == "origin"


def test_switching_branch_shows_what_the_new_branch_tracks(qapp, settings, repo):
    from git_assistant.ui.preview_dialog import CommitPanel

    git_ops.set_tracking_remote(repo, "feature", "work")
    panel = CommitPanel(settings, auto_start=False)

    panel._on_branch_chosen("feature")

    assert panel.remotes_page.tracking_label.text() == "'feature' tracks work."


def test_a_first_push_is_confirmed_naming_the_remote_it_goes_to(
    qapp, settings, repo, monkeypatch
):
    from git_assistant.ui.preview_dialog import CommitPanel

    git_ops.set_tracking_remote(repo, "main", "work")
    asked = []

    def cancel(*args):
        asked.append(args[2])
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", cancel)
    panel = CommitPanel(settings, auto_start=False)

    panel._on_push()

    assert "a new upstream branch on 'work'" in asked[0]


def test_choosing_another_remote_to_track_is_named_in_the_bar(qapp, settings):
    from git_assistant.ui.settings_dialog import SettingsDialog

    dlg = SettingsDialog(settings)
    assert dlg.identity_bar.auth_status.text() == "origin (github.com as ONEoo7)"
    page = dlg.commit_panel.remotes_page
    _select(page, "work")

    page.track_btn.click()

    assert dlg.identity_bar.auth_status.text().startswith("work (gitlab.example.com")


def test_a_checkout_names_the_remote_the_new_branch_pushes_to(qapp, settings, repo):
    from git_assistant.ui.settings_dialog import SettingsDialog

    git_ops.set_tracking_remote(repo, "feature", "work")
    dlg = SettingsDialog(settings)

    dlg.commit_panel._on_branch_chosen("feature")

    assert dlg.identity_bar.auth_status.text().startswith("work (")
