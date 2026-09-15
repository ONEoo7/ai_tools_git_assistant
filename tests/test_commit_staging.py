"""The Commit tab's way into staging: the unstaged count and the window it opens."""

import subprocess
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QHBoxLayout, QPushButton  # noqa: E402

from git_assistant import git_ops  # noqa: E402
from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui import preview_dialog, theme  # noqa: E402
from git_assistant.ui.preview_dialog import CommitPanel  # noqa: E402
from git_assistant.ui.staging_dialog import diff_colours  # noqa: E402

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


# ---- the frame's colour --------------------------------------------------------------
@pytest.fixture
def app(qapp):
    yield qapp
    theme.apply(qapp, theme.SYSTEM)  # never leave it on the next test


def _recoloured(button):
    """Down the middle of `button`, every pixel painted unlike a plain button of the
    same size, text and state -- top edge first -- as its colour, and whether the
    plain button draws a line there rather than the fill just inside it."""
    mine = button.grab().toImage()  # first: it lays out a tab never shown
    plain = QPushButton(button.text(), button.parentWidget())
    plain.setEnabled(button.isEnabled())
    plain.resize(button.size())
    theirs = plain.grab().toImage()
    plain.deleteLater()
    x, height = mine.width() // 2, mine.height()

    def inside(y):
        return y + 1 if y < height // 2 else y - 1

    return [
        (mine.pixelColor(x, y).name(), theirs.pixel(x, y) != theirs.pixel(x, inside(y)))
        for y in range(height)
        if mine.pixel(x, y) != theirs.pixel(x, y)
    ]


def _over_its_own_frame(colour):
    """Its top edge and its bottom one in `colour`, each over the line the style
    drew there, and nothing else: the same native button, recoloured."""
    return [(colour, True)] * 2


def test_the_frame_is_red_while_anything_is_left_unstaged(qapp, repo, settings):
    (repo / "staged.txt").write_bytes(b"A\nb\n")
    _git(repo, "add", "staged.txt")
    (repo / "edited.txt").write_bytes(b"a\nB\n")

    panel = CommitPanel(settings, auto_start=False)

    assert panel.unstaged_btn.text() == "Unstaged Changes (1/2)"
    assert _recoloured(panel.unstaged_btn) == _over_its_own_frame(diff_colours()["-"])


@pytest.mark.parametrize("staged", [True, False], ids=["staged-only", "nothing-changed"])
def test_and_green_once_nothing_is(qapp, repo, settings, staged):
    if staged:
        (repo / "staged.txt").write_bytes(b"A\nb\n")
        _git(repo, "add", "staged.txt")

    panel = CommitPanel(settings, auto_start=False)

    assert _recoloured(panel.unstaged_btn) == _over_its_own_frame(diff_colours()["+"])


def test_refreshing_after_an_edit_elsewhere_turns_it_red(qapp, repo, settings):
    panel = CommitPanel(settings, auto_start=False)
    assert _recoloured(panel.unstaged_btn) == _over_its_own_frame(diff_colours()["+"])
    (repo / "edited.txt").write_bytes(b"changed\n")

    panel.refresh_btn.click()

    assert _recoloured(panel.unstaged_btn) == _over_its_own_frame(diff_colours()["-"])


def test_with_no_repository_the_frame_claims_neither(qapp):
    empty = Settings()
    empty.save = lambda: None

    panel = CommitPanel(empty, auto_start=False)

    assert panel.unstaged_btn.text() == "Unstaged Changes (0/0)"
    assert _recoloured(panel.unstaged_btn) == []


def test_nor_for_a_repository_git_cannot_read(qapp, settings, monkeypatch):
    """Nothing counted is not nothing unstaged."""

    def refuse(_repo):
        raise git_ops.GitError("not a repository")

    monkeypatch.setattr(git_ops, "status_entries", refuse)

    panel = CommitPanel(settings, auto_start=False)

    assert _recoloured(panel.unstaged_btn) == []


@pytest.mark.parametrize("key", [theme.DARK, theme.LIGHT, theme.PONY])
def test_each_theme_gets_its_own_red_on_its_own_frame(app, repo, settings, key):
    """Where the frame is differs by style -- two pixels in, or on the edge for the
    pink stylesheet -- and it is recoloured wherever it is, never drawn twice."""
    theme.apply(app, key)
    (repo / "edited.txt").write_bytes(b"changed\n")

    panel = CommitPanel(settings, auto_start=False)

    assert _recoloured(panel.unstaged_btn) == _over_its_own_frame(diff_colours()["-"])


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
