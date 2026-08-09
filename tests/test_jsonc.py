"""JSON with comments: what is tolerated on the way in, and what is written out.

Two things are being protected here. On the way in, a hand-edited file must not
be refused for a comma somebody left behind, and must still be refused -- with a
line number that points at the right line -- when it is genuinely broken. On the
way out, a comment must never change what the file means.
"""

from __future__ import annotations

import json

import pytest

from git_assistant import jsonc

# ---- reading -------------------------------------------------------------------------
def test_a_line_comment_is_ignored():
    assert jsonc.loads('{\n  // what this is for\n  "a": 1\n}') == {"a": 1}


def test_a_block_comment_is_ignored():
    assert jsonc.loads('{/* aside */ "a": 1}') == {"a": 1}


def test_a_block_comment_over_several_lines_is_ignored():
    assert jsonc.loads('{\n/* one\n   two */\n"a": 1}') == {"a": 1}


def test_an_unterminated_block_comment_swallows_the_rest():
    """Which is what it does in every language that has them."""
    with pytest.raises(json.JSONDecodeError):
        jsonc.loads('{"a": 1 /* and then nothing')


def test_two_slashes_inside_a_string_are_not_a_comment():
    """The case that catches every naive implementation."""
    assert jsonc.loads('{"url": "http://example.com/x"}') == {
        "url": "http://example.com/x"
    }


def test_a_comment_marker_inside_a_string_is_kept():
    assert jsonc.loads('{"a": "not // a comment", "b": "nor /* this */"}') == {
        "a": "not // a comment",
        "b": "nor /* this */",
    }


def test_an_escaped_quote_does_not_end_the_string():
    assert jsonc.loads(r'{"a": "say \"hi\" // still a string"}') == {
        "a": 'say "hi" // still a string'
    }


def test_a_trailing_comma_is_tolerated():
    """It is what you get when you delete the last entry of a list."""
    assert jsonc.loads('{"a": [1, 2,], "b": 3,}') == {"a": [1, 2], "b": 3}


@pytest.mark.parametrize(
    "text", ['{,}', '[,1]', '{"a": 1,, "b": 2}', '{"a": 1, , }']
)
def test_a_comma_with_nothing_in_front_of_it_is_still_an_error(text):
    """Tolerating a *trailing* comma is not tolerating a missing entry."""
    with pytest.raises(json.JSONDecodeError):
        jsonc.loads(text)


def test_a_comma_inside_a_string_is_not_a_trailing_comma():
    assert jsonc.loads('{"a": "one, two"}') == {"a": "one, two"}


def test_a_genuinely_broken_file_is_still_refused():
    with pytest.raises(json.JSONDecodeError):
        jsonc.loads('{"a": }')


def test_the_error_points_at_the_line_in_the_file_the_user_has_open():
    """Comments are blanked, not deleted, so the positions still line up."""
    text = '{\n  // a comment\n  // and another\n  "a": oops\n}'
    with pytest.raises(json.JSONDecodeError) as caught:
        jsonc.loads(text)

    assert caught.value.lineno == 4
    assert text.split("\n")[caught.value.lineno - 1].strip() == '"a": oops'


def test_stripping_keeps_the_length_and_the_line_breaks():
    text = '{\n  // one\n  "a": 1 /* two */\n}'
    stripped = jsonc.strip(text)

    assert len(stripped) == len(text)
    assert stripped.count("\n") == text.count("\n")


def test_plain_json_is_read_unchanged():
    assert jsonc.loads('{"a": [1, {"b": null}], "c": true}') == {
        "a": [1, {"b": None}],
        "c": True,
    }


# ---- writing -------------------------------------------------------------------------
def test_the_comment_goes_above_the_key_not_beside_it():
    text = jsonc.dumps({"a": 1}, {"a": "what a is"})
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    assert lines[1] == "// what a is"
    assert lines[2] == '"a": 1'


def test_what_is_written_reads_back_as_what_went_in():
    data = {
        "version": 1,
        "branch": {"pattern": "{name}", "patterns": ["a/{name}", "b/{name}"]},
        "fetch": {"shallow": False, "depth": 1},
        "commit": {"ignore_globs": ["*.lock"], "body_limit": 1000},
    }
    comments = {"branch": "x", "branch.pattern": "y", "commit.ignore_globs": "z"}

    assert jsonc.loads(jsonc.dumps(data, comments, "a header")) == data


def test_a_nested_key_is_named_by_its_path():
    """So `history_limit` under two sections can say two different things."""
    text = jsonc.dumps(
        {"audit": {"history_limit": 1}, "review": {"history_limit": 2}},
        {"audit.history_limit": "audits", "review.history_limit": "reviews"},
    )

    assert "// audits" in text and "// reviews" in text


def test_the_header_is_the_first_thing_in_the_file():
    text = jsonc.dumps({"a": 1}, {}, "These settings are overridden by x")
    assert text.split("\n")[1].strip() == "// These settings are overridden by x"


def test_a_header_of_several_lines_is_several_comments():
    text = jsonc.dumps({"a": 1}, {}, "one\ntwo")
    assert "// one" in text and "// two" in text


def test_a_key_with_nothing_to_say_gets_no_comment():
    """A file where half the comments say nothing is a file nobody reads."""
    text = jsonc.dumps({"a": 1, "b": 2}, {"a": "about a"})

    assert text.count("//") == 1


def test_a_long_list_is_written_one_entry_per_line_and_still_parses():
    data = {"commit": {"ignore_globs": ["*.a", "*.b", "*.c"]}}
    text = jsonc.dumps(data, {})

    assert '"*.a",' in text  # laid out, not crammed onto one line
    assert jsonc.loads(text) == data


def test_an_empty_object_is_written_as_one():
    assert jsonc.loads(jsonc.dumps({"a": {}, "b": []}, {})) == {"a": {}, "b": []}


def test_a_comment_containing_a_quote_does_not_break_the_file():
    text = jsonc.dumps({"a": 1}, {"a": 'the "cached" one'})
    assert jsonc.loads(text) == {"a": 1}


def test_non_ascii_survives_both_ways():
    data = {"user": "Stefan Ghițescu"}
    assert jsonc.loads(jsonc.dumps(data, {"user": "who ünïcode is"})) == data
