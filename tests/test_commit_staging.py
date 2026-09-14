"""The Commit tab's way into staging: the unstaged count and the window it opens."""

import subprocess
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QHBoxLayout, QPushButton  # noqa: E402

from git_assistant import git_ops  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui import preview_dialog  # noqa: E402
from git_assistant.ui.preview_dialog import CommitPanel  # noqa: E402

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "core.autocrlf", "false")
    (path / ".gitattributes").write_bytes(b"*.txt text eol=lf\n")
    for name in ("staged.txt", "part.txt", "edited.txt"):
        (path / name).write_bytes(b"a\nb\n")
    (path / "raw.dat").write_bytes(b"a\r\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    return path


@pytest.fixture
def settings(repo):
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [RepoEntry(str(repo))]
    s.active_repo = str(repo)
    return s


# ---- the count -----------------------------------------------------------------------
def test_the_count_is_unstaged_changes_of_all_changed_files(qapp, repo, settings):
    (repo / "staged.txt").write_bytes(b"A\nb\n")
    _git(repo, "add", "staged.txt")
    (repo / "part.txt").write_bytes(b"A\nb\n")
    _git(repo, "add", "part.txt")
    (repo / "part.txt").write_bytes(b"A\nB\n")
    (repo / "edited.txt").write_bytes(b"a\nB\n")
    (repo / "new.txt").write_bytes(b"new\n")

    panel = CommitPanel(settings, auto_start=False)

    # part.txt, edited.txt and new.txt have unstaged work; all four changed.
    assert panel.unstaged_btn.text() == "Unstaged Changes (3/4)"
    assert panel.unstaged_btn.isEnabled()


def test_with_nothing_changed_there_is_nothing_to_open(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)

    assert panel.unstaged_btn.text() == "Unstaged Changes (0/0)"
    assert not panel.unstaged_btn.isEnabled()


def test_staged_changes_alone_still_open_the_window_to_unstage_them(
    qapp, repo, settings
):
    (repo / "staged.txt").write_bytes(b"A\nb\n")
    _git(repo, "add", "staged.txt")

    panel = CommitPanel(settings, auto_start=False)

    assert panel.unstaged_btn.text() == "Unstaged Changes (0/1)"
    assert panel.unstaged_btn.isEnabled()


def test_refresh_counts_changes_made_outside_the_window(qapp, repo, settings):
    panel = CommitPanel(settings, auto_start=False)
    (repo / "edited.txt").write_bytes(b"changed\n")

    panel.refresh_btn.click()

    assert panel.unstaged_btn.text() == "Unstaged Changes (1/1)"


def test_the_count_sits_on_the_staged_files_heading(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)

    rows = [
        layout
        for layout in panel.findChildren(QHBoxLayout)
        if layout.indexOf(panel.files_label) >= 0
    ]
    assert rows
    assert rows[0].indexOf(panel.unstaged_btn) > rows[0].indexOf(panel.files_label)


def test_the_count_waits_while_a_message_is_being_written(qapp, repo, settings):
    (repo / "edited.txt").write_bytes(b"changed\n")
    panel = CommitPanel(settings, auto_start=False)

    panel._set_busy(True)
    assert not panel.unstaged_btn.isEnabled()

    panel._set_busy(False)
    assert panel.unstaged_btn.isEnabled()


def test_normalizing_is_the_staging_windows_now_not_this_tabs(qapp, settings):
    panel = CommitPanel(settings, auto_start=False)

    labels = [button.text() for button in panel.findChildren(QPushButton)]
    assert not any("Normalize" in label for label in labels)


# ---- the window ----------------------------------------------------------------------
def test_closing_the_window_shows_what_it_staged(qapp, repo, settings, monkeypatch):
    (repo / "edited.txt").write_bytes(b"changed\n")
    opened = []

    class Window:
        def __init__(self, path, *, name="", parent=None):
            opened.append((path, name))

        def exec(self):
            git_ops.stage_paths(repo, ["edited.txt"])  # what the user did in it
            return 0

    monkeypatch.setattr(preview_dialog, "StagingDialog", Window)
    panel = CommitPanel(settings, auto_start=False)
    assert panel.file_list.topLevelItemCount() == 0

    panel.unstaged_btn.click()

    assert opened == [(str(repo), RepoEntry(str(repo)).display())]
    assert panel.file_list.topLevelItem(0).text(0) == "edited.txt"
    assert panel.unstaged_btn.text() == "Unstaged Changes (0/1)"
