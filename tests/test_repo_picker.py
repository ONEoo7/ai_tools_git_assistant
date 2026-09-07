"""The branch each repository is on, shown beside its name in the picker.

Against real repositories: the branch is read out of ``.git`` rather than asked
of ``git``, and a stub for the one thing that writes that file would only be
testing this file's idea of what git writes.
"""

import os
import subprocess
import sys

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication, QStyleOptionViewItem  # noqa: E402

from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui.repo_picker import (  # noqa: E402
    ALL_GROUP,
    BRANCH_ROLE,
    RepoPicker,
)

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
    """A repository with one commit, on ``branch``."""
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "f.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "initial")
    _git(path, "branch", "-M", branch)
    return path


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def repos(tmp_path):
    return {
        "alpha": _repo(tmp_path / "alpha", "main"),
        "beta": _repo(tmp_path / "beta", "release/2026.09"),
    }


@pytest.fixture
def settings(repos):
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [RepoEntry(str(p)) for p in repos.values()]
    s.active_repo = str(repos["alpha"])
    return s


def _branches(picker):
    """The branch shown against each repository name, by name."""
    return {
        item.text(0): item.data(0, BRANCH_ROLE)
        for item in picker._items()
        if item.data(0, Qt.ItemDataRole.UserRole)
    }


def test_every_repository_is_labelled_with_its_branch(qapp, settings, repos):
    picker = RepoPicker(settings)

    assert _branches(picker) == {"alpha": "main", "beta": "release/2026.09"}


def test_the_branch_is_in_the_tooltip_too(qapp, settings, repos):
    """It is elided out of a narrow list before the name is; the tooltip keeps it."""
    picker = RepoPicker(settings)

    tips = [
        item.toolTip(0)
        for item in picker._items()
        if item.data(0, Qt.ItemDataRole.UserRole) == str(repos["beta"])
    ]

    assert tips and all(
        tip == f"{repos['beta']}\nOn branch release/2026.09" for tip in tips
    )


def test_a_repository_that_is_not_on_a_branch_is_labelled_with_nothing(
    qapp, settings, repos
):
    """A detached HEAD, and a repository that has been moved or deleted."""
    _git(repos["alpha"], "checkout", "-q", "--detach", "HEAD")
    settings.repos.append(RepoEntry(str(repos["alpha"].parent / "gone")))

    picker = RepoPicker(settings)

    assert _branches(picker) == {"alpha": "", "beta": "release/2026.09", "gone": ""}


def test_a_checkout_is_picked_up_without_rebuilding_the_list(qapp, settings, repos):
    """`refresh_branches` is what the tabs call after a checkout.

    It must not rebuild: `refresh` would take the scroll position and whatever
    the user had folded open with it, for a change to one label.
    """
    picker = RepoPicker(settings)
    rows = list(picker._items())

    _git(repos["alpha"], "checkout", "-q", "-b", "dev/rem/sg/thing")
    picker.refresh_branches()

    assert _branches(picker)["alpha"] == "dev/rem/sg/thing"
    # The same row objects, so nothing about the tree itself was disturbed.
    assert [id(row) for row in picker._items()] == [id(row) for row in rows]


def test_the_filter_matches_repository_names_and_not_branches(qapp, settings, repos):
    """The box says "Filter repositories", and a branch is an annotation.

    A branch that matched here would hide repositories for a reason that is not
    on screen -- and every repository on `main` would answer to "mai".
    """
    picker = RepoPicker(settings)

    picker.filter_edit.setText("release")

    shown = [
        item.text(0)
        for item in picker._items()
        if item.data(0, Qt.ItemDataRole.UserRole) and not item.isHidden()
    ]
    assert shown == ["alpha"]  # the selected one, which stays visible regardless


def test_the_row_reserves_room_for_the_branch(qapp, settings, repos):
    """The branch is drawn after the name, so it has to be measured with it."""
    picker = RepoPicker(settings)
    delegate = picker.repo_list.itemDelegate()
    model = picker.repo_list.model()
    group = next(
        i
        for i in range(picker.repo_list.topLevelItemCount())
        if picker.repo_list.topLevelItem(i).text(0) == ALL_GROUP
    )
    index = model.index(0, 0, model.index(group, 0))
    option = QStyleOptionViewItem()
    option.initFrom(picker.repo_list)

    with_branch = delegate.sizeHint(option, index)
    model.setData(index, "", BRANCH_ROLE)
    without = delegate.sizeHint(option, index)

    assert with_branch.width() > without.width()
