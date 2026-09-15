"""Which submodule on one side is the same one on the other, and whether they agree."""

import pytest

from git_assistant import git_ops
from git_assistant import submodule_compare as compare
from git_assistant.git_ops import CommitSummary, SubmoduleState
from git_assistant.submodule_compare import Match, Row, Showing


def commit(hash_):
    return CommitSummary(hash_, hash_[:7], f"subject {hash_}", "2026-01-01 00:00", "A", "2026-01-01 00:00")


def state(path, *, url="", recorded="aaa", checked_out="same", problem=""):
    """A submodule at ``recorded``, checked out at ``checked_out`` (the same unless said)."""
    at = recorded if checked_out == "same" else checked_out
    return SubmoduleState(
        path=path,
        name=path,
        url=url,
        recorded_hash=recorded,
        recorded=commit(recorded) if recorded and not problem else None,
        checked_out=commit(at) if at and not problem else None,
        problem=problem,
    )


# ---- one source, however it is written ------------------------------------------------
@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("git@github.com:ONEoo7/can.git", "https://github.com/ONEoo7/can"),
        ("ssh://git@gitlab.example.com:2222/team/can.git", "git@gitlab.example.com:team/can"),
        ("https://user@host.example.com/team/can.git/", "https://HOST.example.com/Team/Can"),
        ("../can.git", "../can"),
        # A drive letter is not a host: both are the folder on disk.
        (r"C:\repos\can", "C:/repos/can"),
    ],
)
def test_one_source_is_one_source_however_it_is_written(first, second):
    assert compare.source_of(first) == compare.source_of(second)


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("git@github.com:ONEoo7/can.git", "git@github.com:ONEoo7/ccp.git"),
        ("https://github.com/ONEoo7/can", "https://gitlab.com/ONEoo7/can"),
        (r"C:\repos\can", "D:/repos/can"),
    ],
)
def test_different_sources_stay_different(first, second):
    assert compare.source_of(first) != compare.source_of(second)


# ---- pairing ----------------------------------------------------------------------------
def test_the_same_path_on_both_sides_is_one_row():
    rows = compare.pair([state("libs/can"), state("libs/ccp")], [state("libs/ccp"), state("libs/can")])

    assert [(row.left.path, row.right.path) for row in rows] == [
        ("libs/can", "libs/can"),
        ("libs/ccp", "libs/ccp"),
    ]


def test_one_side_only_is_a_row_with_nothing_on_the_other():
    rows = compare.pair([state("libs/can"), state("libs/left")], [state("libs/can"), state("libs/right")])

    assert [(row.path, row.match(Showing.CHECKED_OUT)) for row in rows] == [
        ("libs/can", Match.SAME),
        ("libs/left", Match.ONLY_LEFT),
        ("libs/right", Match.ONLY_RIGHT),
    ]


def test_a_submodule_kept_somewhere_else_is_paired_by_where_it_is_fetched_from():
    rows = compare.pair(
        [state("libs/can", url="git@github.com:team/can.git", recorded="aaa")],
        [state("modules/CAN", url="https://github.com/team/can", recorded="bbb")],
    )

    [row] = rows
    assert (row.left.path, row.right.path) == ("libs/can", "modules/CAN")
    assert row.match(Showing.CHECKED_OUT) is Match.DIFFERENT


def test_a_source_two_submodules_share_on_one_side_pairs_neither():
    """Either could be the one; a wrong guess reports a difference that is not there."""
    url = "https://github.com/team/can"
    rows = compare.pair(
        [state("a/can", url=url), state("b/can", url=url)],
        [state("modules/can", url=url)],
    )

    assert sorted(row.match(Showing.CHECKED_OUT).value for row in rows) == [
        "only left",
        "only left",
        "only right",
    ]


def test_a_path_is_paired_before_a_source_is():
    """The same place is the same submodule, even fetched from somewhere else now."""
    rows = compare.pair(
        [state("libs/can", url="https://github.com/team/can")],
        [
            state("libs/can", url="https://mirror.example.com/team/can"),
            state("modules/can", url="https://github.com/team/can"),
        ],
    )

    assert [(row.path, row.left is not None, row.right is not None) for row in rows] == [
        ("libs/can", True, True),
        ("modules/can", False, True),
    ]


def test_rows_are_in_order_of_path_with_nested_submodules_after_their_own():
    names = ["libs/can-x", "libs/can/sub", "libs/Can", "a"]
    rows = compare.pair([state(name) for name in names], [])

    assert [row.path for row in rows] == ["a", "libs/Can", "libs/can/sub", "libs/can-x"]


# ---- agreeing ---------------------------------------------------------------------------
def test_checked_out_and_recorded_are_compared_separately():
    left = state("libs/can", recorded="aaa")
    right = state("libs/can", recorded="bbb", checked_out="aaa")
    row = Row(left, right)

    assert row.match(Showing.CHECKED_OUT) is Match.SAME
    assert row.match(Showing.RECORDED) is Match.DIFFERENT


def test_a_submodule_never_checked_out_is_compared_at_what_is_recorded():
    left = state("libs/can", recorded="aaa")
    right = state("libs/can", recorded="aaa", checked_out="", problem=git_ops.NOT_CHECKED_OUT)

    assert Row(left, right).match(Showing.CHECKED_OUT) is Match.SAME
    assert compare.shown_commit(right, Showing.CHECKED_OUT) is None


def test_a_checkout_git_could_not_read_is_never_the_same():
    left = state("libs/can", recorded="aaa")
    right = state("libs/can", recorded="aaa", problem="fatal: bad object HEAD")

    assert Row(left, right).match(Showing.CHECKED_OUT) is Match.DIFFERENT
    assert Row(left, right).match(Showing.RECORDED) is Match.SAME
    assert Row(right, right).match(Showing.CHECKED_OUT) is Match.DIFFERENT, "two unknowns"


def test_the_details_shown_are_of_the_commit_compared():
    moved = state("libs/can", recorded="aaa", checked_out="bbb")

    assert compare.shown_commit(moved, Showing.CHECKED_OUT).hash == "bbb"
    assert compare.shown_commit(moved, Showing.RECORDED).hash == "aaa"


def test_one_repository_on_both_sides_is_read_once(tmp_path, monkeypatch):
    read = []
    monkeypatch.setattr(git_ops, "submodule_states", lambda repo: read.append(repo) or [])

    assert compare.read_both(str(tmp_path), str(tmp_path) + "/") == ([], [])
    assert len(read) == 1
