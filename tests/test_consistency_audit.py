"""The consistency audit: what it reports, and what it refuses to propose."""

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from git_assistant import agents
from git_assistant.agents import history as history_mod
from git_assistant.agents.base import AgentContext
from git_assistant.agents.branches import StaleRules
from git_assistant.agents.consistency_audit import AGENT_ID, ConsistencyAuditAgent
from git_assistant.config import RepoEntry, Settings

NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


def git(repo, *args, when=None):
    import os

    env = None
    if when is not None:
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": when.isoformat(),
            "GIT_COMMITTER_DATE": when.isoformat(),
        }
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, env=env
    )
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout


@pytest.fixture
def repo(tmp_path):
    """`main`, one old merged branch, one old unmerged branch."""
    where = tmp_path / "widget"
    where.mkdir()
    git(where, "init", "-b", "main")
    git(where, "config", "user.email", "t@example.com")
    git(where, "config", "user.name", "T")
    (where / "a.txt").write_text("one", encoding="utf-8")
    git(where, "add", "-A")
    git(where, "commit", "-m", "first", when=NOW - timedelta(days=500))

    for name, merge in (("tidy-up", True), ("half-finished", False)):
        git(where, "checkout", "-b", name)
        (where / f"{name}.txt").write_text("x", encoding="utf-8")
        git(where, "add", "-A")
        git(where, "commit", "-m", name, when=NOW - timedelta(days=400))
        git(where, "checkout", "main")
        if merge:
            git(where, "merge", "--no-ff", "-m", f"merge {name}", name)
    return where


@pytest.fixture
def settings(repo):
    s = Settings()
    s.save = lambda: None
    s.repos = [RepoEntry(str(repo))]
    s.active_repo = str(repo)
    return s


def _run(settings, repo, **kw):
    return ConsistencyAuditAgent().collect(
        AgentContext(repo=str(repo), settings=settings, **kw)
    )


def _section(report, number):
    return report.find(number)


# ---- it is offered ---------------------------------------------------------------
def test_the_agent_is_in_the_list():
    assert AGENT_ID in [info.id for info in agents.infos()]


def test_its_cost_is_described_honestly():
    """A repository with submodules costs more than one without; say so."""
    info = agents.get(AGENT_ID).info
    assert "per submodule" in info.cost_hint
    assert "changes nothing" in info.cost_hint


# ---- branches ---------------------------------------------------------------------
def test_a_merged_stale_branch_is_proposed_with_a_command(settings, repo):
    report = _run(settings, repo)
    section = _section(report, "2.1")

    assert "tidy-up" in section.tables[0].rows[0][0]
    assert section.commands and "git branch -d tidy-up" in section.commands[0][1]


def test_an_unmerged_stale_branch_gets_no_command_and_says_why(settings, repo):
    section = _section(_run(settings, repo), "2.2")

    assert "half-finished" in str(section.tables[0].rows)
    assert section.commands == []
    assert "only copy is the branch itself" in section.tables[0].note


def test_the_delete_block_holds_nothing_from_the_unmerged_table(settings, repo):
    block = _section(_run(settings, repo), "2.1").commands[0][1]
    assert "half-finished" not in block


def test_the_default_branch_is_in_neither(settings, repo):
    report = _run(settings, repo)
    for number in ("2.1", "2.2"):
        assert "main" not in str(_section(report, number).tables)


def test_the_rules_that_spared_something_are_shown(settings, repo):
    """So the rules can be seen working, rather than guessed at."""
    section = _section(_run(settings, repo), "2.3")
    assert "main" in str(section.tables[0].rows)
    assert "release/*" in section.facts[1].value


def test_the_threshold_decides_what_is_stale(settings, repo):
    settings.stale_branch_rules = StaleRules(months=99)
    report = _run(settings, repo)

    assert report.facts_by_key()["stale_total"].raw == 0
    assert _section(report, "2.1").commands == []


def test_turning_the_merged_rule_off_is_what_it_takes(settings, repo):
    settings.stale_branch_rules = StaleRules(merged_only=False)
    block = _section(_run(settings, repo), "2.1").commands[0][1]
    assert "half-finished" in block


# ---- scope is never left to be inferred --------------------------------------------
def test_the_branch_sections_name_the_repository_they_are_about(settings, repo):
    assert "widget" in _section(_run(settings, repo), "2").title


def test_the_submodule_section_names_the_repository_it_read(settings, repo):
    assert "widget" in _section(_run(settings, repo), "3").title


