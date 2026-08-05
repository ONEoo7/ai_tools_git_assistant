"""The review profile a repository carries, for whoever clones it."""

import json

import pytest

from git_assistant.review import shared
from git_assistant.review.profiles import LanguageRules, Profile, Selection, resolve
from git_assistant.review.rules import Rule, RuleStore, RuleTable

HOUSE = RuleTable("House rules", [Rule("H-1", "one"), Rule("H-2", "two")])


@pytest.fixture
def store():
    return RuleStore([HOUSE])


def _profile():
    return Profile(
        name="Firmware review",
        languages=[
            LanguageRules("cpp", version="c++17", selections=[Selection("builtin:cpp", ["CPP-07"])]),
            LanguageRules("python", selections=[Selection("table:House rules")]),
        ],
        overrides={".h": "cpp"},
    )


# ---- writing ---------------------------------------------------------------------
def test_it_is_written_where_a_clone_will_find_it(tmp_path, store):
    path = shared.write(str(tmp_path), _profile(), store)

    assert path == tmp_path / ".git-assistant" / "code-review-profile.json"
    assert shared.exists(str(tmp_path))


def test_a_custom_table_is_written_out_in_full(tmp_path, store):
    """A colleague who clones has to get the rules, not a name to look up."""
    shared.write(str(tmp_path), _profile(), store)

    data = json.loads(shared.profile_path(str(tmp_path)).read_text(encoding="utf-8"))

    table = [t for t in data["tables"] if t["ref"] == "table:House rules"][0]
    assert [r["rule_id"] for r in table["rules"]] == ["H-1", "H-2"]
    assert table["fingerprint"] == HOUSE.fingerprint()


def test_a_shipped_table_is_named_not_copied(tmp_path, store):
    shared.write(str(tmp_path), _profile(), store)
    data = json.loads(shared.profile_path(str(tmp_path)).read_text(encoding="utf-8"))

    assert [t["ref"] for t in data["tables"]] == ["table:House rules"]
    refs = [s["ref"] for e in data["languages"] for s in e["selections"]]
    assert "builtin:cpp" in refs
    assert data["builtin_schema"]["cpp"] >= 1


def test_what_was_turned_off_travels_with_it(tmp_path, store):
    shared.write(str(tmp_path), _profile(), store)
    data = json.loads(shared.profile_path(str(tmp_path)).read_text(encoding="utf-8"))

    cpp = [e for e in data["languages"] if e["language"] == "cpp"][0]
    assert cpp["selections"][0]["exclude"] == ["CPP-07"]
    assert cpp["version"] == "c++17"


def test_it_is_written_the_same_way_every_time(tmp_path, store):
    """A Windows collaborator must not get a whole-file diff for one line."""
    shared.write(str(tmp_path), _profile(), store)
    raw = shared.profile_path(str(tmp_path)).read_bytes()

    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")


def test_writing_leaves_no_temporary_file_behind(tmp_path, store):
    shared.write(str(tmp_path), _profile(), store)
    names = [p.name for p in (tmp_path / ".git-assistant").iterdir()]
    assert names == ["code-review-profile.json"]


# ---- reading it back ---------------------------------------------------------------
def test_a_clone_reads_the_profile_the_repository_shipped(tmp_path, store):
    shared.write(str(tmp_path), _profile(), store)

    profile, tables = shared.read(str(tmp_path))

    assert profile.name == "Firmware review"
    assert profile.from_repository()
    assert profile.overrides == {".h": "cpp"}
    assert "table:House rules" in tables


def test_a_clone_is_reviewed_against_what_the_repository_shipped(tmp_path, store):
    """Not against a local table that happens to share the name."""
    shared.write(str(tmp_path), _profile(), store)
    profile, tables = shared.read(str(tmp_path))
    mine = RuleStore([RuleTable("House rules", [Rule("X-9", "something of my own")])])

    table = resolve(profile, mine, "python", "", inlined=tables)

    assert table.find("H-1") is not None
    assert table.find("X-9") is None


def test_a_repository_with_no_profile_answers_nothing(tmp_path):
    assert shared.read(str(tmp_path)) == (None, {})
    assert not shared.exists(str(tmp_path))


def test_a_broken_file_reads_as_absent_rather_than_raising(tmp_path):
    path = shared.profile_path(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text("{half", encoding="utf-8")

    assert shared.read(str(tmp_path)) == (None, {})


def test_a_file_that_is_not_a_profile_reads_as_absent(tmp_path):
    path = shared.profile_path(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text('{"something": "else"}', encoding="utf-8")

    assert shared.read(str(tmp_path))[0] is None


def test_what_arrives_from_a_clone_is_capped(tmp_path):
    """The one input that comes from somebody else."""
    path = shared.profile_path(str(tmp_path))
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "name": "Huge",
                "languages": [],
                "tables": [
                    {
                        "ref": "table:Huge",
                        "name": "Huge",
                        "rules": [
                            {"rule_id": f"R-{i}", "details": "x" * 9000}
                            for i in range(900)
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    _profile_read, tables = shared.read(str(tmp_path))

    table = tables["table:Huge"]
    assert len(table.rules) == shared.MAX_RULES
    assert len(table.rules[0].details) == shared.MAX_DETAILS


# ---- the user's own library is never touched ----------------------------------------
def test_reading_a_repository_profile_adds_nothing_to_the_local_library(tmp_path, store):
    shared.write(str(tmp_path), _profile(), store)
    mine = RuleStore()

    shared.read(str(tmp_path))

    assert mine.names() == []


def test_a_repository_table_can_be_copied_in_on_request(tmp_path, store, monkeypatch):
    monkeypatch.setattr(
        "git_assistant.review.rules.user_config_dir", lambda *a, **k: str(tmp_path / "cfg")
    )
    shared.write(str(tmp_path), _profile(), store)
    _p, tables = shared.read(str(tmp_path))
    mine = RuleStore()

    added = shared.copy_into(mine, tables)

    assert added == ["House rules"]
    assert mine.find("House rules").find("H-1") is not None


def test_copying_never_overwrites_a_table_of_the_same_name(tmp_path, store, monkeypatch):
    monkeypatch.setattr(
        "git_assistant.review.rules.user_config_dir", lambda *a, **k: str(tmp_path / "cfg")
    )
    shared.write(str(tmp_path), _profile(), store)
    _p, tables = shared.read(str(tmp_path))
    mine = RuleStore([RuleTable("House rules", [Rule("MINE-1", "my own")])])

    shared.copy_into(mine, tables)

    assert mine.find("House rules").find("MINE-1") is not None
    assert mine.find("House rules (2)").find("H-1") is not None


# ---- noticing that it changed --------------------------------------------------------
def test_the_file_has_an_identity_so_a_change_can_be_noticed_once(tmp_path, store):
    shared.write(str(tmp_path), _profile(), store)
    before = shared.fingerprint(str(tmp_path))

    other = _profile()
    other.name = "Something else"
    shared.write(str(tmp_path), other, store)

    assert before and shared.fingerprint(str(tmp_path)) != before


def test_a_repository_with_no_profile_has_no_fingerprint(tmp_path):
    assert shared.fingerprint(str(tmp_path)) == ""
