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
    """It sweeps every configured repository; "seconds" alone would mislead."""
    info = agents.get(AGENT_ID).info
    assert "per configured repository" in info.cost_hint
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
    settings.stale_branch_rules = StaleRules(months=99).to_dict()
    report = _run(settings, repo)

    assert report.facts_by_key()["stale_total"].raw == 0
    assert _section(report, "2.1").commands == []


def test_turning_the_merged_rule_off_is_what_it_takes(settings, repo):
    settings.stale_branch_rules = StaleRules(merged_only=False).to_dict()
    block = _section(_run(settings, repo), "2.1").commands[0][1]
    assert "half-finished" in block


# ---- scope is never left to be inferred --------------------------------------------
def test_the_branch_sections_name_the_repository_they_are_about(settings, repo):
    assert "widget" in _section(_run(settings, repo), "2").title


def test_the_submodule_section_names_the_fleet_it_swept(settings, repo):
    assert "configured repositories" in _section(_run(settings, repo), "3").title


# ---- submodules across repositories --------------------------------------------------
@pytest.fixture
def two_repos(tmp_path, repo):
    """Two repositories vendoring one dependency at different versions."""
    dep = tmp_path / "dep"
    dep.mkdir()
    git(dep, "init", "-b", "main")
    git(dep, "config", "user.email", "t@example.com")
    git(dep, "config", "user.name", "T")
    for tag in ("v1.0.0", "v2.0.0"):
        (dep / "lib.txt").write_text(tag, encoding="utf-8")
        git(dep, "add", "-A")
        git(dep, "commit", "-m", tag, when=NOW - timedelta(days=50))
        git(dep, "tag", tag)

    other = tmp_path / "gadget"
    other.mkdir()
    git(other, "init", "-b", "main")
    git(other, "config", "user.email", "t@example.com")
    git(other, "config", "user.name", "T")
    (other / "b.txt").write_text("one", encoding="utf-8")
    git(other, "add", "-A")
    git(other, "commit", "-m", "first", when=NOW - timedelta(days=50))

    allow = ["-c", "protocol.file.allow=always"]
    for parent, where in ((repo, "vendor/dep"), (other, "third_party/dep")):
        git(parent, *allow, "submodule", "add", str(dep), where)
        git(parent, "commit", "-m", "add dep")
    # Move `repo` back a version, so the two disagree.
    git(repo / "vendor" / "dep", "checkout", "v1.0.0")
    git(repo, "add", "vendor/dep")
    git(repo, "commit", "-m", "pin v1")
    return other


def test_one_dependency_at_two_paths_is_one_row(settings, repo, two_repos):
    """By path they are two things; by remote they are one, and that is the
    only reading that makes "how many repositories use this" a real number."""
    settings.repos = [RepoEntry(str(repo)), RepoEntry(str(two_repos))]
    rows = _section(_run(settings, repo), "3.1").tables[0].rows

    assert len(rows) == 1
    assert rows[0][1] == "2", "used by two repositories"


def test_a_disagreement_is_reported(settings, repo, two_repos):
    settings.repos = [RepoEntry(str(repo)), RepoEntry(str(two_repos))]
    report = _run(settings, repo)

    assert report.facts_by_key()["submodule_disagreements"].raw == 1
    versions = {row[2] for row in _section(report, "3.2").tables[0].rows}
    assert versions == {"v1.0.0", "v2.0.0"}


def test_agreement_says_so_rather_than_showing_an_empty_table(settings, two_repos):
    settings.repos = [RepoEntry(str(two_repos))]
    section = _section(_run(settings, two_repos), "3.2")

    assert section.tables == []
    assert any(f.value == "yes" for f in section.facts)


def test_the_repository_you_are_looking_at_is_always_swept(settings, repo, two_repos):
    """Selecting one and being told about every repository except it would be
    a surprising way to read a report about it."""
    settings.repos = [RepoEntry(str(two_repos))]
    report = _run(settings, repo)

    assert report.facts_by_key()["repos_scanned"].raw == 2


def test_a_working_tree_off_its_pin_is_drift(settings, repo, two_repos):
    settings.repos = [RepoEntry(str(repo))]
    git(repo / "vendor" / "dep", "checkout", "v2.0.0")

    report = _run(settings, repo)

    assert report.facts_by_key()["submodule_drift"].raw == 1
    assert "not what you would clone" in _section(report, "3.3").tables[0].title


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


def test_progress_is_reported_per_repository(settings, repo):
    said = []
    _run(settings, repo, progress=said.append)
    assert any("Submodules:" in line for line in said)


def test_cancelling_stops_the_sweep(settings, repo):
    from git_assistant.agents.base import CancelledError

    settings.repos = [RepoEntry(str(repo))] * 5
    seen = []

    def cancel_after_two():
        seen.append(True)
        return len(seen) > 3

    with pytest.raises(CancelledError):
        _run(settings, repo, is_cancelled=cancel_after_two)


# ---- the rules, stored and read back --------------------------------------------------
def test_the_rules_survive_the_settings_file(settings):
    from git_assistant.agents.branches import StaleRules

    settings.set_stale_rules(StaleRules(months=3, protect=["keep/*"], merged_only=False))

    back = Settings.from_dict(settings.to_dict()).stale_rules()

    assert (back.months, back.protect, back.merged_only) == (3, ["keep/*"], False)


def test_an_unconfigured_install_gets_working_defaults():
    rules = Settings().stale_rules()
    assert rules.months == 6 and rules.merged_only and "main" in rules.protect


def test_a_hand_edited_file_falls_back_rather_than_raising():
    settings = Settings.from_dict({"stale_branch_rules": {"months": "half a year"}})
    assert settings.stale_rules().months == 6


def test_nonsense_in_place_of_the_rules_is_ignored():
    assert Settings.from_dict({"stale_branch_rules": 7}).stale_rules().months == 6


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


def test_more_repositories_in_the_list_is_not_progress():
    """It is a fact about what you configured, not about tidiness."""
    from git_assistant.agents import compare

    assert compare.metric_for(AGENT_ID, "repos_scanned").polarity is compare.Polarity.NEUTRAL


def test_nothing_in_the_repository_is_changed(settings, repo):
    before = git(repo, "status", "--porcelain") + git(repo, "branch", "--list")
    _run(settings, repo)
    assert git(repo, "status", "--porcelain") + git(repo, "branch", "--list") == before
