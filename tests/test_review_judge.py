"""Scoring a review: reading the judge's answer, and refusing to invent one."""

import pytest

from git_assistant.review import prompts
from git_assistant.review.judge import (
    MAX_SCORE,
    MIN_SCORE,
    JudgeConfig,
    Verdict,
    build_prompt,
    clamp,
    mean_of,
    parse_verdict,
)


# ---- reading an answer -----------------------------------------------------------
def test_the_line_it_was_asked_for_is_read():
    verdict = parse_verdict("SCORE | 7.5 | quoted a rule that was not on the list")

    assert verdict.score == 7.5
    assert verdict.reason == "quoted a rule that was not on the list"
    assert verdict.scored is True


def test_a_colon_reads_the_same_as_a_pipe():
    """Models substitute one for the other; both are unambiguous here."""
    assert parse_verdict("SCORE: 9 : clean").score == 9.0


def test_a_score_after_the_reasoning_is_still_found():
    """A model that thinks out loud puts the answer last."""
    reply = "Let me check the ids.\nR-1 and R-2 are on the list.\nSCORE | 8 | fine"
    assert parse_verdict(reply).score == 8.0


def test_a_reason_is_optional():
    assert parse_verdict("SCORE | 6").score == 6.0
    assert parse_verdict("SCORE | 6").reason == ""


def test_a_bare_number_on_its_own_is_accepted():
    """Some models drop the label and answer with the number alone."""
    assert parse_verdict("7.5").score == 7.5
    assert parse_verdict("8/10").score == 8.0


def test_a_number_inside_prose_is_not_a_score():
    """`rule PY-06 was quoted correctly` must not be read as a 6.

    This is the failure that would matter: it looks like a working judge and
    fills the leaderboard with digits from rule ids.
    """
    verdict = parse_verdict("It was a good review; rule PY-06 was quoted correctly.")

    assert verdict.scored is False
    assert "SCORE line" in verdict.error


# ---- refusing to invent one --------------------------------------------------------
def test_an_unreadable_answer_is_an_error_not_a_zero():
    """Zero is a judgement. "The judge did not answer" is not one.

    Recording it as zero would put the judge's failures into the reviewer's
    average, which is the one number this feature exists to get right.
    """
    verdict = parse_verdict("I am unable to assess this.")

    assert verdict.scored is False
    assert verdict.score == 0.0  # the field's default, never counted


def test_nothing_at_all_says_so_distinctly():
    assert parse_verdict("").error == "the judge returned nothing"
    assert parse_verdict("   \n ").error == "the judge returned nothing"


def test_a_failed_call_is_left_out_of_the_mean():
    verdicts = [
        parse_verdict("SCORE | 8 |"),
        parse_verdict("SCORE | 6 |"),
        parse_verdict("the judge fell over"),
    ]
    assert mean_of(verdicts) == 7.0  # not 4.67, which counting the failure gives


def test_a_run_nobody_could_score_has_no_mean():
    assert mean_of([parse_verdict("nonsense")]) == 0.0
    assert mean_of([]) == 0.0


# ---- the band ----------------------------------------------------------------------
@pytest.mark.parametrize(
    ("answered", "kept"),
    [("12", MAX_SCORE), ("-3", MIN_SCORE), ("10", 10.0), ("0", 0.0), ("4.25", 4.25)],
)
def test_a_score_outside_the_band_is_pulled_back_into_it(answered, kept):
    assert parse_verdict(f"SCORE | {answered} | why").score == kept


def test_clamp_is_the_band_and_nothing_else():
    assert clamp(-1) == MIN_SCORE
    assert clamp(99) == MAX_SCORE


# ---- what the judge is shown --------------------------------------------------------
def test_the_exchange_is_filled_into_the_prompt():
    filled = build_prompt(prompts.JUDGE_TEMPLATE, prompt="THE RULES", reply="FINDING | R-1")

    assert "THE RULES" in filled
    assert "FINDING | R-1" in filled
    assert "{prompt}" not in filled and "{reply}" not in filled


def test_braces_in_the_code_being_judged_do_not_blow_up():
    """The prompt carries a diff, and a diff is full of braces.

    `str.format` would raise on somebody's C++ rather than score it, which is
    why `prompts.render` is used throughout.
    """
    filled = build_prompt(
        prompts.JUDGE_TEMPLATE,
        prompt="int main() { return {0}; }",
        reply="void f() {}",
    )
    assert "int main() { return {0}; }" in filled


def test_an_empty_template_falls_back_to_the_shipped_one():
    """A user who empties the prompt gets the default, not an empty question."""
    assert "SCORE |" in build_prompt("", prompt="p", reply="r")


# ---- the configuration -----------------------------------------------------------
def test_a_judge_needs_both_a_provider_and_a_model():
    assert JudgeConfig(provider="claude", model="sonnet").usable() is True
    assert JudgeConfig(provider="claude").usable() is False
    assert JudgeConfig(model="sonnet").usable() is False
    assert JudgeConfig().usable() is False


def test_an_unusable_judge_says_so_rather_than_naming_nothing():
    assert JudgeConfig().label() == "not configured"
    assert JudgeConfig(provider="claude", model="sonnet").label() == "sonnet (claude)"


def test_the_config_is_frozen():
    """A run must be scored by the model its estimate was priced against."""
    with pytest.raises(Exception):
        JudgeConfig(provider="claude", model="sonnet").model = "opus"


# ---- what the rest of the code asks a verdict --------------------------------------
def test_a_verdict_with_an_error_is_never_counted_however_its_score_reads():
    assert Verdict(score=9.0, error="timed out").scored is False
