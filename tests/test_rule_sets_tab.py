"""The Rule Sets tab: every set a profile can draw on, shipped or your own."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.review import languages, rule_files  # noqa: E402
from git_assistant.review.rules import Rule, RuleStore, RuleTable  # noqa: E402
from git_assistant.ui.rule_sets_tab import RuleSetsTab, applies_label  # noqa: E402

HOUSE = RuleTable("House rules", [Rule("H-1", "one"), Rule("H-2", "two")], source="x.xlsx")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def config_dir(tmp_path, monkeypatch):
    """The per-language files are written under here, never the real config."""
    monkeypatch.setattr(rule_files, "user_config_dir", lambda *a, **k: str(tmp_path))
    return tmp_path


def _tab(store=None):
    tab = RuleSetsTab()
    tab.show_sets(store if store is not None else RuleStore())
    return tab


def _refs(tab, group):
    head = tab.sets_tree.topLevelItem(group)
    return [
        head.child(i).data(0, Qt.ItemDataRole.UserRole) for i in range(head.childCount())
    ]


# ---- what is listed -------------------------------------------------------------
def test_both_kinds_of_rule_set_are_listed_together(qapp):
    tab = _tab(RuleStore([HOUSE]))
    assert tab.sets_tree.topLevelItem(0).text(0) == "Built in"
    assert tab.sets_tree.topLevelItem(1).text(0) == "Mine"


def test_every_shipped_language_has_a_set(qapp):
    tab = _tab()
    assert _refs(tab, 0) == [f"builtin:{one}" for one in rule_files.languages_covered()]


def test_a_table_of_your_own_is_listed_by_name(qapp):
    tab = _tab(RuleStore([HOUSE]))
    assert _refs(tab, 1) == ["table:House rules"]


def test_having_no_tables_of_your_own_says_so_rather_than_showing_nothing(qapp):
    tab = _tab()
    mine = tab.sets_tree.topLevelItem(1)
    assert mine.childCount() == 1
    assert mine.child(0).text(0) == "none yet"
    assert mine.child(0).data(0, Qt.ItemDataRole.UserRole) is None


def test_the_rule_count_is_shown_beside_each_set(qapp):
    tab = _tab(RuleStore([HOUSE]))
    mine = tab.sets_tree.topLevelItem(1)
    assert mine.child(0).text(1) == "2"


def test_a_group_heading_is_not_something_you_can_pick(qapp):
    tab = _tab()
    assert not tab.sets_tree.topLevelItem(0).flags() & Qt.ItemFlag.ItemIsSelectable


# ---- what the selected set shows ---------------------------------------------------
def test_it_opens_on_a_set_rather_than_on_nothing(qapp):
    """A tab whose right half is blank until you click reads as broken."""
    tab = _tab()
    assert tab.current_ref().startswith("builtin:")
    assert tab.rules_table.rowCount() > 0


def test_a_built_in_set_shows_the_versions_a_rule_applies_to(qapp):
    tab = _tab()
    tab.show_sets(RuleStore(), current="builtin:python")
    spans = [
        tab.rules_table.item(row, 2).text() for row in range(tab.rules_table.rowCount())
    ]
    assert any(one.endswith("and later") for one in spans)


def test_a_rule_true_of_every_version_says_nothing_rather_than_any(qapp):
    """A span is a claim; "any" would be one nobody made."""
    tab = _tab()
    tab.show_sets(RuleStore(), current="builtin:python")
    spans = [
        tab.rules_table.item(row, 2).text() for row in range(tab.rules_table.rowCount())
    ]
    assert "" in spans


def test_the_file_behind_a_built_in_set_is_named(qapp, config_dir):
    """A rule set nobody can find is one nobody can correct."""
    tab = _tab()
    tab.show_sets(RuleStore(), current="builtin:python")
    assert str(rule_files.path_for("python")) in tab.rules_note.text()


def test_one_of_your_own_shows_where_it_was_imported_from(qapp):
    tab = _tab(RuleStore([HOUSE]))
    tab.show_sets(RuleStore([HOUSE]), current="table:House rules")
    assert "2 rule(s)" in tab.rules_note.text()
    assert "x.xlsx" in tab.rules_note.text()


def test_one_of_your_own_carries_no_version_span(qapp):
    """Only the shipped rules have one, and a blank cell says so honestly."""
    store = RuleStore([HOUSE])
    tab = _tab(store)
    tab.show_sets(store, current="table:House rules")
    assert [tab.rules_table.item(r, 2).text() for r in range(2)] == ["", ""]


# ---- which buttons apply to which kind ----------------------------------------------
def test_a_built_in_set_is_opened_and_reset_not_renamed_and_deleted(qapp):
    tab = _tab(RuleStore([HOUSE]))
    tab.show_sets(RuleStore([HOUSE]), current="builtin:python")

    assert tab.open_file_btn.isEnabled()
    assert tab.restore_btn.isEnabled()
    assert not tab.rename_table_btn.isEnabled()
    assert not tab.delete_table_btn.isEnabled()


def test_one_of_your_own_is_renamed_and_deleted_not_reset(qapp):
    store = RuleStore([HOUSE])
    tab = _tab(store)
    tab.show_sets(store, current="table:House rules")

    assert tab.rename_table_btn.isEnabled()
    assert tab.delete_table_btn.isEnabled()
    assert tab.export_xlsx_btn.isEnabled()
    assert not tab.open_file_btn.isEnabled()
    assert not tab.restore_btn.isEnabled()


def test_importing_is_always_offered(qapp):
    """There is nothing to select before importing the first table."""
    tab = _tab()
    assert tab.import_xlsx_btn.isEnabled()
    assert tab.import_json_btn.isEnabled()
    assert tab.open_folder_btn.isEnabled()


# ---- what the owner asks it -----------------------------------------------------
def test_the_selected_set_is_named_the_way_a_profile_names_one(qapp):
    """`review_panel` hands these straight to a Selection, so they must match."""
    store = RuleStore([HOUSE])
    tab = _tab(store)
    tab.show_sets(store, current="table:House rules")

    assert tab.current_ref() == "table:House rules"
    assert tab.current_table_name() == "House rules"
    assert tab.current_language() == ""


def test_a_built_in_set_answers_with_its_language_and_no_table_name(qapp):
    tab = _tab()
    tab.show_sets(RuleStore(), current="builtin:cpp")

    assert tab.current_language() == "cpp"
    assert tab.current_table_name() == ""


def test_a_set_that_is_gone_falls_back_rather_than_leaving_the_pane_blank(qapp):
    """A table deleted a moment ago must not leave the tab showing nothing."""
    tab = _tab(RuleStore([HOUSE]))
    tab.show_sets(RuleStore(), current="table:House rules")

    assert tab.current_ref().startswith("builtin:")
    assert tab.rules_table.rowCount() > 0


def test_redrawing_does_not_look_like_the_user_picking_something(qapp):
    """Otherwise the owner redraws the tab it is in the middle of filling."""
    tab = _tab()
    picked = []
    tab.selected.connect(picked.append)

    tab.show_sets(RuleStore([HOUSE]))

    assert picked == []


# ---- a file somebody has broken ------------------------------------------------
def test_an_unreadable_file_is_reported_where_its_rules_are_shown(qapp, config_dir):
    rule_files.ensure_files()
    rule_files.path_for("python").write_text("{ not json", encoding="utf-8")

    tab = _tab()
    tab.show_sets(RuleStore(), current="builtin:python")

    assert "could not be read" in tab.rules_note.text()
    # And the shipped rules are shown, so the tab is never empty over a typo.
    assert tab.rules_table.rowCount() > 0


def test_an_unreadable_file_is_counted_in_the_note_beside_the_list(qapp, config_dir):
    rule_files.ensure_files()
    rule_files.path_for("rust").write_text("{ not json", encoding="utf-8")

    tab = _tab()

    assert "could not be read" in tab.shipped_rules_note.text()
    assert "rust" in tab.shipped_rules_note.text()


# ---- how a version span is written ------------------------------------------------
def test_a_span_is_written_the_way_someone_would_say_it():
    cpp = languages.get("cpp")
    assert applies_label(cpp, Rule("x", "y")) == ""
    assert applies_label(cpp, _spanned(since="c++20")) == "C++20 and later"
    assert applies_label(cpp, _spanned(until="c++11")) == "up to C++11"
    assert applies_label(cpp, _spanned(since="c++11", until="c++17")) == "C++11 to C++17"


def _spanned(since="", until=""):
    from git_assistant.review.builtin import BuiltinRule

    return BuiltinRule(rule_id="X-1", details="d", since=since, until=until)
