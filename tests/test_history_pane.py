"""The history graph on the Commit tab, drawn from a real repository."""

import subprocess
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QUrl  # noqa: E402
from PyQt6.QtGui import QGuiApplication  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMenu  # noqa: E402

from git_assistant import git_ops  # noqa: E402
from git_assistant.ui import history_pane as pane_module  # noqa: E402
from git_assistant.ui.history_pane import (  # noqa: E402
    AUTHOR,
    COMMIT_INDEX,
    COMMIT_ROLE,
    DATE,
    HASH,
    MESSAGE,
    WORKING_DIRECTORY,
    HistoryPane,
    initials,
    relative_date,
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(cwd, *args, env=None):
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


@pytest.fixture(autouse=True)
def git_home(tmp_path, monkeypatch):
    config = tmp_path / "global.gitconfig"
    config.write_text(
        "[user]\n\tname = Bogdan Taloi\n\temail = bogdan@example.com\n"
        "[init]\n\tdefaultBranch = master\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def inline(monkeypatch):
    """Read on the test's own thread, so the rows are there when the call returns."""
    monkeypatch.setattr(pane_module, "run_worker", lambda worker: worker.run())


def _commit(repo, message, *, author=None):
    (repo / "a.txt").write_text(message, encoding="utf-8")
    _git(repo, "add", "-A")
    args = ["commit", "-q", "-m", message]
    if author:
        args += [f"--author={author}"]
    _git(repo, *args)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """first, second (tagged v1.0, by someone else), third on master; a side branch off first."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    first = _commit(path, "first")
    second = _commit(path, "second\n\nwhy it was done", author="Denis Anitei <denis@example.com>")
    _git(path, "tag", "v1.0", second)
    third = _commit(path, "third")
    _git(path, "branch", "side", first)
    _git(path, "checkout", "-q", "side")
    side = _commit(path, "side work")
    _git(path, "checkout", "-q", "master")
    return {"path": path, "first": first, "second": second, "third": third, "side": side}


@pytest.fixture
def pane(qapp, repo):
    shown = HistoryPane()
    shown.resize(1000, 600)
    shown.show()
    shown.show_repo(str(repo["path"]))
    yield shown
    shown.close()


def _item(pane, full):
    return pane._item_for(full)


def _texts(pane, index):
    item = pane.tree.topLevelItem(index)
    return [item.text(column) for column in (MESSAGE, AUTHOR, DATE, HASH)]


def test_every_commit_is_a_row_under_two_for_what_is_not_committed(pane, repo):
    tree = pane.tree
    assert tree.topLevelItemCount() == 2 + 4
    commits = [tree.topLevelItem(i).data(MESSAGE, COMMIT_ROLE) for i in range(2, 6)]
    assert {commit.hash for commit in commits} == {repo[k] for k in ("first", "second", "third", "side")}
    third = _item(pane, repo["third"])
    assert [third.text(c) for c in (MESSAGE, AUTHOR, HASH)] == ["third", "Bogdan Taloi", repo["third"][:7]]
    assert third.text(DATE).endswith("ago")
    assert pane.note.text() == "4 commits"


def test_the_rows_above_the_commits_count_what_is_not_committed(pane):
    pane.show_worktree(3, 1)

    assert _texts(pane, 0)[0] == "3 file(s) with unstaged changes"
    assert _texts(pane, 1)[0] == "1 file(s) staged"
    pane.show_worktree(0, 0)
    assert (_texts(pane, 0)[0], _texts(pane, 1)[0]) == ("no unstaged changes", "nothing staged")


def test_head_is_selected_and_told_in_full_underneath(pane, repo):
    assert pane.current_hash() == repo["third"]
    text = pane.details.text.toPlainText()
    assert repo["third"] in text
    assert "Bogdan Taloi <bogdan@example.com>" in text
    assert "Branches here:" in text and "master" in text


def test_a_parent_can_be_followed_from_the_details(pane, repo):
    pane.details.text.anchorClicked.emit(QUrl(f"commit:{repo['second']}"))

    assert pane.current_hash() == repo["second"]
    text = pane.details.text.toPlainText()
    assert "why it was done" in text
    assert "Tags here:\nv1.0" in text  # a table, a cell a line in plain text


def test_the_selected_author_s_commits_are_marked(pane, repo):
    pane.select_commit(repo["second"])
    tree = pane.tree

    def marked(full):
        return tree.by_highlighted_author(tree.indexFromItem(_item(pane, full), MESSAGE))

    assert marked(repo["second"]) is True
    assert marked(repo["third"]) is False
    pane.select_commit(repo["third"])
    assert marked(repo["first"]) is True and marked(repo["second"]) is False


def test_what_contains_a_commit_is_looked_up_once_it_is_selected(qapp, pane, repo):
    import time

    pane.select_commit(repo["first"])
    assert "Looking up" in pane.details.text.toPlainText()

    deadline = time.monotonic() + 10
    while "Contained in branches" not in pane.details.text.toPlainText():
        assert time.monotonic() < deadline, "never looked up"
        qapp.processEvents()
        time.sleep(0.02)

    text = pane.details.text.toPlainText()
    assert "Contained in branches: master, side" in text
    assert "Contained in tags: v1.0" in text
    assert "Derives from tag: none" in text


def test_an_answer_about_a_commit_no_longer_selected_is_dropped(pane, repo):
    pane.select_commit(repo["first"])
    pane._on_looked_up((pane._looked_up - 1, ["elsewhere"], ["nothing"], "old"))

    assert "elsewhere" not in pane.details.text.toPlainText()


def test_the_checked_out_branch_alone(pane, repo):
    pane.all_branches.setChecked(False)

    hashes = {
        pane.tree.topLevelItem(i).data(MESSAGE, COMMIT_ROLE).hash
        for i in range(2, pane.tree.topLevelItemCount())
    }
    assert hashes == {repo["first"], repo["second"], repo["third"]}


def test_a_history_read_for_an_earlier_request_is_dropped(pane):
    rows = pane.tree.topLevelItemCount()

    pane._on_read((pane._asked - 1, [], ""))

    assert pane.tree.topLevelItemCount() == rows


def test_nothing_is_read_while_the_pane_is_off_screen(qapp, repo, monkeypatch):
    read = []
    real = git_ops.commit_log
    monkeypatch.setattr(git_ops, "commit_log", lambda *a, **k: read.append(a) or real(*a, **k))
    hidden = HistoryPane()

    hidden.show_repo(str(repo["path"]))
    hidden.show_repo(str(repo["path"]))
    assert read == []

    hidden.show()
    for _ in range(3):
        qapp.processEvents()
    assert len(read) == 1
    assert hidden.tree.topLevelItemCount() == 6
    hidden.close()


def test_the_pending_rows_say_what_they_are_when_selected(pane):
    pane.show_worktree(2, 0)
    pane.tree.setCurrentItem(pane.tree.topLevelItem(0))

    assert WORKING_DIRECTORY in pane.details.text.toPlainText()
    assert "2 file(s) with unstaged changes" in pane.details.text.toPlainText()
    pane.tree.setCurrentItem(pane.tree.topLevelItem(1))
    assert COMMIT_INDEX in pane.details.text.toPlainText()


def test_the_graph_draws_every_row(pane):
    """Every delegate paints: a crash in one is a window that goes blank."""
    image = pane.tree.grab()

    assert not image.isNull()


def test_a_selected_row_is_written_in_the_text_colour_on_this_style(pane):
    """The Windows 11 style marks a selected row with a shade of the list's own
    background, and the palette's highlighted text -- white -- vanished into it."""
    from PyQt6.QtGui import QColor, QPalette
    from PyQt6.QtWidgets import QStyle, QStyleOptionViewItem

    option = QStyleOptionViewItem()
    option.widget = pane.tree
    # As the light theme has it: black text, white highlighted text.
    palette = QPalette(pane.tree.palette())
    palette.setColor(QPalette.ColorRole.Text, QColor("black"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    option.palette = palette
    option.state = QStyle.StateFlag.State_Selected
    delegate = pane.tree.itemDelegateForColumn(MESSAGE)
    if pane.tree.style().name() != "windows11":
        pytest.skip("only the Windows 11 style shades a selection rather than filling it")

    assert delegate._text_colour(option) == option.palette.color(QPalette.ColorRole.Text)


class _Clipboard:
    """The clipboard is the whole desktop's: another process holding it open makes a
    real copy fail quietly, which the suite run four at a time is."""

    def __init__(self):
        self.text = ""

    def setText(self, text):  # noqa: N802 - Qt naming
        self.text = text


def test_a_commit_hash_can_be_copied(pane, repo, monkeypatch):
    item = _item(pane, repo["second"])
    clipboard = _Clipboard()
    monkeypatch.setattr(QGuiApplication, "clipboard", staticmethod(lambda: clipboard))
    monkeypatch.setattr(QMenu, "exec", lambda menu, *a, **k: menu.actions()[0])

    pane._on_menu(pane.tree.visualItemRect(item).center())

    assert clipboard.text == repo["second"]


@pytest.mark.parametrize(
    ("seconds", "said"),
    [
        (5, "5 seconds ago"),
        (60, "1 minute ago"),
        (7200, "2 hours ago"),
        (86400 * 15, "15 days ago"),
        (86400 * 45, "1 month ago"),
        (86400 * 364, "11 months ago"),
        (86400 * 800, "2 years ago"),
    ],
)
def test_a_date_is_said_as_how_long_ago(seconds, said):
    assert relative_date(1_000_000_000, now=1_000_000_000 + seconds) == said


@pytest.mark.parametrize(
    ("name", "letters"),
    [("bogdan.taloi", "BT"), ("Denis Anitei", "DA"), ("stefan", "S"), ("", "?")],
)
def test_initials(name, letters):
    assert initials(name) == letters
