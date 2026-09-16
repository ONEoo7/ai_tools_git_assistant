"""Which lane each commit of a history takes, and which lines run through its row."""

from git_assistant.commit_graph import Line, lay_out


def _lanes(rows):
    return [row.lane for row in rows]


def test_a_straight_history_is_one_lane_one_colour():
    rows = lay_out([("c", ["b"]), ("b", ["a"]), ("a", [])])

    assert _lanes(rows) == [0, 0, 0]
    assert rows[0].above == () and rows[0].below == (Line(0, 0, 0),)
    assert rows[1].above == (Line(0, 0, 0),) and rows[1].below == (Line(0, 0, 0),)
    assert rows[2].above == (Line(0, 0, 0),) and rows[2].below == ()


def test_a_merge_opens_a_lane_for_its_second_parent_and_the_fork_closes_it():
    rows = lay_out(
        [
            ("merge", ["main2", "feature"]),
            ("feature", ["base"]),
            ("main2", ["base"]),
            ("base", ["root"]),
            ("root", []),
        ]
    )

    assert _lanes(rows) == [0, 1, 0, 0, 0]
    merge, feature, main2, base, root = rows
    assert merge.below == (Line(0, 0, 0), Line(0, 1, 1))
    assert feature.above == (Line(0, 0, 0), Line(1, 1, 1))
    assert feature.colour == 1
    assert main2.below == (Line(1, 1, 1), Line(0, 0, 0))
    # The branch's lane is drawn in to where it forked, and ends there.
    assert base.above == (Line(0, 0, 0), Line(1, 0, 1))
    assert base.below == (Line(0, 0, 0),)
    assert root.lanes == 1


def test_every_parent_of_an_octopus_merge_has_a_lane():
    rows = lay_out([("octopus", ["a", "b", "c"]), ("a", []), ("b", []), ("c", [])])

    assert rows[0].below == (Line(0, 0, 0), Line(0, 1, 1), Line(0, 2, 2))
    assert _lanes(rows) == [0, 0, 1, 2]
    assert rows[0].lanes == 3


def test_a_parent_already_waited_for_is_joined_rather_than_given_a_second_lane():
    rows = lay_out(
        [
            ("tip", ["base"]),
            ("merge", ["other", "base"]),
            ("other", []),
            ("base", []),
        ]
    )

    assert rows[1].lane == 1
    assert Line(1, 0, 0) in rows[1].below
    assert rows[1].lanes == 2


def test_a_lane_left_free_is_the_next_new_branch_s():
    rows = lay_out([("a", []), ("b", ["x"]), ("x", [])])

    assert _lanes(rows) == [0, 0, 0]
    assert [row.colour for row in rows] == [0, 1, 1]


def test_a_lane_freed_left_of_one_still_waiting_is_taken_before_a_new_one():
    rows = lay_out(
        [
            ("tip", ["base"]),
            ("other_tip", ["other"]),
            ("base", []),  # the first lane is free now; the second still waits
            ("late_tip", ["other"]),
            ("other", []),
        ]
    )

    # late_tip takes the freed first lane; other, waited for in both, the leftmost.
    assert _lanes(rows) == [0, 1, 0, 0, 0]
    assert rows[3].lanes == 2


def test_head_under_the_work_in_progress_starts_a_colour_of_its_own():
    """The dashed line down from the work in progress stops being HEAD's colour at HEAD."""
    rows = lay_out(
        [
            ("working", ["index"]),
            ("index", ["head"]),
            ("elsewhere", ["base"]),
            ("head", ["base"]),
            ("base", []),
        ],
        fresh={"head"},
    )

    working, index, elsewhere, head, base = rows
    assert (working.lane, index.lane, elsewhere.lane, head.lane) == (0, 0, 1, 0)
    assert elsewhere.above == (Line(0, 0, 0),)  # the line down to HEAD passes it
    assert head.above[0] == Line(0, 0, 0)
    assert head.colour != working.colour
    assert head.below[-1] == Line(0, 0, head.colour)
    assert base.above == (Line(0, 0, head.colour), Line(1, 0, elsewhere.colour))
