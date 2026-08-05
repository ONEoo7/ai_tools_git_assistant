"""The rule tables: kept between sessions, moved between machines."""

import json

import pytest

from git_assistant.review import rules as rules_mod
from git_assistant.review.rules import Rule, RuleStore, RuleTable


@pytest.fixture(autouse=True)
def store_dir(tmp_path, monkeypatch):
    """Redirect the store; patched where it is imported, as test_identity does."""
    monkeypatch.setattr(rules_mod, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


def _table(name="House rules", count=3):
    return RuleTable(
        name=name,
        rules=[Rule(f"R-{i}", f"rule number {i}") for i in range(1, count + 1)],
        source="rules.xlsx",
    )


# ---- round trip -----------------------------------------------------------------
def test_a_table_comes_back_exactly_as_it_went_in():
    store = RuleStore()
    store.add(_table())

    reloaded = RuleStore.load()

    assert reloaded.names() == ["House rules"]
    table = reloaded.find("House rules")
    assert [r.rule_id for r in table.rules] == ["R-1", "R-2", "R-3"]
    assert table.rules[0].details == "rule number 1"
    assert table.source == "rules.xlsx"


def test_an_imported_table_records_when_it_arrived():
    store = RuleStore()
    stored = store.add(_table())
    assert stored.imported_at.endswith("Z")


def test_a_missing_store_file_reads_as_no_tables():
    assert RuleStore.load().tables == []


def test_a_corrupt_store_file_reads_as_no_tables_rather_than_raising(store_dir):
    (store_dir / rules_mod.RULES_FILE).write_text("{not json", encoding="utf-8")
    assert RuleStore.load().tables == []


def test_a_hand_written_bare_list_still_loads(store_dir):
    (store_dir / rules_mod.RULES_FILE).write_text(
        json.dumps([{"name": "By hand", "rules": [{"rule_id": "A", "details": "b"}]}]),
        encoding="utf-8",
    )
    assert RuleStore.load().find("By hand").rules[0].rule_id == "A"


def test_a_rule_without_an_id_is_not_a_rule(store_dir):
    (store_dir / rules_mod.RULES_FILE).write_text(
        json.dumps({"tables": [{"name": "T", "rules": [{"details": "orphan"}]}]}),
        encoding="utf-8",
    )
    assert RuleStore.load().find("T").rules == []


def test_saving_leaves_no_temporary_file_behind(store_dir):
    RuleStore().add(_table())
    assert [p.name for p in store_dir.iterdir()] == [rules_mod.RULES_FILE]


# ---- naming ----------------------------------------------------------------------
def test_a_second_table_of_the_same_name_is_kept_under_a_free_one():
    store = RuleStore()
    store.add(_table("Python"))
    second = store.add(_table("Python"))

    assert second.name == "Python (2)"
    assert store.names() == ["Python", "Python (2)"]


def test_renaming_refuses_to_collide_with_another_table():
    store = RuleStore()
    store.add(_table("Python"))
    store.add(_table("Go"))

    assert store.rename("Go", "Python") is False
    assert store.names() == ["Python", "Go"]


def test_a_removed_table_is_gone_from_the_file_too():
    store = RuleStore()
    store.add(_table("Python"))
    assert store.remove("Python") is True
    assert RuleStore.load().tables == []


# ---- fingerprint -----------------------------------------------------------------
def test_the_fingerprint_changes_when_a_rule_changes():
    before = _table().fingerprint()
    table = _table()
    table.rules[1].details = "something else"
    assert table.fingerprint() != before


def test_the_fingerprint_ignores_the_name_the_table_is_filed_under():
    assert _table("A").fingerprint() == _table("B").fingerprint()


# ---- matching a rule id the way a model spells it ----------------------------------
@pytest.mark.parametrize("spelling", ["R-1", "r 1", "R_1", "r1", " R-1 "])
def test_a_rule_id_is_found_however_the_model_punctuates_it(spelling):
    assert _table().find(spelling).details == "rule number 1"


def test_an_id_that_is_not_in_the_table_is_not_invented():
    assert _table().find("R-99") is None


# ---- transfer ---------------------------------------------------------------------
def test_exporting_and_importing_round_trips_a_table(tmp_path):
    RuleStore([_table()]).export_to(tmp_path / "out.json")

    store = RuleStore()
    added, renamed = store.import_from(tmp_path / "out.json")

    assert (added, renamed) == (1, 0)
    assert [r.rule_id for r in store.find("House rules").rules] == ["R-1", "R-2", "R-3"]


def test_importing_does_not_delete_tables_that_only_exist_here(tmp_path):
    RuleStore([_table("From work")]).export_to(tmp_path / "out.json")
    store = RuleStore()
    store.add(_table("Mine"))

    store.import_from(tmp_path / "out.json")

    assert store.names() == ["Mine", "From work"]


def test_an_import_never_overwrites_the_rules_a_repository_is_reviewed_against(tmp_path):
    """An old export must not quietly rewrite a table still in use."""
    RuleStore([_table("Python", count=1)]).export_to(tmp_path / "old.json")
    store = RuleStore()
    store.add(_table("Python", count=5))

    added, renamed = store.import_from(tmp_path / "old.json")

    assert (added, renamed) == (1, 1)
    assert len(store.find("Python").rules) == 5
    assert len(store.find("Python (2)").rules) == 1


def test_exporting_one_table_leaves_the_others_out_of_the_file(tmp_path):
    store = RuleStore()
    store.add(_table("Python"))
    store.add(_table("Go"))

    store.export_to(tmp_path / "one.json", name="Go")

    assert [t.name for t in rules_mod.read_file(tmp_path / "one.json")] == ["Go"]


def test_importing_a_file_that_is_not_ours_adds_nothing_and_does_not_raise(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("nope", encoding="utf-8")
    assert RuleStore().import_from(bad) == (0, 0)
