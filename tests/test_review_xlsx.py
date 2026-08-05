"""Reading the spreadsheet a team already keeps its standards in."""

import pytest

pytest.importorskip("openpyxl")

import openpyxl  # noqa: E402

from git_assistant.review.rules import Rule, RuleTable  # noqa: E402
from git_assistant.review.xlsx import XlsxError, read_rules, write_rules  # noqa: E402


def _book(tmp_path, rows, name="rules.xlsx", sheets=None):
    """Write a workbook from rows of cells; ``sheets`` adds more before it."""
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Rules"
    for row in rows:
        sheet.append(row)
    for title, extra in (sheets or []):
        other = book.create_sheet(title, 0)
        for row in extra:
            other.append(row)
    path = tmp_path / name
    book.save(str(path))
    return path


HEADER = ["ruleID", "ruleDetails"]
BODY = [["R-1", "no bare except"], ["R-2", "log at the boundary"]]


# ---- the ordinary case ------------------------------------------------------------
def test_a_sheet_with_ruleid_and_ruledetails_reads_as_rules(tmp_path):
    table = read_rules(_book(tmp_path, [HEADER, *BODY]))

    assert [r.rule_id for r in table.rules] == ["R-1", "R-2"]
    assert table.rules[0].details == "no bare except"


def test_the_table_is_named_after_the_file_it_came_from(tmp_path):
    assert read_rules(_book(tmp_path, [HEADER, *BODY], name="Python house.xlsx")).name == (
        "Python house"
    )
    assert read_rules(_book(tmp_path, [HEADER, *BODY]), name="Chosen").name == "Chosen"


@pytest.mark.parametrize(
    "headers",
    [
        ["Rule ID", "Rule Details"],
        ["rule_id", "rule_details"],
        ["RULEID", "RULEDETAILS"],
        ["  ruleId  ", "  ruledetails "],
        ["ID", "Description"],
    ],
)
def test_the_columns_are_found_whatever_they_are_called(tmp_path, headers):
    assert len(read_rules(_book(tmp_path, [headers, *BODY])).rules) == 2


def test_the_header_is_found_below_a_title_row(tmp_path):
    rows = [["Coding standard v4"], [], HEADER, *BODY]
    assert len(read_rules(_book(tmp_path, rows)).rules) == 2


def test_columns_nobody_here_cares_about_are_ignored(tmp_path):
    rows = [
        ["Owner", "ruleID", "Severity", "ruleDetails"],
        ["ana", "R-1", "high", "no bare except"],
    ]
    table = read_rules(_book(tmp_path, rows))
    assert (table.rules[0].rule_id, table.rules[0].details) == ("R-1", "no bare except")


def test_the_first_sheet_that_has_the_columns_wins(tmp_path):
    cover = [["Coding standard"], ["Reviewed 2026-01"]]
    path = _book(tmp_path, [HEADER, *BODY], sheets=[("Cover", cover)])
    assert len(read_rules(path).rules) == 2


# ---- rows that are not rules --------------------------------------------------------
def test_a_row_with_no_rule_id_is_skipped(tmp_path):
    rows = [HEADER, ["R-1", "no bare except"], [None, "a note in the margin"], [], ["R-2", "x"]]
    assert [r.rule_id for r in read_rules(_book(tmp_path, rows)).rules] == ["R-1", "R-2"]


def test_a_rule_with_no_details_is_still_a_rule(tmp_path):
    assert read_rules(_book(tmp_path, [HEADER, ["R-1", None]])).rules[0].details == ""


def test_numbers_are_read_as_the_text_they_look_like(tmp_path):
    assert read_rules(_book(tmp_path, [HEADER, [12, 34]])).rules[0].rule_id == "12"


# ---- when it cannot be read ---------------------------------------------------------
def test_a_sheet_without_the_two_columns_says_which_columns_it_wanted(tmp_path):
    with pytest.raises(XlsxError) as exc:
        read_rules(_book(tmp_path, [["name", "notes"], ["a", "b"]]))
    assert "ruleID" in str(exc.value) and "ruleDetails" in str(exc.value)


def test_a_sheet_with_the_columns_but_nothing_under_them_says_so(tmp_path):
    with pytest.raises(XlsxError) as exc:
        read_rules(_book(tmp_path, [HEADER]))
    assert "no rules" in str(exc.value)


def test_a_file_that_is_not_a_workbook_fails_with_a_readable_message(tmp_path):
    path = tmp_path / "notes.xlsx"
    path.write_text("this is not a spreadsheet", encoding="utf-8")
    with pytest.raises(XlsxError) as exc:
        read_rules(path)
    assert "notes.xlsx" in str(exc.value)


def test_a_missing_file_fails_with_a_readable_message(tmp_path):
    with pytest.raises(XlsxError):
        read_rules(tmp_path / "nothing.xlsx")


# ---- writing ------------------------------------------------------------------------
def test_writing_a_table_and_reading_it_back_gives_the_same_rules(tmp_path):
    table = RuleTable(name="House rules", rules=[Rule("R-1", "no bare except")])
    path = tmp_path / "out.xlsx"

    write_rules(path, table)
    back = read_rules(path)

    assert [(r.rule_id, r.details) for r in back.rules] == [("R-1", "no bare except")]


def test_a_table_name_a_sheet_cannot_have_does_not_stop_the_export(tmp_path):
    table = RuleTable(name="a/b:c" + "x" * 60, rules=[Rule("R-1", "d")])
    path = tmp_path / "out.xlsx"

    write_rules(path, table)

    assert len(read_rules(path).rules) == 1
