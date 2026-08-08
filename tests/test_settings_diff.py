"""Comparing two sets of settings. No Qt, no files."""

from git_assistant import settings_diff
from git_assistant.settings_diff import State


def test_a_nested_setting_is_one_row_named_by_its_path():
    flat = settings_diff.flatten({"fetch": {"depth": 1, "prune": True}})
    assert flat == {"fetch.depth": 1, "fetch.prune": True}


def test_a_list_is_one_setting_and_not_several():
    """Half a list of globs is not a setting at all."""
    flat = settings_diff.flatten({"ignore": ["*.lock", "*.map"]})
    assert flat == {"ignore": ["*.lock", "*.map"]}


def test_the_schema_version_is_not_a_setting():
    """It is about the file, and a row for it is a row nobody can act on."""
    assert settings_diff.flatten({"version": 1, "fetch": {"depth": 2}}) == {
        "fetch.depth": 2
    }


def test_every_state_is_reported():
    changes = {
        c.key: c
        for c in settings_diff.compare(
            {"a": 1, "b": 2, "gone": 3},
            {"a": 1, "b": 9, "new": 4},
        )
    }

    assert changes["a"].state is State.SAME
    assert changes["b"].state is State.CHANGED
    assert changes["gone"].state is State.REMOVED
    assert changes["new"].state is State.ADDED


def test_the_rows_come_back_in_a_stable_order():
    """Two runs of the same comparison must not shuffle the window."""
    first = settings_diff.compare({"z": 1, "a": 2}, {"a": 2, "z": 3})
    second = settings_diff.compare({"a": 2, "z": 1}, {"z": 3, "a": 2})
    assert [c.key for c in first] == [c.key for c in second] == ["a", "z"]


def test_only_the_differences_are_asked_for_separately():
    changes = settings_diff.differences({"a": 1, "b": 2}, {"a": 1, "b": 3})
    assert [c.key for c in changes] == ["b"]


def test_key_order_and_indentation_are_not_differences():
    """A textual diff would report a change nobody made."""
    before = settings_diff.parse('{"fetch": {"depth": 1, "prune": true}}')
    after = settings_diff.parse('{\n  "fetch": {\n  "prune": true,\n "depth": 1 }\n}')

    assert settings_diff.differences(before, after) == []


def test_a_missing_value_reads_as_a_dash_rather_than_as_none():
    change = settings_diff.differences({}, {"fetch": {"depth": 3}})[0]
    assert change.shown(change.before) == "-"
    assert change.shown(change.after) == "3"


def test_a_boolean_reads_as_json_writes_it():
    """`True` in a window over a file that says `true` invites a wrong edit."""
    change = settings_diff.differences({"a": True}, {"a": False})[0]
    assert (change.shown(change.before), change.shown(change.after)) == (
        "true",
        "false",
    )


def test_a_row_says_what_happened_to_it():
    said = {
        change.key: change.describe()
        for change in settings_diff.differences({"b": 2, "gone": 3}, {"b": 9, "new": 4})
    }
    assert said["b"] == "b: 2 -> 9"
    assert said["new"] == "new: added, 4"
    assert said["gone"] == "gone: removed (was 3)"


def test_the_summary_counts_each_kind():
    changes = settings_diff.differences({"b": 2, "gone": 3}, {"b": 9, "new": 4})
    summary = settings_diff.summarise(changes)
    assert "1 changed" in summary and "1 added" in summary and "1 removed" in summary


def test_nothing_to_report_says_so():
    assert settings_diff.summarise([]) == "No differences."


def test_text_that_is_not_settings_reads_as_nothing_rather_than_raising():
    assert settings_diff.parse("{oops}") == {}
    assert settings_diff.parse("[1, 2]") == {}
    assert settings_diff.parse("") == {}


# ---- building one out of two ---------------------------------------------------------
def test_a_path_goes_back_to_the_shape_it_came_from():
    nested = {"branch": {"pattern": "x"}, "fetch": {"depth": 2, "prune": True}}
    assert settings_diff.unflatten(settings_diff.flatten(nested)) == nested


def test_a_merge_takes_each_key_from_the_side_it_was_told_to():
    left = {"fetch": {"depth": 1, "prune": True}}
    right = {"fetch": {"depth": 9, "prune": False}}

    out = settings_diff.merged(
        left, right, {"fetch.depth": settings_diff.RIGHT, "fetch.prune": settings_diff.LEFT}
    )

    assert out == {"fetch": {"depth": 9, "prune": True}}


def test_a_key_nobody_chose_comes_from_the_left():
    """The side the window shows first, and the one every row starts on."""
    out = settings_diff.merged({"a": 1}, {"a": 2}, {})
    assert out == {"a": 1}


def test_taking_the_side_that_does_not_have_a_key_leaves_it_out():
    """Which is the only way a merge can produce a file that says less."""
    out = settings_diff.merged(
        {"a": 1, "b": 2}, {"a": 1}, {"b": settings_diff.RIGHT}
    )
    assert out == {"a": 1}


def test_a_merge_of_two_identical_sides_is_that_side():
    same = {"branch": {"pattern": "x/{name}"}}
    assert settings_diff.merged(same, same, {}) == same
