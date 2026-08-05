"""What a review will do, decided before any of it is done."""

import pytest

from git_assistant import git_ops
from git_assistant.config import Settings
from git_assistant.review import builtin, languages
from git_assistant.review.plan import ReviewPlan, build
from git_assistant.review.reviewer import staged_files
from git_assistant.review.rules import Rule, RuleTable

TABLE = RuleTable("House rules", [Rule("R-1", "no bare except")])


def _diff(path, body="+one line\n"):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -0,0 +1 @@\n{body}"


@pytest.fixture
def settings():
    s = Settings(selected_model="m")
    s.save = lambda: None
    s.active_repo = "/x/demo"
    return s


@pytest.fixture
def staged(monkeypatch):
    """A Python file, a C++ header, a lockfile and a readme."""

    def diff(repo, mode):
        return (
            _diff("app.py")
            + _diff("engine.h", "+namespace engine {\n")
            + _diff("uv.lock")
            + _diff("README.md")
        )

    monkeypatch.setattr(git_ops, "get_diff", diff)


def _rules_for_python(language, version):
    return TABLE if language == "python" else None


# ---- what each file is -------------------------------------------------------------
def test_every_marked_file_gets_a_language(settings, staged):
    plan = build(settings, "/x/demo", ["app.py", "engine.h"], lambda lang, v: TABLE)

    assert {f.path: f.language for f in plan.files} == {
        "app.py": "python",
        "engine.h": "cpp",  # from its own first lines
    }


def test_a_file_that_was_not_marked_is_not_in_the_plan(settings, staged):
    plan = build(settings, "/x/demo", ["app.py"], lambda lang, v: TABLE)
    assert [f.path for f in plan.files] == ["app.py"]


def test_a_file_filtered_as_noise_is_listed_and_not_reviewable(settings, staged):
    settings.ignore_globs = ["*.lock"]
    plan = build(settings, "/x/demo", ["app.py", "uv.lock"], lambda lang, v: TABLE)

    lock = [f for f in plan.files if f.path == "uv.lock"][0]
    assert lock.reviewable is False
    assert "noise" in lock.skipped


def test_a_file_no_language_claims_is_listed_with_its_reason(settings, staged):
    plan = build(settings, "/x/demo", ["README.md"], lambda lang, v: TABLE)

    readme = plan.files[0]
    assert readme.reviewable is False
    assert "no language" in readme.skipped
    assert plan.reviewable() == []


def test_a_language_the_profile_has_no_rules_for_is_not_sent(settings, staged):
    """An empty rules block reads to a model as a clean file."""
    plan = build(settings, "/x/demo", ["app.py", "engine.h"], _rules_for_python)

    assert [f.path for f in plan.reviewable()] == ["app.py"]
    header = [f for f in plan.files if f.path == "engine.h"][0]
    assert "no rules apply to C++" in header.skipped


# ---- versions ------------------------------------------------------------------------
def test_the_version_a_repository_declares_reaches_the_file(settings, staged):
    plan = build(
        settings,
        "/x/demo",
        ["app.py"],
        lambda lang, v: TABLE,
        versions={"python": "py312"},
    )

    assert plan.files[0].version == "py312"
    assert plan.files[0].version_label() == "Python 3.12+"


def test_the_version_is_what_the_rules_are_asked_for(settings, staged):
    asked = []
    build(
        settings,
        "/x/demo",
        ["app.py"],
        lambda lang, v: asked.append((lang, v)) or TABLE,
        versions={"python": "py38"},
    )
    assert asked == [("python", "py38")]


def test_a_language_with_no_version_is_left_without_one(settings, staged):
    plan = build(settings, "/x/demo", ["app.py"], lambda lang, v: TABLE)
    assert plan.files[0].version == ""
    assert plan.files[0].version_label() == ""


def test_the_rules_for_one_language_are_worked_out_once(settings, staged):
    """One table per language and version, not one per file."""
    calls = []

    def rules_for(language, version):
        calls.append(language)
        return TABLE

    monkeypatched = build(
        settings, "/x/demo", ["app.py", "engine.h"], rules_for
    )
    assert monkeypatched.reviewable()
    assert calls == ["python", "cpp"]


# ---- the answer to an ambiguous extension --------------------------------------------
def test_a_header_is_settled_by_the_repository_when_its_own_lines_say_nothing(
    settings, monkeypatch
):
    monkeypatch.setattr(
        git_ops, "get_diff", lambda r, m: _diff("thing.h") + _diff("main.cpp")
    )
    plan = build(settings, "/x/demo", ["thing.h"], lambda lang, v: TABLE)
    assert plan.files[0].language == "cpp"


def test_an_answer_already_given_settles_it(settings, monkeypatch):
    monkeypatch.setattr(git_ops, "get_diff", lambda r, m: _diff("thing.h"))
    plan = build(
        settings, "/x/demo", ["thing.h"], lambda lang, v: TABLE, overrides={".h": "cpp"}
    )
    assert plan.files[0].language == "cpp"


# ---- what the plan says about itself ---------------------------------------------------
def test_the_plan_lists_the_languages_and_the_tables_in_use(settings, staged):
    def rules_for(language, version):
        return builtin.table_for(language, version) or TABLE

    plan = build(settings, "/x/demo", ["app.py", "engine.h"], rules_for)

    assert plan.languages() == ["python", "cpp"]
    assert plan.tables() == ["Python (built in)", "C++ (built in)"]


def test_the_fingerprint_covers_every_table_the_plan_uses(settings, staged):
    one = build(settings, "/x/demo", ["app.py"], lambda lang, v: TABLE)
    other = build(
        settings,
        "/x/demo",
        ["app.py"],
        lambda lang, v: RuleTable("House rules", [Rule("R-1", "something else")]),
    )
    assert one.fingerprint() != other.fingerprint()


def test_a_row_says_what_its_file_will_be_checked_against(settings, staged):
    plan = build(settings, "/x/demo", ["app.py", "README.md"], _rules_for_python)

    rows = {f.path: f.rules_label() for f in plan.files}
    assert rows["app.py"] == "1 rule(s) - House rules"
    assert "no language" in rows["README.md"]


# ---- one table for everything, which is what a review was before profiles -------------
def test_one_table_can_still_cover_every_file(settings, staged):
    found = staged_files("/x/demo", "cached", settings.ignore_globs)
    plan = ReviewPlan.of_table("/x/demo", found, TABLE)

    assert plan.profile == "House rules"
    assert [f.language for f in plan.reviewable()] == [languages.ANY] * len(
        plan.reviewable()
    )
    assert all(f.table is TABLE for f in plan.reviewable())


def test_one_table_with_no_rules_reviews_nothing(settings, staged):
    found = staged_files("/x/demo", "cached", settings.ignore_globs)
    plan = ReviewPlan.of_table("/x/demo", found, RuleTable("Empty"))
    assert plan.reviewable() == []
