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


# ---- sending only file names ---------------------------------------------------------
def _commit_estimate(only_names=False):
    names = Estimate(
        feature="Commit message",
        calls=1,
        input_tokens=3_412,
        output_tokens=2_048,
        model="qwen3.5-4b",
        provider="lmstudio",
        lines=["The names of 1,234 file(s), grouped by what happened to each."],
    )
    return _estimate(
        feature="Commit message",
        calls=15,
        input_tokens=245_000,
        output_tokens=7_808,
        lines=["The diff is larger than the window, so it is summarised in pieces."],
        names_only=names,
        only_names=only_names,
    )


def _opened(monkeypatch, act):
    """``confirm`` with the dialog handed to ``act`` instead of shown."""
    monkeypatch.setattr(QMessageBox, "exec", lambda box: act(box) or 0)


def test_the_run_from_names_is_priced_beside_the_whole_one():
    text = describe(_commit_estimate())

    assert text.splitlines()[0].startswith("15 call(s), about 245,000 tokens in")
    assert "Send only file names: 1 call(s), about 3,412 tokens in and 2,048 out" in text
    assert "The names of 1,234 file(s)" in text


def test_ticked_the_headline_is_the_run_that_will_happen():
    assert describe(_commit_estimate(only_names=True)).splitlines()[0].startswith(
        "1 call(s), about 3,412 tokens in"
    )


def test_the_box_starts_as_the_estimate_says(qapp, monkeypatch):
    seen = []
    _opened(
        monkeypatch,
        lambda box: seen.append((box.checkBox().text(), box.checkBox().isChecked(), box.text())),
    )

    confirm(None, _commit_estimate(only_names=True))

    (text, ticked, headline), = seen
    assert (text, ticked) == ("Send only file names", True)
    assert "3,412" in headline


def test_ticking_it_moves_the_headline_and_is_what_the_run_does(qapp, monkeypatch):
    priced = _commit_estimate()
    headlines = []

    def tick_and_run(box):
        headlines.append(box.text())
        box.checkBox().setChecked(True)
        headlines.append(box.text())
        box.defaultButton().click()

    _opened(monkeypatch, tick_and_run)

    assert confirm(None, priced) is True
    assert "245,000" in headlines[0] and "3,412" in headlines[1]
    assert priced.only_names is True


def test_unticking_it_sends_the_changes(qapp, monkeypatch):
    priced = _commit_estimate(only_names=True)

    def untick_and_run(box):
        box.checkBox().setChecked(False)
        box.defaultButton().click()

    _opened(monkeypatch, untick_and_run)

    assert confirm(None, priced) is True and priced.only_names is False


def test_a_run_with_nothing_to_choose_has_no_box(qapp, monkeypatch):
    seen = []
    _opened(monkeypatch, lambda box: seen.append(box.checkBox()))

    confirm(None, _estimate())

    assert seen == [None]
