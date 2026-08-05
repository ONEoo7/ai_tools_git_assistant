"""The pop-up that shows what a run will send before it sends it."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from git_assistant.estimate import Estimate  # noqa: E402
from git_assistant.ui import estimate_dialog  # noqa: E402
from git_assistant.ui.estimate_dialog import confirm, describe  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _estimate(**kw):
    fields = {
        "feature": "Code review",
        "calls": 12,
        "input_tokens": 48_120,
        "output_tokens": 6_144,
        "model": "qwen3.5-4b",
        "provider": "lmstudio",
        "lines": ["One call per marked file: 12 file(s), 4 at a time."],
    }
    fields.update(kw)
    return Estimate(**fields)


# ---- what it says --------------------------------------------------------------
def test_it_leads_with_the_number_of_calls_and_the_tokens():
    text = describe(_estimate())

    assert "12 call(s)" in text
    assert "48,120 tokens in" in text
    assert "6,144 out" in text
    assert "54,264 in total" in text


def test_it_names_the_provider_and_model_that_will_answer():
    assert "LM Studio - qwen3.5-4b" in describe(_estimate())


def test_it_says_when_no_model_has_been_chosen():
    assert "no model selected" in describe(_estimate(model=""))


def test_it_explains_where_the_number_came_from():
    assert "One call per marked file" in describe(_estimate())


def test_it_admits_the_numbers_are_estimates():
    """The provider does its own counting, and that is what lands in the usage table."""
    assert "estimates" in describe(_estimate())


def test_an_unknown_input_is_not_dressed_up_as_a_figure():
    text = describe(_estimate(input_unknown=True, input_tokens=0, input_cap=29_492))
    assert "not known until" in text
    assert "capped at 29,492" in text


def test_an_unknown_provider_does_not_break_the_message():
    assert "made-up-provider" in describe(_estimate(provider="made-up-provider"))


# ---- what it does --------------------------------------------------------------
def test_a_run_with_nothing_to_do_is_refused_with_its_reason(qapp, monkeypatch):
    shown = []
    monkeypatch.setattr(
        estimate_dialog.QMessageBox,
        "information",
        lambda parent, title, text: shown.append(text),
    )

    assert confirm(None, _estimate(calls=0, problem="No files are marked.")) is False
    assert shown == ["No files are marked."]


def test_a_run_that_sends_nothing_is_not_worth_a_pop_up(qapp):
    """An audit written from the measurements alone asks the model nothing."""
    assert confirm(None, _estimate(calls=0, input_tokens=0, output_tokens=0)) is True


def test_a_dialog_that_is_dismissed_does_not_agree_to_anything(qapp, monkeypatch):
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    assert confirm(None, _estimate()) is False


def test_agreeing_runs_it(qapp, monkeypatch):
    def press_run(box):
        # The accept button is the default one; press it as a user would.
        box.defaultButton().click()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", press_run)
    assert confirm(None, _estimate()) is True
