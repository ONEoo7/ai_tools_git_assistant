"""The window shown after Review and before the first call."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant import git_ops  # noqa: E402
from git_assistant.config import Settings  # noqa: E402
from git_assistant.estimate import Estimate  # noqa: E402
from git_assistant.review import builtin  # noqa: E402
from git_assistant.review.plan import build  # noqa: E402
from git_assistant.review.rules import Rule, RuleTable  # noqa: E402
from git_assistant.ui.review_plan_dialog import ReviewPlanDialog  # noqa: E402

TABLE = RuleTable("House rules", [Rule("R-1", "no bare except")])


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _diff(path, body="+one line\n"):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


@pytest.fixture
def staged(monkeypatch):
    monkeypatch.setattr(
        git_ops,
        "get_diff",
        lambda r, m: _diff("app.py") + _diff("engine.h") + _diff("README.md"),
    )


@pytest.fixture
def settings():
    s = Settings(selected_model="m")
    s.save = lambda: None
    return s


def _plan(settings, paths=("app.py", "engine.h", "README.md"), rules_for=None):
    return build(
        settings,
        "/x/demo",
        list(paths),
        rules_for or (lambda language, version: builtin.table_for(language, version)),
        versions={"python": "py312"},
        profile="Built-in defaults",
    )


def _estimate(calls=2):
    return Estimate(
        feature="Code review",
        calls=calls,
        input_tokens=12_000,
        output_tokens=1_024,
        model="qwen3.5-4b",
        provider="lmstudio",
        lines=["One call per marked file."],
    )


def _rows(dialog):
    tree = dialog.files
    return [
        [tree.topLevelItem(i).text(c) for c in range(tree.columnCount())]
        for i in range(tree.topLevelItemCount())
    ]


# ---- what it lists ------------------------------------------------------------
def test_every_marked_file_is_listed_reviewable_or_not(qapp, settings, staged):
    dialog = ReviewPlanDialog(_plan(settings), _estimate())
    assert [row[0] for row in _rows(dialog)] == ["app.py", "engine.h", "README.md"]


def test_a_row_says_the_version_and_the_rules_that_apply(qapp, settings, staged):
    dialog = ReviewPlanDialog(_plan(settings), _estimate())

    row = [r for r in _rows(dialog) if r[0] == "app.py"][0]

    assert row[2] == "Python 3.12+"
    assert "rule(s)" in row[3] and "Python (built in)" in row[3]


def test_a_file_no_language_claims_says_why_it_will_not_be_reviewed(qapp, settings, staged):
    dialog = ReviewPlanDialog(_plan(settings), _estimate())
    row = [r for r in _rows(dialog) if r[0] == "README.md"][0]
    assert "no language" in row[3]


def test_the_rule_sets_in_use_are_named(qapp, settings, staged):
    dialog = ReviewPlanDialog(_plan(settings), _estimate())
    assert "Built-in defaults" in dialog.rule_sets.text()
    assert "Python (built in)" in dialog.rule_sets.text()


def test_the_summary_counts_what_will_and_will_not_be_reviewed(qapp, settings, staged):
    dialog = ReviewPlanDialog(_plan(settings), _estimate())
    text = dialog.summary.text()
    assert "2 file(s) will be reviewed" in text
    assert "1 will not" in text


def test_the_cost_is_shown_beside_the_files(qapp, settings, staged):
    dialog = ReviewPlanDialog(_plan(settings), _estimate())
    assert "12,000 tokens in" in dialog.summary.text()


def test_a_plan_with_nothing_in_it_cannot_be_run(qapp, settings, staged):
    plan = _plan(settings, paths=["README.md"])
    dialog = ReviewPlanDialog(plan, Estimate(feature="Code review", problem="Nothing."))
    assert not dialog.run_btn.isEnabled()


# ---- correcting a language ------------------------------------------------------
def _picker(dialog, path):
    tree = dialog.files
    for i in range(tree.topLevelItemCount()):
        if tree.topLevelItem(i).text(0) == path:
            return tree.itemWidget(tree.topLevelItem(i), 1)
    raise AssertionError(path)


def test_the_language_of_a_file_can_be_corrected(qapp, settings, staged):
    """`.h` is C or C++, and only this repository knows which."""
    dialog = ReviewPlanDialog(_plan(settings), _estimate())
    dialog.rules_for = lambda language, version: builtin.table_for(language, version)

    picker = _picker(dialog, "engine.h")
    picker.setCurrentIndex(picker.findData("cpp"))

    row = [r for r in _rows(dialog) if r[0] == "engine.h"][0]
    assert "C++ (built in)" in row[3]


def test_correcting_a_language_changes_what_that_file_will_be_checked_against(
    qapp, settings, staged
):
    dialog = ReviewPlanDialog(_plan(settings), _estimate())
    dialog.rules_for = lambda language, version: builtin.table_for(language, version)

    picker = _picker(dialog, "README.md")
    picker.setCurrentIndex(picker.findData("python"))

    readme = [f for f in dialog.plan.files if f.path == "README.md"][0]
    assert readme.reviewable
    assert readme.table.find("PY-01") is not None


def test_a_file_can_be_dropped_by_choosing_no_language(qapp, settings, staged):
    dialog = ReviewPlanDialog(_plan(settings), _estimate())
    dialog.rules_for = lambda language, version: builtin.table_for(language, version)

    picker = _picker(dialog, "app.py")
    picker.setCurrentIndex(picker.findData(""))

    assert "app.py" not in [f.path for f in dialog.plan.reviewable()]
    row = [r for r in _rows(dialog) if r[0] == "app.py"][0]
    assert "not reviewed" in row[3]


def test_the_cost_is_re_read_when_a_language_changes(qapp, settings, staged):
    asked = []
    dialog = ReviewPlanDialog(_plan(settings), _estimate())
    dialog.rules_for = lambda language, version: builtin.table_for(language, version)
    dialog.price = lambda plan: asked.append(plan) or _estimate(calls=9)

    picker = _picker(dialog, "engine.h")
    picker.setCurrentIndex(picker.findData("cpp"))

    assert asked, "the window must not quote a price for a plan it has changed"
    assert "9 call(s)" in dialog.summary.text()


def test_the_corrections_are_reported_by_extension(qapp, settings, staged):
    """`.h` is settled for a repository, not for one file of it."""
    dialog = ReviewPlanDialog(_plan(settings), _estimate())
    dialog.rules_for = lambda language, version: builtin.table_for(language, version)

    picker = _picker(dialog, "engine.h")
    picker.setCurrentIndex(picker.findData("cpp"))

    assert dialog.overrides()[".h"] == "cpp"