# ---- submodules, of the selected repository and nothing else -------------------------
@pytest.fixture
def dep(tmp_path):
    """A dependency with two tagged versions, to be vendored."""
    where = tmp_path / "dep"
    where.mkdir()
    git(where, "init", "-b", "main")
    git(where, "config", "user.email", "t@example.com")
    git(where, "config", "user.name", "T")
    for tag in ("v1.0.0", "v2.0.0"):
        (where / "lib.txt").write_text(tag, encoding="utf-8")
        git(where, "add", "-A")
        git(where, "commit", "-m", tag, when=NOW - timedelta(days=50))
        git(where, "tag", tag)
    return where


def _vendor(parent, dep, where):
    git(parent, "-c", "protocol.file.allow=always", "submodule", "add", str(dep), where)
    git(parent, "commit", "-m", f"add {where}")


@pytest.fixture
def other_repo(tmp_path, dep):
    """A second configured repository, vendoring the same dependency."""
    where = tmp_path / "gadget"
    where.mkdir()
    git(where, "init", "-b", "main")
    git(where, "config", "user.email", "t@example.com")
    git(where, "config", "user.name", "T")
    (where / "b.txt").write_text("one", encoding="utf-8")
    git(where, "add", "-A")
    git(where, "commit", "-m", "first", when=NOW - timedelta(days=50))
    _vendor(where, dep, "third_party/dep")
    return where


def test_only_the_selected_repository_is_read(settings, repo, dep, other_repo):
    """The Repositories list is not swept. A report headed with one repository
    that quietly measured another is a report whose numbers cannot be read."""
    settings.repos = [RepoEntry(str(repo)), RepoEntry(str(other_repo))]
    _vendor(repo, dep, "vendor/dep")

    report = _run(settings, repo)
    rows = _section(report, "3.1").tables[0].rows

    assert [row[0] for row in rows] == ["vendor/dep"]
    assert "third_party" not in str(rows)
    assert report.facts_by_key()["submodules_used"].raw == 1


def test_selecting_the_other_repository_changes_the_answer(settings, repo, dep, other_repo):
    settings.repos = [RepoEntry(str(repo)), RepoEntry(str(other_repo))]
    _vendor(repo, dep, "vendor/dep")

    rows = _section(_run(settings, other_repo), "3.1").tables[0].rows

    assert [row[0] for row in rows] == ["third_party/dep"]


def test_the_version_is_the_pinned_commit(settings, repo, dep):
    _vendor(repo, dep, "vendor/dep")
    row = _section(_run(settings, repo), "3.1").tables[0].rows[0]

    assert row[2] == "v2.0.0"
    assert "not whatever the working tree" in _section(_run(settings, repo), "3.1").tables[0].note


def test_one_remote_vendored_twice_at_two_versions_is_the_finding(settings, repo, dep):
    """Nothing in `git status` says the two copies are not the same copy."""
    _vendor(repo, dep, "vendor/dep")
    _vendor(repo, dep, "third_party/dep")
    git(repo / "vendor" / "dep", "checkout", "v1.0.0")
    git(repo, "add", "vendor/dep")
    git(repo, "commit", "-m", "pin v1")

    report = _run(settings, repo)

    assert report.facts_by_key()["submodule_disagreements"].raw == 1
    rows = _section(report, "3.2").tables[0].rows
    assert {row[2] for row in rows} == {"v1.0.0", "v2.0.0"}
    assert {row[1] for row in rows} == {"vendor/dep", "third_party/dep"}


def test_the_same_remote_at_one_version_is_not_a_finding(settings, repo, dep):
    _vendor(repo, dep, "vendor/dep")
    _vendor(repo, dep, "third_party/dep")

    assert _run(settings, repo).facts_by_key()["submodule_disagreements"].raw == 0


def test_agreement_says_so_rather_than_showing_an_empty_table(settings, repo, dep):
    _vendor(repo, dep, "vendor/dep")
    section = _section(_run(settings, repo), "3.2")

    assert section.tables == []
    assert any(f.value == "yes" for f in section.facts)


def test_no_submodules_says_so_rather_than_showing_an_empty_table(settings, repo):
    section = _section(_run(settings, repo), "3.1")

    assert section.tables == []
    assert any(f.value == "none" for f in section.facts)


def test_a_working_tree_off_its_pin_is_drift(settings, repo, dep):
    _vendor(repo, dep, "vendor/dep")
    git(repo / "vendor" / "dep", "checkout", "v1.0.0")

    report = _run(settings, repo)

    assert report.facts_by_key()["submodule_drift"].raw == 1
    table = _section(report, "3.3").tables[0]
    assert "not what you would clone" in table.title
    assert table.rows[0][0] == "vendor/dep"


