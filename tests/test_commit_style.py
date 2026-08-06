"""How long a commit message may be: what is asked, and what is checked."""

import pytest

from git_assistant import commit_style
from git_assistant.commit_style import Limits, measure, rules, split, with_rules
from git_assistant.config import Settings

TEMPLATE = "Write a commit message.\n\nChanges:\n{diff}\n"


# ---- the conventions, as numbers ---------------------------------------------------
def test_the_defaults_are_the_conventions():
    """50 soft, 72 hard, and a body cap in the usual 500-1000 range."""
    limits = Limits()
    assert (limits.subject_target, limits.subject_limit) == (50, 72)
    assert 500 <= limits.body_limit <= 1000


def test_the_limits_come_from_settings():
    s = Settings()
    s.commit_subject_target, s.commit_subject_limit, s.commit_body_limit = 40, 60, 400
    assert Limits.of(s) == Limits(40, 60, 400)


def test_a_nonsense_setting_falls_back_rather_than_raising():
    s = Settings()
    s.commit_subject_limit = "seventy-two"
    assert Limits.of(s).subject_limit == 72


def test_a_negative_setting_reads_as_off():
    s = Settings()
    s.commit_body_limit = -1
    assert Limits.of(s).body_limit == commit_style.OFF


# ---- what the model is told ----------------------------------------------------------
def test_both_numbers_reach_the_prompt():
    block = rules(Limits())
    assert "72 characters" in block and "Aim for 50" in block
    assert "1000 characters" in block


def test_the_rules_say_why_the_subject_limit_exists():
    """"<= 72" is a number; "tools cut it" is a reason to obey the number."""
    assert "cut it without saying so" in rules(Limits())


def test_a_target_equal_to_the_limit_is_not_offered_twice():
    assert "Aim for" not in rules(Limits(subject_target=72, subject_limit=72))


def test_turning_every_rule_off_says_nothing():
    off = Limits(0, 0, 0)
    assert rules(off) == ""
    assert not off.asks_anything()


def test_the_rules_are_appended_to_whatever_template_is_in_use():
    """Not written into the default one: a saved template would never see them."""
    rendered = with_rules(TEMPLATE, Limits())

    assert rendered.startswith("Write a commit message.")
    assert "{diff}" in rendered, "the placeholders still have to be substituted"
    assert "72 characters" in rendered


def test_a_template_is_untouched_when_there_are_no_rules():
    assert with_rules(TEMPLATE, Limits(0, 0, 0)) == TEMPLATE


# ---- reading a message back ------------------------------------------------------------
def test_the_subject_is_the_first_line_and_the_body_is_the_rest():
    subject, body = split("feat: add the thing\n\nBecause it was missing.\nTwice.")
    assert subject == "feat: add the thing"
    assert body == "Because it was missing.\nTwice."


def test_the_blank_line_between_them_belongs_to_neither():
    """Counting it would make a well-formed message longer than a malformed one."""
    with_blank = measure("subject here\n\nbody")
    without = measure("subject here\nbody")
    assert with_blank.body == without.body == 4


def test_a_message_with_no_body_has_a_body_of_nothing():
    assert measure("just a subject").body == 0


def test_an_empty_message_measures_zero():
    assert (measure("").subject, measure("").body) == (0, 0)


# ---- what counts as too long -------------------------------------------------------------
def test_a_subject_over_the_hard_cap_is_flagged():
    measured = measure("x" * 73, Limits())
    assert measured.over_limit and measured.too_long
    assert "cut by tools" in measured.note()


def test_a_subject_over_the_target_but_under_the_cap_is_noted_not_flagged():
    """It still fits; saying it is "too long" would be false."""
    measured = measure("x" * 60, Limits())
    assert measured.over_target and not measured.over_limit
    assert not measured.too_long
    assert "still fits" in measured.note()


def test_a_body_over_its_cap_is_flagged():
    measured = measure("subject\n\n" + "x" * 1001, Limits())
    assert measured.over_body and measured.too_long
    assert "over the 1000" in measured.note()


def test_a_message_within_every_limit_says_nothing():
    assert measure("feat: a short subject\n\nA short body.", Limits()).note() == ""


def test_nothing_is_flagged_when_the_rule_is_off():
    measured = measure("x" * 500, Limits(0, 0, 0))
    assert not measured.too_long and measured.note() == ""


def test_the_counts_read_as_counts():
    assert measure("x" * 47 + "\n\n" + "y" * 312, Limits()).label() == (
        "Subject 47/72 - body 312/1000"
    )


def test_a_limit_that_is_off_shows_a_count_without_a_bar():
    assert "Subject 47 -" in measure("x" * 47, Limits(0, 0, 500)).label()


# ---- it is a report, never an enforcement ------------------------------------------------
def test_nothing_here_shortens_a_message():
    """Cutting at 72 would produce the mangled subject the limit prevents."""
    long_one = "x" * 200
    assert measure(long_one, Limits()).subject == 200
    assert split(long_one)[0] == long_one


@pytest.mark.parametrize("name", ["measure", "split", "rules", "with_rules"])
def test_the_module_offers_no_way_to_truncate(name):
    assert "truncat" not in (getattr(commit_style, name).__doc__ or "").lower()
