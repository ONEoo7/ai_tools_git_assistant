"""The review rules as files: one per language, spans intact."""

import json

import pytest

from git_assistant.review import builtin, languages, rule_files


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        rule_files, "user_config_dir", lambda *a, **k: str(tmp_path / "config")
    )
    return tmp_path


def _write(language, data):
    path = rule_files.path_for(language)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        data if isinstance(data, str) else json.dumps(data), encoding="utf-8"
    )


# ---- writing them out ------------------------------------------------------------
def test_one_file_per_language_is_written_on_the_first_run():
    written = rule_files.ensure_files()

    assert set(written) == set(builtin.languages_covered())
    assert rule_files.path_for("cpp").is_file()
    assert rule_files.path_for("python").is_file()


def test_a_file_is_named_for_its_language_and_nothing_else():
    """One file per language, so a rule spanning versions is written once."""
    rule_files.ensure_files()

    names = sorted(p.name for p in rule_files.rules_dir().iterdir())

    assert "cpp.json" in names
    assert not any("v26" in name for name in names)
    assert len(names) == len(builtin.languages_covered())


def test_the_version_span_is_kept_in_the_file():
    """The whole reason there is one file and not seven."""
    rule_files.ensure_files()

    data = json.loads(rule_files.path_for("cpp").read_text(encoding="utf-8"))
    spans = [r for r in data["rules"] if r.get("since") or r.get("until")]

    assert spans, "the shipped C++ rules carry spans"
    assert any(r["since"] == "c++11" for r in spans)


def test_a_rule_with_no_span_does_not_get_an_empty_one():
    rule_files.ensure_files()
    data = json.loads(rule_files.path_for("cpp").read_text(encoding="utf-8"))

    unspanned = [r for r in data["rules"] if "since" not in r]

    assert unspanned
    assert all("until" not in r for r in unspanned)


def test_existing_files_are_never_rewritten():
    """A build that rewrote them on start-up would be one that ate the edit."""
    _write("cpp", {"rules": [{"id": "MINE-1", "details": "my rule"}]})

    written = rule_files.ensure_files()

    assert "cpp" not in written
    assert [r.rule_id for r in rule_files.table("cpp").rules] == ["MINE-1"]


# ---- what a review is judged by ----------------------------------------------------
def test_the_file_is_what_a_review_uses():
    rule_files.ensure_files()
    _write(
        "python",
        {"label": "Python", "rules": [{"id": "PY-MINE", "details": "my only rule"}]},
    )

    table = rule_files.table_for("python", "py312")

    assert [r.rule_id for r in table.rules] == ["PY-MINE"]
    assert table.source == "python.json"


def test_spans_still_filter_by_version():
    rule_files.ensure_files()
    _write(
        "cpp",
        {
            "rules": [
                {"id": "OLD", "details": "always"},
                {"id": "NEW", "details": "modern", "since": "c++20"},
            ]
        },
    )

    old = rule_files.table_for("cpp", "c++11")
    new = rule_files.table_for("cpp", "c++23")

    assert [r.rule_id for r in old.rules] == ["OLD"]
    assert [r.rule_id for r in new.rules] == ["OLD", "NEW"]


def test_an_edit_is_picked_up_without_a_restart():
    rule_files.ensure_files()
    first = len(rule_files.table("cpp").rules)

    _write("cpp", {"rules": [{"id": "ONE", "details": "one"}]})

    assert len(rule_files.table("cpp").rules) == 1 != first


def test_a_language_with_no_file_falls_back_to_the_shipped_rules():
    """Nothing here can leave a review with no rules."""
    shipped = builtin.get("rust")

    table = rule_files.table("rust")

    assert [r.rule_id for r in table.rules] == [r.rule_id for r in shipped.rules]
    assert rule_files.table_for("rust", "rust2021").source == "built in"


def test_a_broken_file_is_reported_and_the_shipped_rules_are_used():
    """A review that quietly checked nothing is worse than one that says why."""
    _write("cpp", '{"rules": [,]}')

    assert "valid JSON" in rule_files.problem_with("cpp")
    assert len(rule_files.table("cpp").rules) == len(builtin.get("cpp").rules)


def test_a_rule_with_no_id_is_not_a_rule():
    _write("cpp", {"rules": [{"details": "no id"}, {"id": "OK", "details": "fine"}]})
    assert [r.rule_id for r in rule_files.table("cpp").rules] == ["OK"]


def test_a_file_that_is_a_list_is_refused():
    _write("cpp", [1, 2])
    assert "object" in rule_files.problem_with("cpp")


def test_every_language_this_build_knows_about_is_covered():
    rule_files.ensure_files()
    covered = set(rule_files.languages_covered())
    assert covered == set(builtin.languages_covered())
    assert covered <= {lang.id for lang in languages.LANGUAGES}


# ---- editing them ------------------------------------------------------------------
def test_what_cannot_be_read_back_is_not_written():
    rule_files.ensure_files()
    before = rule_files.read_text("cpp")

    problem = rule_files.write_text("cpp", '{"rules": [,]}')

    assert "valid JSON" in problem
    assert rule_files.read_text("cpp") == before


def test_rules_that_are_not_a_list_are_refused():
    assert "list of rules" in rule_files.check('{"rules": {"a": 1}}')


def test_a_file_can_be_put_back_to_what_this_build_ships_with():
    rule_files.ensure_files()
    _write("cpp", {"rules": [{"id": "ONLY", "details": "mine"}]})

    assert rule_files.restore("cpp") == ""

    assert len(rule_files.table("cpp").rules) == len(builtin.get("cpp").rules)


def test_restoring_a_language_nothing_ships_for_says_so():
    assert "Nothing ships" in rule_files.restore("cobol")


def test_a_disk_that_refuses_reports_rather_than_raising(monkeypatch):
    monkeypatch.setattr(
        rule_files.Path,
        "mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    assert "read-only" in rule_files.write_text("cpp", '{"rules": []}')


# ---- what the review actually resolves ----------------------------------------------
def test_the_default_profile_resolves_through_the_files():
    """The rules a review runs on are the ones in the folder, not in the build."""
    from git_assistant.review import profiles

    rule_files.ensure_files()
    _write("python", {"rules": [{"id": "PY-EDITED", "details": "changed here"}]})
    from git_assistant.review.rules import RuleStore

    lookup = profiles.rules_for(profiles.defaults(), RuleStore(tables=[]))

    resolved = lookup("python", "py312")
    assert [r.rule_id for r in resolved.rules] == ["PY-EDITED"]
