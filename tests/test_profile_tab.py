"""The Profile tab: languages, versions and which rules are checked."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.review.profiles import (  # noqa: E402
    LanguageRules,
    Profile,
    Selection,
    resolve,
)
from git_assistant.review.rules import Rule, RuleStore, RuleTable  # noqa: E402
from git_assistant.ui.profile_tab import ProfileTab  # noqa: E402

HOUSE = RuleTable("House rules", [Rule("H-1", "one"), Rule("H-2", "two")])


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def store():
    return RuleStore([HOUSE])


def _profile():
    return Profile(
        name="Mine",
        languages=[
            LanguageRules("python", selections=[Selection("builtin:python")]),
            LanguageRules("cpp", version="c++17", selections=[Selection("builtin:cpp")]),
        ],
    )


def _tab(qapp, profile=None, store=None, detected=None, sources=None):
    tab = ProfileTab()
    tab.show_profile(
        profile if profile is not None else _profile(),
        store if store is not None else RuleStore([HOUSE]),
        detected or {},
        sources or {},
    )
    tab.attach_version_pickers()
    return tab


def _row(tab, label):
    for index in range(tab.tree.topLevelItemCount()):
        item = tab.tree.topLevelItem(index)
        if item.text(0) == label:
            return item
    raise AssertionError(label)


# ---- what it shows -----------------------------------------------------------
def test_a_row_per_language_the_profile_covers(qapp, store):
    tab = _tab(qapp, store=store)
    labels = [tab.tree.topLevelItem(i).text(0) for i in range(tab.tree.topLevelItemCount())]
    assert labels == ["Python", "C++"]


def test_each_language_lists_the_rules_that_will_be_checked(qapp, store):
    tab = _tab(qapp, store=store)
    python = _row(tab, "Python")

    table_row = python.child(0)
    assert table_row.text(0) == "Python (built in)"
    assert table_row.childCount() > 0
    assert table_row.child(0).text(0).startswith("PY-")


def test_the_version_decides_which_rules_are_listed(qapp, store):
    old = _tab(qapp, profile=Profile("Mine", [
        LanguageRules("python", version="py2", selections=[Selection("builtin:python")])
    ]), store=store)
    new = _tab(qapp, profile=Profile("Mine", [
        LanguageRules("python", version="py312", selections=[Selection("builtin:python")])
    ]), store=store)

    assert _row(old, "Python").child(0).childCount() < _row(new, "Python").child(0).childCount()


def test_a_table_that_is_gone_says_so_rather_than_showing_nothing(qapp):
    profile = Profile("Mine", [LanguageRules("python", selections=[Selection("table:Gone")])])
    tab = _tab(qapp, profile=profile, store=RuleStore())

    assert "missing" in _row(tab, "Python").child(0).text(0)


# ---- versions ------------------------------------------------------------------
def test_a_version_can_be_chosen_for_a_language(qapp, store):
    tab = _tab(qapp, store=store)
    picker = tab.tree.itemWidget(_row(tab, "Python"), 1)

    picker.setCurrentIndex(picker.findData("py38"))

    entry = [e for e in tab._profile.languages if e.language == "python"][0]
    assert entry.version == "py38"


def test_what_the_repository_declares_is_offered_as_the_default(qapp, store):
    tab = _tab(
        qapp,
        store=store,
        detected={"python": "py312"},
        sources={"python": "from pyproject.toml: >=3.12"},
    )
    picker = tab.tree.itemWidget(_row(tab, "Python"), 1)

    assert picker.currentData() == "", "detected, not pinned"
    assert "Python 3.12+" in picker.itemText(0) and "detected" in picker.itemText(0)
    assert "pyproject" in picker.toolTip()


def test_a_language_nobody_declared_a_version_for_says_every_rule_applies(qapp, store):
    tab = _tab(qapp, store=store)
    picker = tab.tree.itemWidget(_row(tab, "Python"), 1)

    assert "every rule applies" in picker.itemText(0)
    assert "No version is set" in tab.note.text()


def test_choosing_a_version_is_reported_so_it_can_be_saved(qapp, store):
    tab = _tab(qapp, store=store)
    seen = []
    tab.changed.connect(lambda: seen.append(True))

    picker = tab.tree.itemWidget(_row(tab, "Python"), 1)
    picker.setCurrentIndex(picker.findData("py310"))

    assert seen


# ---- which rules are checked -------------------------------------------------------
def test_unticking_a_rule_leaves_it_out_of_the_review(qapp, store):
    profile = _profile()
    tab = _tab(qapp, profile=profile, store=store)
    rule_row = _row(tab, "Python").child(0).child(0)
    rule_id = rule_row.text(0).split(":", 1)[0]

    rule_row.setCheckState(0, Qt.CheckState.Unchecked)

    selection = profile.languages[0].selections[0]
    assert rule_id in selection.exclude
    assert resolve(profile, store, "python", "").find(rule_id) is None


def test_ticking_it_again_puts_it_back(qapp, store):
    profile = _profile()
    tab = _tab(qapp, profile=profile, store=store)
    rule_row = _row(tab, "Python").child(0).child(0)

    rule_row.setCheckState(0, Qt.CheckState.Unchecked)
    rule_row.setCheckState(0, Qt.CheckState.Checked)

    assert profile.languages[0].selections[0].exclude == []


def test_a_rule_already_turned_off_is_shown_unticked(qapp, store):
    profile = Profile("Mine", [
        LanguageRules("python", selections=[Selection("builtin:python", ["PY-01"])])
    ])
    tab = _tab(qapp, profile=profile, store=store)

    first = _row(tab, "Python").child(0).child(0)
    assert first.text(0).startswith("PY-01")
    assert first.checkState(0) == Qt.CheckState.Unchecked


# ---- adding and removing a language ---------------------------------------------------
def test_a_language_can_be_added_with_whatever_ships_for_it(qapp, store):
    profile = Profile("Mine", [])
    tab = _tab(qapp, profile=profile, store=store)

    tab.add_language("rust")

    assert [e.language for e in profile.languages] == ["rust"]
    assert _row(tab, "Rust").child(0).text(0) == "Rust (built in)"


def test_only_the_languages_not_covered_yet_are_offered(qapp, store):
    tab = _tab(qapp, store=store)
    assert "python" not in tab.missing_languages()
    assert "rust" in tab.missing_languages()


def test_a_language_can_be_removed(qapp, store):
    profile = _profile()
    tab = _tab(qapp, profile=profile, store=store)
    tab.tree.setCurrentItem(_row(tab, "C++"))

    tab.remove_language()

    assert [e.language for e in profile.languages] == ["python"]


# ---- a profile that came from the repository -------------------------------------------
def test_a_repository_s_profile_is_read_only_here(qapp, store):
    profile = _profile()
    profile.source = "repository"
    tab = _tab(qapp, profile=profile, store=store)

    assert not tab.share_btn.isEnabled()
    assert not tab.add_language_btn.isEnabled()
    assert tab.copy_btn.isVisibleTo(tab)
    assert "read-only" in tab.header.text()


def test_no_profile_selected_says_so(qapp, store):
    tab = ProfileTab()
    tab.show_profile(None, store, {})
    assert "No profile" in tab.header.text()
