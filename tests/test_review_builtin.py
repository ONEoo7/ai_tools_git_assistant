"""The rules that ship with the application, and their version ranges."""

import pytest

from git_assistant.review import builtin, languages
from git_assistant.review.builtin import BuiltinRule
from git_assistant.review.rules import normalize_id


# ---- what ships ------------------------------------------------------------------
def test_every_language_has_a_table():
    assert set(builtin.languages_covered()) == set(languages.ids())


def test_each_table_holds_enough_rules_to_be_worth_running():
    for language, table in builtin.tables().items():
        assert 6 <= len(table.rules) <= 12, f"{language}: {len(table.rules)}"


def test_every_rule_says_something():
    for table in builtin.tables().values():
        for rule in table.rules:
            assert rule.rule_id and rule.details
            assert len(rule.details) > 30, rule.rule_id


def test_no_two_rules_collide_once_punctuation_is_stripped():
    """`RuleTable.find` matches ids with punctuation removed, so PY-01 and py 01 are one."""
    seen: dict[str, str] = {}
    for table in builtin.tables().values():
        for rule in table.rules:
            key = normalize_id(rule.rule_id)
            assert key not in seen, f"{rule.rule_id} collides with {seen[key]}"
            seen[key] = rule.rule_id


def test_ids_are_prefixed_so_they_cannot_collide_with_a_user_s_own():
    for table in builtin.tables().values():
        for rule in table.rules:
            assert "-" in rule.rule_id, rule.rule_id


# ---- the version ranges ------------------------------------------------------------
def test_every_version_a_rule_names_is_a_real_version_of_its_language():
    """A typo makes a rule apply to nothing, which looks exactly like a clean file."""
    for language, table in builtin.tables().items():
        lang = languages.get(language)
        assert lang is not None, language
        for rule in table.rules:
            for edge in (rule.since, rule.until):
                if edge:
                    assert edge in lang.versions, f"{rule.rule_id}: {edge!r}"


def test_a_rule_pinned_to_a_later_version_is_left_out_of_an_earlier_one():
    old = builtin.table_for("python", "py2")
    new = builtin.table_for("python", "py312")

    assert any(r.rule_id == "PY-06" for r in new.rules), "f-strings apply to 3.6+"
    assert not any(r.rule_id == "PY-06" for r in old.rules)
    assert len(old.rules) < len(new.rules)


def test_a_rule_with_no_range_applies_to_every_version():
    for version in languages.get("python").versions:
        table = builtin.table_for("python", version)
        assert any(r.rule_id == "PY-01" for r in table.rules), version


def test_an_unknown_version_keeps_every_rule():
    """Filtering on a version nobody established would silently drop rules."""
    everything = builtin.table_for("cpp", "")
    assert len(everything.rules) == len(builtin.get("cpp").rules)
    assert len(builtin.table_for("cpp", "not-a-version").rules) == len(everything.rules)


def test_the_newest_version_is_not_assumed():
    """C++26 rules over a C++98 codebase would read as real findings."""
    assert len(builtin.table_for("cpp", "c++98").rules) < len(
        builtin.table_for("cpp", "c++26").rules
    )


@pytest.mark.parametrize(
    ("since", "until", "version", "expected"),
    [
        ("", "", "c++98", True),
        ("c++14", "", "c++11", False),
        ("c++14", "", "c++14", True),
        ("c++14", "", "c++20", True),
        ("", "c++14", "c++17", False),
        ("", "c++14", "c++11", True),
        ("c++11", "c++17", "c++14", True),
        ("c++11", "c++17", "c++20", False),
    ],
)
def test_both_ends_of_a_range_are_inclusive(since, until, version, expected):
    rule = BuiltinRule("X-1", "something", since=since, until=until)
    assert rule.applies_to(languages.get("cpp"), version) is expected


# ---- what a caller gets ------------------------------------------------------------
def test_a_table_arrives_as_an_ordinary_rule_table():
    table = builtin.table_for("rust", "rust2021")
    assert table.name == "Rust (built in)"
    assert table.source == "built in"
    assert table.find("RS-01") is not None


def test_a_language_that_ships_nothing_has_no_table():
    assert builtin.table_for("cobol") is None


def test_a_table_is_named_the_same_way_wherever_it_is_referred_to():
    assert builtin.ref_of("python") == "builtin:python"
    assert builtin.language_of("builtin:python") == "python"
    assert builtin.language_of("table:My rules") == ""


def test_a_missing_rules_file_leaves_the_application_reviewing(monkeypatch):
    """A build that shipped without it still runs against the user's own tables."""
    monkeypatch.setattr(builtin, "data_file", lambda *parts: None)
    assert builtin._read() == {}


def test_a_broken_rules_file_is_not_a_reason_to_refuse_to_start(monkeypatch, tmp_path):
    bad = tmp_path / "review_rules.json"
    bad.write_text("{half", encoding="utf-8")
    monkeypatch.setattr(builtin, "data_file", lambda *parts: bad)
    assert builtin._read() == {}
