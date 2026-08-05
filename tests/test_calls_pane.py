"""The shared pane that shows what was sent to the model, and what came back."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.llm_log import LlmCall  # noqa: E402
from git_assistant.ui.calls_pane import CallsPane, transcript  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pane(qapp):
    return CallsPane()


def _call(index=1, phase="reviewing a file", response="NO FINDINGS", error=""):
    return LlmCall(
        index=index,
        phase=phase,
        model="m",
        system="be strict",
        user="the file",
        max_tokens=512,
        response=response,
        error=error,
        seconds=1.5,
    )


# ---- what it shows -------------------------------------------------------------
def test_the_pane_starts_empty(pane):
    assert pane.calls == []
    assert pane.calls_list.count() == 0
    assert pane.calls_label.text() == "View LLM Calls"
    assert not pane.copy_all_calls_btn.isEnabled()


def test_each_call_appears_as_it_finishes(pane):
    pane.add_call(_call(1))
    pane.add_call(_call(2, phase="writing the message"))

    assert pane.calls_list.count() == 2
    assert "reviewing a file" in pane.calls_list.item(0).text()
    assert pane.calls_label.text() == "View LLM Calls (2)"


def test_selecting_a_call_shows_exactly_what_was_sent_and_returned(pane):
    pane.add_call(_call(response="FINDING | R-1 | 4 | x"))
    pane.calls_list.setCurrentRow(0)

    shown = pane.call_view.toPlainText()

    assert "be strict" in shown and "the file" in shown
    assert "FINDING | R-1 | 4 | x" in shown


def test_the_first_call_is_selected_so_the_view_is_never_blank(pane):
    pane.add_call(_call())
    assert pane.calls_list.currentRow() == 0
    assert pane.call_view.toPlainText()


def test_a_failed_call_is_marked(pane):
    pane.add_call(_call(error="Server error '500'"))
    assert "[failed]" in pane.calls_list.item(0).text()


def test_keeping_a_call_is_offered_only_once_there_is_one(pane):
    assert not pane.save_calls_btn.isEnabled()
    pane.add_call(_call())
    assert pane.save_calls_btn.isEnabled()
    assert pane.copy_all_calls_btn.isEnabled()


def test_resetting_forgets_the_previous_run(pane):
    pane.add_call(_call())
    pane.reset()

    assert pane.calls == []
    assert pane.calls_list.count() == 0
    assert pane.call_view.toPlainText() == ""
    assert not pane.save_calls_btn.isEnabled()


def test_a_run_with_no_calls_of_its_own_can_say_why(pane):
    """Opening a stored review: the findings survive, the calls do not."""
    pane.add_call(_call())
    pane.say("The calls of a stored review are not recorded.")

    assert pane.calls == []
    assert "not recorded" in pane.call_view.toPlainText()


# ---- what it hands back ----------------------------------------------------------
def test_the_host_is_told_what_to_put_in_its_status_line(pane):
    said = []
    pane.noted.connect(said.append)
    pane.add_call(_call())
    pane._on_copy_all()

    assert said and "1 call(s) copied" in said[0]


def test_the_whole_transcript_is_every_call_in_order():
    text = transcript([_call(1, response="first"), _call(2, response="second")])
    assert "2 call(s)" in text
    assert text.index("first") < text.index("second")
