"""The usage table beside the connection settings."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant import usage  # noqa: E402
from git_assistant.ui.usage_pane import UsagePane  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


# A pane must be bound to a name for the length of a test: an unreferenced
# widget is collected, and every lookup into it then raises.


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


def _rows(tree):
    return [
        [tree.topLevelItem(i).text(c) for c in range(tree.columnCount())]
        for i in range(tree.topLevelItemCount())
    ]


def _find(tree, name):
    for i in range(tree.topLevelItemCount()):
        if tree.topLevelItem(i).text(0) == name:
            return tree.topLevelItem(i)
    raise AssertionError(f"no row for {name}")


# ---- before anything has been used -------------------------------------------
def test_every_provider_has_a_row_even_before_it_is_used(qapp):
    """A provider with no row reads as a missing feature, not as an unused one."""
    pane = UsagePane()

    lmstudio = _find(pane.totals_tree, "LM Studio")

    assert lmstudio.text(1) == "not used yet"
    assert pane.totals_tree.topLevelItemCount() > 1


def test_it_says_nothing_is_recorded_rather_than_showing_zeroes(qapp):
    pane = UsagePane()
    assert "Nothing recorded yet" in pane.note.text()


# ---- once there is something --------------------------------------------------
def test_a_provider_shows_its_totals_and_its_models(qapp):
    usage.record("lmstudio", "qwen3.5-4b", 1200, 340)
    usage.record("lmstudio", "qwen3.5-4b", 800, 60)
    usage.record("lmstudio", "another-model", 10, 5)

    pane = UsagePane()
    lmstudio = _find(pane.totals_tree, "LM Studio")

    assert lmstudio.text(2) == "3"  # calls
    assert lmstudio.text(3) == "2,010"  # input
    assert lmstudio.text(4) == "405"  # output
    assert lmstudio.text(5) == "2,415"  # total
    assert {lmstudio.child(i).text(0) for i in range(lmstudio.childCount())} == {
        "qwen3.5-4b",
        "another-model",
    }


def test_one_provider_s_usage_is_not_counted_under_another(qapp):
    usage.record("lmstudio", "qwen3.5-4b", 100, 10)
    usage.record("claude", "claude-opus-5", 7, 2)

    pane = UsagePane()

    assert _find(pane.totals_tree, "LM Studio").text(5) == "110"
    assert _find(pane.totals_tree, "Claude").text(5) == "9"


def test_a_recent_call_says_provider_model_what_for_when_and_tokens(qapp):
    usage.record("lmstudio", "qwen3.5-4b", 1200, 340, feature=usage.REVIEW)

    pane = UsagePane()
    row = _rows(pane.calls_tree)[0]

    assert row[0] == "LM Studio"
    assert row[1] == "qwen3.5-4b"
    assert row[2] == "Code review"
    assert row[3]  # a readable local timestamp
    assert row[4:] == ["1,200", "340", "1,540"]


def test_the_newest_call_is_at_the_top(qapp):
    usage.record("lmstudio", "first", 1, 1)
    usage.record("lmstudio", "second", 1, 1)
    pane = UsagePane()
    assert _rows(pane.calls_tree)[0][1] == "second"


def test_a_counted_here_call_is_marked_rather_than_presented_as_measured(qapp):
    usage.record("lmstudio", "qwen3.5-4b", 100, 10, estimated=True)

    pane = UsagePane()

    assert _rows(pane.calls_tree)[0][4].startswith("~")
    assert "did not report usage" in pane.note.text()


def test_the_footer_adds_up_every_provider(qapp):
    usage.record("lmstudio", "a", 100, 10)
    usage.record("claude", "b", 7, 2)

    pane = UsagePane()
    note = pane.note.text()

    assert "2 call(s)" in note and "107 in" in note and "12 out" in note


# ---- what the user can do with it -----------------------------------------------
def test_refreshing_picks_up_calls_made_since_it_was_drawn(qapp):
    pane = UsagePane()
    assert _find(pane.totals_tree, "LM Studio").text(1) == "not used yet"

    usage.record("lmstudio", "qwen3.5-4b", 100, 10)
    pane.refresh()

    assert _find(pane.totals_tree, "LM Studio").text(2) == "1"


def test_the_totals_copy_as_a_table(qapp):
    usage.record("lmstudio", "qwen3.5-4b", 1200, 340)
    pane = UsagePane()
    text = pane.to_markdown()

    assert "| LM Studio | qwen3.5-4b |" in text
    assert "1,540" in text


def test_clearing_forgets_everything_shown(qapp, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    usage.record("lmstudio", "qwen3.5-4b", 100, 10)
    pane = UsagePane()
    monkeypatch.setattr(
        "git_assistant.ui.usage_pane.QMessageBox.question",
        lambda *a, **k: QMessageBox.StandardButton.Yes,
    )

    pane._on_clear()

    assert pane.calls_tree.topLevelItemCount() == 0
    assert _find(pane.totals_tree, "LM Studio").text(1) == "not used yet"


# ---- which run the tokens went on -------------------------------------------------
def test_a_model_used_by_two_tabs_is_broken_down_by_what_it_was_used_for(qapp):
    """One figure for a model every tab shares does not answer the question."""
    usage.record("lmstudio", "qwen3.5-4b", 1000, 100, feature=usage.COMMIT)
    usage.record("lmstudio", "qwen3.5-4b", 4000, 200, feature=usage.REVIEW)
    usage.record("lmstudio", "qwen3.5-4b", 500, 50, feature=usage.REVIEW)

    pane = UsagePane()
    model = _find(pane.totals_tree, "LM Studio").child(0)

    assert model.text(0) == "qwen3.5-4b"
    assert model.text(2) == "3"  # every call, whatever it was for
    features = {model.child(i).text(0): model.child(i).text(5) for i in range(model.childCount())}
    assert features == {"Commit message": "1,100", "Code review": "4,750"}


def test_a_call_from_before_features_were_recorded_is_shown_as_a_gap(qapp):
    usage.record("lmstudio", "qwen3.5-4b", 10, 1)

    pane = UsagePane()

    assert _rows(pane.calls_tree)[0][2] == "(unattributed)"


def test_the_copied_table_says_what_each_row_was_for(qapp):
    usage.record("lmstudio", "qwen3.5-4b", 1200, 340, feature=usage.AUDIT)
    pane = UsagePane()
    assert "| Repository audit |" in pane.to_markdown()

