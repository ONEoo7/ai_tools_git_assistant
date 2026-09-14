"""The branch list behind the Commit tab's "Branch" title: filtered, marked, and
switched from only when a branch is clicked or entered."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant import git_ops  # noqa: E402
from git_assistant.ui.branch_picker import BranchPicker  # noqa: E402
from git_assistant.ui.repo_picker import branch_colour  # noqa: E402

BRANCHES = ["main", "feature/login", "feature/signup", "release/1.0", "fix/typo"]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def picker(qapp):
    made = BranchPicker()
    made.set_branches(BRANCHES, "main")
    return made


@pytest.fixture
def chosen(picker):
    seen = []
    picker.branchChosen.connect(seen.append)
    return seen


def _visible(picker):
    return [
        picker.branch_list.item(row).text()
        for row in range(picker.branch_list.count())
        if not picker.branch_list.item(row).isHidden()
    ]


# ---- what is listed ------------------------------------------------------------------
def test_every_branch_is_listed_in_the_order_given(picker):
    """Most recently committed to first is the order git_ops gives."""
    assert picker.branches() == BRANCHES
    assert _visible(picker) == BRANCHES


def test_the_checked_out_branch_is_marked_and_selected(picker):
    item = picker.item_for("main")

    assert picker.current_branch() == "main"
    assert picker.selected_branch() == "main"
    assert item.font().bold()
    assert item.foreground().color() == branch_colour(picker.branch_list.palette())
    assert not picker.item_for("fix/typo").font().bold()


def test_a_detached_head_is_shown_as_a_state_and_nothing_is_selected(qapp):
    picker = BranchPicker()

    picker.set_branches(["main"], git_ops.DETACHED_HEAD)

    first = picker.branch_list.item(0)
    assert first.text() == "(detached HEAD)"
    assert not first.flags() & Qt.ItemFlag.ItemIsSelectable
    assert picker.branches() == ["main"], "the state is not a branch"
    assert picker.current_branch() == ""
    assert picker.selected_branch() == ""


def test_listing_again_is_not_choosing(picker, chosen):
    picker.set_branches(BRANCHES, "release/1.0")

    assert chosen == []
    assert picker.selected_branch() == "release/1.0"


# ---- the filter ----------------------------------------------------------------------
def test_the_filter_keeps_branches_whose_name_contains_it(picker):
    picker.filter_edit.setText("FEATURE")

    assert _visible(picker) == ["main", "feature/login", "feature/signup"]


def test_the_checked_out_branch_stays_visible_whatever_the_filter(picker):
    """A list that seems to have nothing checked out says something untrue."""
    picker.filter_edit.setText("typo")

    assert _visible(picker) == ["main", "fix/typo"]


def test_clearing_the_filter_shows_everything_again(picker):
    picker.filter_edit.setText("release")
    picker.filter_edit.clear()

    assert _visible(picker) == BRANCHES


def test_the_filter_holds_when_the_list_is_filled_again(picker):
    picker.filter_edit.setText("login")

    picker.set_branches(BRANCHES, "main")

    assert _visible(picker) == ["main", "feature/login"]


# ---- choosing ------------------------------------------------------------------------
def test_clicking_another_branch_chooses_it(picker, chosen):
    picker.branch_list.itemClicked.emit(picker.item_for("release/1.0"))

    assert chosen == ["release/1.0"]


def test_enter_on_a_branch_chooses_it(picker, chosen):
    picker.branch_list.itemActivated.emit(picker.item_for("fix/typo"))

    assert chosen == ["fix/typo"]


def test_clicking_the_checked_out_branch_chooses_nothing(picker, chosen):
    """The second click of a double-click lands here, after the switch."""
    picker.branch_list.itemClicked.emit(picker.item_for("main"))

    assert chosen == []


def test_moving_through_the_list_chooses_nothing(picker, chosen):
    """Arrow keys walk the list; they must not check out every branch passed."""
    for name in ("feature/login", "feature/signup", "release/1.0"):
        picker.branch_list.setCurrentItem(picker.item_for(name))

    assert chosen == []


def test_the_detached_head_row_cannot_be_chosen(qapp):
    picker = BranchPicker()
    picker.set_branches(["main"], git_ops.DETACHED_HEAD)
    seen = []
    picker.branchChosen.connect(seen.append)

    picker.branch_list.itemClicked.emit(picker.branch_list.item(0))

    assert seen == []
