"""Which rules a repository is reviewed against, per language and version."""

import pytest

from git_assistant.config import RepoEntry, Settings
from git_assistant.review import builtin, languages, profiles
from git_assistant.review.profiles import (
    LanguageRules,
    Profile,
    Selection,
    defaults,
    imported,
    resolve,
)
from git_assistant.review.rules import Rule, RuleStore, RuleTable

HOUSE = RuleTable("House rules", [Rule("H-1", "one"), Rule("H-2", "two")])


@pytest.fixture
def store():
    return RuleStore([HOUSE])


def _profile(*entries, name="Mine", **kw):
    return Profile(name=name, languages=list(entries), **kw)


# ---- what a profile answers ----------------------------------------------------
def test_a_language_gets_the_rules_named_for_it(store):
    profile = _profile(
        LanguageRules("python", selections=[Selection("builtin:python")]),
        LanguageRules("rust", selections=[Selection("builtin:rust")]),
    )

    table = resolve(profile, store, "python", "py312")

    assert table.name == "Python (built in)"
    assert table.find("PY-01") is not None
    assert table.find("RS-01") is None


def test_a_language_with_no_entry_of_its_own_falls_to_the_any_entry(store):
    profile = _profile(LanguageRules(languages.ANY, selections=[Selection("table:House rules")]))

    for language in ("python", "rust", "cobol"):
        table = resolve(profile, store, language, "")
        assert table.find("H-1") is not None, language


def test_a_language_nothing_covers_has_no_rules(store):
    profile = _profile(LanguageRules("python", selections=[Selection("builtin:python")]))
    assert resolve(profile, store, "rust", "") is None


def test_the_version_filters_the_rules_that_are_sent(store):
    profile = _profile(LanguageRules("python", selections=[Selection("builtin:python")]))

    old = resolve(profile, store, "python", "py2")
    new = resolve(profile, store, "python", "py312")

    assert len(old.rules) < len(new.rules)
    assert old.find("PY-06") is None  # f-strings are 3.6 and later


def test_two_rule_sets_are_merged_into_one_table(store):
    profile = _profile(
        LanguageRules(
            "python",
            selections=[Selection("builtin:python"), Selection("table:House rules")],
        )
    )

    table = resolve(profile, store, "python", "py312")

    assert table.find("PY-01") is not None
    assert table.find("H-1") is not None
    assert "+" in table.name


def test_a_rule_turned_off_is_not_sent(store):
    profile = _profile(
        LanguageRules(
            "python", selections=[Selection("builtin:python", exclude=["PY-06", "PY-07"])]
        )
    )

    table = resolve(profile, store, "python", "py312")

    assert table.find("PY-06") is None
    assert table.find("PY-01") is not None


def test_a_rule_is_turned_off_however_its_id_is_punctuated(store):
    profile = _profile(
        LanguageRules("python", selections=[Selection("builtin:python", exclude=["py 01"])])
    )
    assert resolve(profile, store, "python", "").find("PY-01") is None


def test_one_id_is_one_rule_whoever_wrote_it(store):
    """A user's PY-01 and the shipped PY-01 are one rule to the matcher."""
    mine = RuleTable("Mine", [Rule("PY-01", "my own wording")])
    profile = _profile(
        LanguageRules(
            "python", selections=[Selection("table:Mine"), Selection("builtin:python")]
        )
    )

    table = resolve(profile, RuleStore([mine]), "python", "")

    assert [r.details for r in table.rules if r.rule_id == "PY-01"] == ["my own wording"]


def test_a_selection_naming_a_table_that_is_gone_is_skipped(store):
    profile = _profile(
        LanguageRules(
            "python",
            selections=[Selection("table:Deleted"), Selection("builtin:python")],
        )
    )
    assert resolve(profile, store, "python", "").find("PY-01") is not None


def test_a_profile_whose_tables_are_all_gone_has_no_rules(store):
    profile = _profile(LanguageRules("python", selections=[Selection("table:Deleted")]))
    assert resolve(profile, store, "python", "") is None


def test_a_shared_profile_s_own_tables_are_preferred_to_a_local_one_of_the_same_name(store):
    """A clone is reviewed against what the repository shipped."""
    theirs = RuleTable("House rules", [Rule("T-1", "the repository's own")])
    profile = _profile(LanguageRules("python", selections=[Selection("table:House rules")]))

    table = resolve(
        profile, store, "python", "", inlined={"table:House rules": theirs}
    )

    assert table.find("T-1") is not None
    assert table.find("H-1") is None


