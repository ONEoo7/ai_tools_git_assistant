"""Reading a small model's answer without losing what it found."""

import pytest

from git_assistant.review.parse import parse_findings, said_clean, salvage
from git_assistant.review.rules import Rule, RuleTable

TABLE = RuleTable(
    name="House rules",
    rules=[
        Rule("R-1", "no bare except"),
        Rule("R-12", "log at the boundary"),
    ],
)


def _parse(reply):
    return parse_findings(reply, TABLE, "app.py")


# ---- the ordinary case ------------------------------------------------------------
def test_a_clean_reply_becomes_findings():
    found = _parse(
        "FINDING | R-1 | 42 | catches everything and swallows it\n"
        "FINDING | R-12 | 88 | the handler logs nothing"
    )

    assert [(f.rule_id, f.line) for f in found] == [("R-1", 42), ("R-12", 88)]
    assert found[0].message == "catches everything and swallows it"
    assert found[0].path == "app.py"


def test_the_rule_text_is_carried_with_the_finding():
    assert _parse("FINDING | R-1 | 4 | x")[0].rule_details == "no bare except"


def test_the_rule_text_is_snapshotted_so_it_survives_the_table_being_edited():
    table = RuleTable(name="t", rules=[Rule("R-1", "as written today")])
    finding = parse_findings("FINDING | R-1 | 1 | x", table, "a.py")[0]

    table.rules[0].details = "rewritten later"

    assert finding.rule_details == "as written today"


# ---- what a small model actually sends back ------------------------------------------
def test_a_reply_wrapped_in_a_code_fence_still_parses():
    assert len(_parse("```\nFINDING | R-1 | 42 | x\n```")) == 1


def test_a_polite_preamble_and_a_closing_summary_are_ignored():
    found = _parse(
        "Sure! Here is my review of the file:\n\n"
        "FINDING | R-1 | 42 | catches everything\n\n"
        "Let me know if you would like me to explain any of these."
    )
    assert len(found) == 1


def test_markdown_bullets_and_bold_around_a_finding_are_stripped():
    found = _parse("- **FINDING** | R-1 | 42 | catches everything\n2. FINDING | R-12 | 1 | y")
    assert [f.rule_id for f in found] == ["R-1", "R-12"]


def test_a_finding_written_with_colons_and_dashes_instead_of_pipes():
    found = _parse("FINDING: R-1 - 42 - catches everything")
    assert (found[0].rule_id, found[0].line, found[0].message) == (
        "R-1",
        42,
        "catches everything",
    )


def test_the_dash_inside_a_rule_id_is_not_mistaken_for_a_separator():
    assert _parse("FINDING - R-12 - 7 - no logging")[0].rule_id == "R-12"


def test_a_finding_with_a_missing_line_number_keeps_its_message():
    found = _parse("FINDING | R-1 | catches everything")
    assert (found[0].line, found[0].message) == (0, "catches everything")


def test_a_dash_where_the_line_number_goes_means_the_model_did_not_say():
    assert _parse("FINDING | R-1 | - | catches everything")[0].line == 0


@pytest.mark.parametrize("field", ["42", "line 42", "L42", " 42 "])
def test_a_line_number_is_read_from_anything_containing_one(field):
    assert _parse(f"FINDING | R-1 | {field} | x")[0].line == 42


def test_a_rule_id_that_differs_only_in_case_matches_the_table():
    found = _parse("FINDING | r_1 | 3 | x")
    assert found[0].rule_id == "R-1" and found[0].rule_known


def test_an_unknown_rule_id_is_kept_and_marked_unknown():
    found = _parse("FINDING | R-99 | 3 | invented one")
    assert found[0].rule_id == "R-99"
    assert found[0].rule_known is False
    assert found[0].rule_details == ""


def test_the_instruction_echoed_back_is_not_a_finding():
    assert _parse("FINDING | <ruleID exactly as listed above> | <line> | <one sentence>") == []


def test_a_message_containing_a_pipe_is_not_cut_in_half():
    found = _parse("FINDING | R-1 | 1 | uses a | b instead of a or b")
    assert found[0].message == "uses a | b instead of a or b"


def test_the_model_s_own_line_is_kept_for_the_detail_view():
    assert "R-1" in _parse("FINDING | R-1 | 1 | x")[0].raw_line


# ---- nothing found, and nothing understood -------------------------------------------
def test_no_findings_is_not_a_parse_failure():
    assert _parse("NO FINDINGS") == []
    assert said_clean("NO FINDINGS")


@pytest.mark.parametrize(
    "reply",
    ["no findings", "No findings.", "I found no violations.", "There are no issues here."],
)
def test_a_clean_file_is_recognised_however_it_is_phrased(reply):
    assert said_clean(reply)


def test_prose_that_never_says_the_file_is_fine_is_not_a_clean_file():
    """Silence is not a verdict; only the caller may decide what to do with it."""
    assert said_clean("This file implements the settings dialog.") is False
    assert _parse("This file implements the settings dialog.") == []


def test_a_reply_that_cannot_be_read_becomes_one_finding_carrying_the_raw_text():
    finding = salvage("Here is a wall of prose about the file.", "app.py")

    assert finding.parsed is False
    assert finding.path == "app.py"
    assert "wall of prose" in finding.raw_line
    assert "could not be read" in finding.message


def test_the_salvaged_finding_does_not_carry_a_whole_essay():
    finding = salvage("x" * 5000, "app.py", limit=100)
    assert len(finding.raw_line) == 100