# ---- what could not be read ----------------------------------------------------------
def test_a_repository_git_refuses_becomes_a_warning(settings, tmp_path):
    """"0 stale branches" and "could not read it" must not look the same."""
    missing = tmp_path / "not-a-repo"
    missing.mkdir()
    report = _run(settings, missing)

    assert any("could not be read" in w for w in report.warnings)


# ---- it fits the machinery that already exists -----------------------------------------
def test_the_run_round_trips_through_history(settings, repo, monkeypatch, tmp_path):
    monkeypatch.setattr(history_mod, "user_config_dir", lambda *a, **k: str(tmp_path))
    report = _run(settings, repo)

    back = history_mod.report_from_dict(history_mod.report_to_dict(report))

    assert back.agent_id == AGENT_ID
    assert [s.number for s in back.walk()] == [s.number for s in report.walk()]
    assert back.find("2.1").commands == report.find("2.1").commands


def test_both_halves_are_narrated_as_they_run(settings, repo):
    said = []
    _run(settings, repo, progress=said.append)
    assert any("branches" in line.lower() for line in said)
    assert any("submodules" in line.lower() for line in said)


def test_cancelling_stops_the_run(settings, repo):
    from git_assistant.agents.base import CancelledError

    with pytest.raises(CancelledError):
        _run(settings, repo, is_cancelled=lambda: True)


# ---- the rules, stored and read back --------------------------------------------------
# They are a repository's, not the user's: whether a six-month branch is stale
# is a question about the project. So they round-trip through the settings a
# repository carries -- see git_assistant.repo_config.StaleRules.
def test_the_rules_survive_the_settings_file(repo):
    from git_assistant import repo_config

    repo_config.write_text(
        repo_config.Tier.REPO,
        str(repo),
        '{"audit": {"stale": {"months": 3, "protect": ["keep/*"],'
        ' "merged_only": false}}}',
    )

    back = repo_config.resolve(repo).audit.stale.as_branch_rules()

    assert (back.months, back.protect, back.merged_only) == (3, ["keep/*"], False)


def test_an_unconfigured_install_gets_working_defaults():
    from git_assistant import repo_config

    rules = repo_config.RepoSettings().audit.stale.as_branch_rules()
    assert rules.months == 6 and rules.merged_only and "main" in rules.protect


def test_a_hand_edited_file_falls_back_rather_than_raising(repo):
    from git_assistant import repo_config

    repo_config.write_text(
        repo_config.Tier.REPO, str(repo), '{"audit": {"stale": {"months": "half a year"}}}'
    )
    assert repo_config.resolve(repo).audit.stale.months == 6


def test_nonsense_in_place_of_the_rules_is_ignored(repo):
    from git_assistant import repo_config

    repo_config.write_text(repo_config.Tier.REPO, str(repo), '{"audit": {"stale": 7}}')
    assert repo_config.resolve(repo).audit.stale.months == 6


# ---- narration and comparison know about it -----------------------------------------------
def test_its_sections_have_narration_slots():
    from git_assistant.agents import prompts

    assert set(prompts.OUTLINES[AGENT_ID]) == {
        "consistency_summary",
        "submodule_disagreements",
    }


def test_the_slots_and_the_outlines_are_the_same_set(settings, repo):
    """Both directions. A slot with no outline never narrates and says nothing
    about it; an outline with no slot is prose nobody will ever see."""
    from git_assistant.agents import prompts

    used = {s.slot for s in _run(settings, repo).walk() if s.slot}
    assert used == set(prompts.OUTLINES[AGENT_ID])


def test_two_runs_can_be_compared(settings, repo):
    from git_assistant.agents import compare

    assert "submodule_disagreements" in compare.headline_keys(AGENT_ID)
    assert compare.metric_for(AGENT_ID, "stale_merged").polarity is compare.Polarity.LOWER


def test_vendoring_one_more_dependency_is_not_progress():
    """It is a fact about the project, not about tidiness."""
    from git_assistant.agents import compare

    assert compare.metric_for(AGENT_ID, "submodules_used").polarity is compare.Polarity.NEUTRAL


def test_nothing_in_the_repository_is_changed(settings, repo):
    before = git(repo, "status", "--porcelain") + git(repo, "branch", "--list")
    _run(settings, repo)
    assert git(repo, "status", "--porcelain") + git(repo, "branch", "--list") == before