# ---- the profiles every install has ------------------------------------------------
def test_the_default_profile_covers_every_language_that_ships_rules():
    profile = defaults()
    assert {e.language for e in profile.languages} == set(builtin.languages_covered())


def test_the_imported_profile_is_one_table_for_every_file():
    """What a review did before profiles: the same rules whatever the file is."""
    profile = imported("House rules")

    assert profile.name == "House rules (imported)"
    assert [e.language for e in profile.languages] == [languages.ANY]
    assert profile.covers("rust") and profile.covers("python")


def test_an_imported_profile_reviews_a_file_exactly_as_that_table_did(store):
    table = resolve(imported("House rules"), store, "python", "py312")
    assert [r.rule_id for r in table.rules] == ["H-1", "H-2"]


# ---- versions and overrides ----------------------------------------------------------
def test_a_profile_remembers_the_version_of_each_language():
    profile = _profile(
        LanguageRules("python", version="py312"),
        LanguageRules("cpp", version="c++17"),
        LanguageRules(languages.ANY, version="ignored"),
    )
    assert profiles.versions_of(profile) == {"python": "py312", "cpp": "c++17"}


def test_a_settled_extension_is_remembered_on_the_profile():
    profile = _profile(overrides={".h": "cpp"})
    assert profile.overrides[".h"] == "cpp"


# ---- keeping the pointers honest -------------------------------------------------------
def test_renaming_a_table_repoints_every_profile_that_used_it(store):
    profile = _profile(LanguageRules("python", selections=[Selection("table:House rules")]))

    profiles.rename_table([profile], "House rules", "Team rules")

    assert profile.languages[0].selections[0].ref == "table:Team rules"


def test_renaming_leaves_the_shipped_tables_alone():
    profile = _profile(LanguageRules("python", selections=[Selection("builtin:python")]))
    profiles.rename_table([profile], "python", "something")
    assert profile.languages[0].selections[0].ref == "builtin:python"


def test_deleting_a_table_drops_the_selections_that_named_it():
    profile = _profile(
        LanguageRules(
            "python",
            selections=[Selection("table:House rules"), Selection("builtin:python")],
        )
    )

    profiles.remove_table([profile], "House rules")

    assert [s.ref for s in profile.languages[0].selections] == ["builtin:python"]


# ---- storage --------------------------------------------------------------------------
def test_a_profile_survives_a_round_trip_through_the_settings_file():
    profile = _profile(
        LanguageRules(
            "python", version="py312", selections=[Selection("builtin:python", ["PY-06"])]
        ),
        overrides={".h": "cpp"},
    )
    s = Settings(review_profiles=[profile], repos=[RepoEntry("/x/a", review_profile="Mine")])

    back = Settings.from_dict(s.to_dict())

    kept = back.review_profiles[0]
    assert kept.name == "Mine"
    assert kept.languages[0].version == "py312"
    assert kept.languages[0].selections[0].exclude == ["PY-06"]
    assert kept.overrides == {".h": "cpp"}
    assert back.repos[0].review_profile == "Mine"


def test_a_profile_with_no_name_is_not_a_profile():
    assert Profile.from_dict({"languages": []}) is None
    assert Profile.from_dict("nonsense") is None


def test_a_hand_edited_profile_keeps_what_can_be_read():
    profile = Profile.from_dict(
        {"name": "Hand written", "languages": [{"language": "python"}, {"nope": 1}]}
    )
    assert [e.language for e in profile.languages] == ["python"]


def test_the_profile_a_repository_uses_is_asked_of_the_settings():
    s = Settings(repos=[RepoEntry("/x/a"), RepoEntry("/x/b")])
    s.set_repo_review_profile("/x/a", "Mine")

    assert s.review_profile_for_repo("/x/a") == "Mine"
    assert s.review_profile_for_repo("/x/b") == ""


def test_a_repository_profile_is_marked_as_one():
    profile = Profile.from_dict({"name": "Firmware"}, source="repository")
    assert profile.from_repository()
    assert "from the repository" in profile.display()
