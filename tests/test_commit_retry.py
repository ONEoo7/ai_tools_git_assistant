"""Asking again when the message came back too long.

The point of every test here: the second attempt is *cheaper* than the first and
*different* from it. Cheaper because a map-reduce run re-sends only its synthesis
prompt; different because the reason for the rejection is quoted into it, which
is what makes a retry worth paying for at the low temperature this application
defaults to.
"""

import pytest

from git_assistant import estimate
from git_assistant.commit_generator import Retry
from git_assistant.commit_style import Limits, measure
from git_assistant.config import Settings

SYSTEM = "You write commit messages."
USER = "Summarise these notes:\n- a change\n- another change\n"


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None
    s.selected_model = "qwen3.5-4b"
    return s


def _retry(calls_before=1):
    return Retry(system=SYSTEM, user=USER, max_tokens=512, calls_before=calls_before)


# ---- what is re-sent ----------------------------------------------------------------
def test_a_retry_sends_the_prompt_that_wrote_the_message():
    assert _retry().with_note("") == USER


def test_the_reason_is_appended_rather_than_replacing_anything():
    note = "Your previous answer was rejected because..."
    again = _retry().with_note(note)

    assert again.startswith("Summarise these notes:")
    assert again.endswith(note + "\n")


def test_a_map_reduce_run_re_sends_only_its_synthesis():
    """The notes are already in this prompt; the chunks are not read again."""
    retry = _retry(calls_before=15)
    priced = estimate.for_retry(Settings(), retry)

    assert priced.calls == 1
    assert any("15 call(s) the first time" in line for line in priced.lines)
    assert any("None of that is repeated" in line for line in priced.lines)


def test_the_estimate_counts_the_prompt_that_will_go(settings):
    note = "shorter please"
    priced = estimate.for_retry(settings, _retry(), note)

    assert priced.input_tokens == _retry().input_tokens(note)
    assert priced.output_tokens == 512
    assert priced.model == "qwen3.5-4b"


def test_the_note_is_counted_too(settings):
    """It is part of the prompt; leaving it out would under-report the spend."""
    plain = estimate.for_retry(settings, _retry())
    with_note = estimate.for_retry(settings, _retry(), "x" * 400)
    assert with_note.input_tokens > plain.input_tokens


def test_a_message_with_no_prompt_behind_it_cannot_be_retried(settings):
    """One opened from history, for instance."""
    priced = estimate.for_retry(settings, None)
    assert priced.problem and "no prompt to send again" in priced.problem


def test_a_retry_is_billed_apart_from_the_run_that_caused_it(settings):
    """Merging them would lose the answer to "what did the retries cost me"."""
    assert estimate.for_retry(settings, _retry()).feature == estimate.SHORTEN
    assert estimate.for_commit(settings).feature != estimate.SHORTEN


# ---- what the model is told it got wrong -----------------------------------------------
def test_the_note_quotes_the_length_that_was_rejected():
    note = measure("x" * 91, Limits()).retry_note()
    assert "91 characters" in note and "72 allowed" in note


def test_a_long_body_is_named_too():
    note = measure("subject\n\n" + "y" * 1200, Limits()).retry_note()
    assert "1200 characters" in note and "1000 allowed" in note


def test_both_faults_are_reported_together():
    note = measure("x" * 91 + "\n\n" + "y" * 1200, Limits()).retry_note()
    assert "first line" in note and "body" in note and " and " in note


def test_a_message_within_the_limits_has_nothing_to_say():
    assert measure("feat: fine\n\nfine", Limits()).retry_note() == ""


def test_the_note_asks_for_the_same_meaning_not_a_different_message():
    note = measure("x" * 91, Limits()).retry_note()
    assert "Keep the same meaning" in note
    assert "not the reason for the change" in note


def test_the_prompt_changes_so_the_answer_can(settings):
    """At temperature 0 the same prompt gives the same answer; this one differs."""
    note = measure("x" * 91, Limits()).retry_note()
    assert _retry().with_note(note) != _retry().with_note("")
    assert any("low temperature" in line for line in estimate.for_retry(
        settings, _retry(), note
    ).lines)
