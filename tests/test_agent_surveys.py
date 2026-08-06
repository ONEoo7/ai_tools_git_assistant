"""The two surveys behind the consistency audit, against real repositories.

Real ones, built in tmp_path, because the whole feature is an argument about
what git actually reports -- whether a squash-merged branch counts as merged,
what `upstream:track` says when a remote branch is deleted, what `ls-tree`
returns for a submodule nobody initialised. A mocked git would only confirm what
this file already believes.
"""

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from git_assistant.agents import branches as branches_mod
from git_assistant.agents import submodules as sub
from git_assistant.agents.branches import BranchInfo, StaleRules

NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


def git(repo, *args, when=None):
    env = None
    if when is not None:
        stamp = when.isoformat()
        import os

        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        }
    done = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env,
    )
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout


@pytest.fixture
def repo(tmp_path):
    """A repository on `main` with one commit."""
    where = tmp_path / "parent"
    where.mkdir()
    git(where, "init", "-b", "main")
    git(where, "config", "user.email", "t@example.com")
    git(where, "config", "user.name", "T")
    (where / "a.txt").write_text("one", encoding="utf-8")
    git(where, "add", "-A")
    git(where, "commit", "-m", "first", when=NOW - timedelta(days=400))
    return where


def _commit_on(repo, branch, *, days_ago, text="x"):
    git(repo, "checkout", "-b", branch)
    (repo / f"{branch.replace('/', '_')}.txt").write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", branch, when=NOW - timedelta(days=days_ago))
    git(repo, "checkout", "main")


def _info(name, *, days, merged=False, ahead=0, head=False, gone=False):
    return BranchInfo(
        name=name,
        last_commit=NOW - timedelta(days=days),
        merged=merged,
        ahead=ahead,
        is_head=head,
        upstream_gone=gone,
    )


# ---- the distinction the whole feature turns on -----------------------------------
def test_a_merged_branch_and_an_unmerged_one_are_told_apart(repo):
    _commit_on(repo, "merged-long-ago", days_ago=300)
    git(repo, "merge", "--no-ff", "-m", "merge", "merged-long-ago")
    _commit_on(repo, "abandoned", days_ago=300)

    found = {b.name: b for b in branches_mod.survey(str(repo)).branches}

    assert found["merged-long-ago"].merged
    assert not found["abandoned"].merged


def test_only_the_merged_one_is_ever_proposed(repo):
    _commit_on(repo, "merged-long-ago", days_ago=300)
    git(repo, "merge", "--no-ff", "-m", "merge", "merged-long-ago")
    _commit_on(repo, "abandoned", days_ago=300)
    survey = branches_mod.survey(str(repo))

    proposed = [b.name for b in survey.proposed(StaleRules(), NOW)]

    assert proposed == ["merged-long-ago"]
    assert "abandoned" in [b.name for b in survey.kept(StaleRules(), NOW)]


def test_deleting_work_takes_turning_a_rule_off(repo):
    _commit_on(repo, "abandoned", days_ago=300)
    survey = branches_mod.survey(str(repo))

    assert survey.proposed(StaleRules(), NOW) == []
    assert [b.name for b in survey.proposed(StaleRules(merged_only=False), NOW)] == [
        "abandoned"
    ]


# ---- what is spared ------------------------------------------------------------
def test_the_default_branch_is_protected_even_if_the_list_forgets_it():
    """A rule file that permits deleting main is a rule file with a mistake."""
    rules = StaleRules(protect=[])
    assert rules.is_protected("main", default_branch="main")
    assert not rules.proposes(_info("main", days=400, merged=True), NOW, "main")


def test_a_glob_is_honoured():
    rules = StaleRules()
    assert rules.is_protected("release/2.1")
    assert not rules.is_protected("feature/2.1")


def test_the_branch_you_are_on_is_never_proposed():
    rules = StaleRules()
    assert not rules.proposes(_info("current", days=400, merged=True, head=True), NOW)


def test_unpushed_work_is_kept():
    """`ahead` means commits the upstream has not got; deleting loses them."""
    branch = _info("half-done", days=400, merged=True, ahead=3)
    assert not StaleRules().proposes(branch, NOW)
    assert StaleRules(keep_unpushed=False).proposes(branch, NOW)


def test_a_branch_with_no_date_is_never_stale():
    """Nothing to measure; a guess here deletes something."""
    assert not StaleRules().is_stale(BranchInfo("odd", last_commit=None), NOW)


def test_the_age_threshold_is_what_decides():
    young, old = _info("a", days=100, merged=True), _info("b", days=300, merged=True)
    rules = StaleRules(months=6)

    assert not rules.proposes(young, NOW) and rules.proposes(old, NOW)
    assert StaleRules(months=2).proposes(young, NOW)


# ---- nothing is claimed that was not asked ------------------------------------------
def test_with_no_trunk_to_compare_against_nothing_is_called_merged(tmp_path):
    """Erring the other way proposes deletions on an unasked question."""
    assert branches_mod._merged_into(str(tmp_path), "") == set()


def test_a_repository_git_refuses_is_a_problem_not_an_empty_list(tmp_path):
    survey = branches_mod.survey(str(tmp_path / "does-not-exist"))
    assert survey.problem
    assert survey.branches == []


# ---- the commands ---------------------------------------------------------------------
def test_the_block_uses_the_safe_flag():
    """`-d` is git's own refusal to lose work; `-D` removes it."""
    block = branches_mod.delete_commands([_info("gone", days=400, merged=True)])
    assert block == "git branch -d gone"
    assert "-D" not in block


# ---- submodule identity -----------------------------------------------------------------
@pytest.mark.parametrize(
    "written",
    [
        "git@github.com:ONEoo7/thing.git",
        "https://github.com/ONEoo7/thing",
        "https://github.com/ONEoo7/thing.git/",
        "ssh://git@github.com/ONEoo7/Thing",
    ],
)
def test_one_remote_however_it_was_written(written):
    """Three keys for one dependency answers "how many use this" wrongly, in
    the direction that hides the problem."""
    assert sub.normalize_url(written) == "github.com/oneoo7/thing"


def test_two_different_remotes_stay_different():
    assert sub.normalize_url("https://host/a/x") != sub.normalize_url("https://host/b/x")


def test_an_empty_url_is_not_a_key():
    assert sub.normalize_url("") == ""


# ---- reading .gitmodules ------------------------------------------------------------------
def _gitmodules(repo, text):
    (repo / ".gitmodules").write_text(text, encoding="utf-8")


def test_path_and_url_are_paired_per_section(repo):
    _gitmodules(
        repo,
        '[submodule "one"]\n\tpath = vendor/one\n\turl = https://host/a/one.git\n'
        '[submodule "two"]\n\turl = https://host/a/two.git\n\tpath = vendor/two\n',
    )
    found = {d.path: d.url for d in sub.declared_in(str(repo))}

    assert found == {
        "vendor/one": "https://host/a/one.git",
        "vendor/two": "https://host/a/two.git",
    }


def test_url_before_path_still_pairs(repo):
    """The order is not fixed, and a swept regex would mismatch them."""
    _gitmodules(repo, '[submodule "x"]\n\turl = https://host/a/x\n\tpath = libs/x\n')
    assert sub.declared_in(str(repo))[0].url == "https://host/a/x"


def test_an_entry_with_no_path_is_dropped(repo):
    _gitmodules(repo, '[submodule "broken"]\n\turl = https://host/a/x\n')
    assert sub.declared_in(str(repo)) == []


def test_a_repository_with_no_gitmodules_declares_nothing(repo):
    assert sub.declared_in(str(repo)) == []


# ---- what a parent pins ---------------------------------------------------------------------
@pytest.fixture
def with_submodule(tmp_path, repo):
    """A real submodule, tagged v1.0.0, added to `repo`."""
    child = tmp_path / "dep"
    child.mkdir()
    git(child, "init", "-b", "main")
    git(child, "config", "user.email", "t@example.com")
    git(child, "config", "user.name", "T")
    (child / "lib.txt").write_text("one", encoding="utf-8")
    git(child, "add", "-A")
    git(child, "commit", "-m", "one", when=NOW - timedelta(days=100))
    git(child, "tag", "v1.0.0")

    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "vendor/dep")
    git(repo, "commit", "-m", "add dep")
    return child


def test_the_version_is_the_pinned_commit_described_by_tags(repo, with_submodule):
    use = sub.uses_in(str(repo))[0]

    assert use.version == "v1.0.0"
    assert use.pinned and not use.drifted


def test_the_key_is_the_url_not_the_path(repo, with_submodule):
    use = sub.uses_in(str(repo))[0]
    assert use.path == "vendor/dep"
    assert use.key == sub.normalize_url(use.url)


def test_a_working_tree_off_its_pin_is_drift_not_a_version(repo, with_submodule):
    """Otherwise one repository reports two versions on two laptops."""
    where = repo / "vendor" / "dep"
    (where / "lib.txt").write_text("two", encoding="utf-8")
    git(where, "add", "-A")
    git(where, "commit", "-m", "moved on", when=NOW)

    use = sub.uses_in(str(repo))[0]

    assert use.drifted
    assert use.version == "v1.0.0", "still the version the parent pins"


def test_a_submodule_nobody_initialised_still_counts(repo, with_submodule):
    """The parent records a commit whether or not anything was cloned."""
    import shutil

    shutil.rmtree(repo / "vendor" / "dep")
    (repo / "vendor" / "dep").mkdir(parents=True)

    use = sub.uses_in(str(repo))[0]

    assert use.version == sub.NOT_CHECKED_OUT
    assert use.pinned, "the pin is in the parent, and is still readable"


def test_an_untagged_pin_is_unknown_rather_than_a_sha(repo, with_submodule):
    """A bare sha in a column headed "Version" looks like an answer."""
    assert sub.describe(str(with_submodule), "0" * 40) == sub.UNKNOWN


# ---- the fleet -------------------------------------------------------------------------------
def _use(repo, key, version):
    return sub.Use(repo=repo, path="v/x", url=key, key=key, version=version)


def test_a_submodule_at_two_versions_is_the_finding():
    fleet = sub.Fleet(
        uses=[
            _use("/a", "host/x", "v1.4.0"),
            _use("/b", "host/x", "v1.4.0"),
            _use("/c", "host/x", "v1.2.1"),
            _use("/d", "host/y", "v3.0.0"),
        ]
    )

    assert list(fleet.disagreements()) == ["host/x"]
    assert fleet.versions_of("host/x") == ["v1.4.0", "v1.2.1"]


def test_agreement_is_not_a_finding():
    fleet = sub.Fleet(uses=[_use("/a", "host/x", "v1"), _use("/b", "host/x", "v1")])
    assert fleet.disagreements() == {}


def test_how_many_repositories_use_each_one():
    fleet = sub.Fleet(uses=[_use("/a", "host/x", "v1"), _use("/b", "host/x", "v1")])
    assert len(fleet.by_key()["host/x"]) == 2


def test_the_sweep_can_be_cancelled_between_repositories(repo):
    """Between, not after: Cancel has to mean something on the tenth of thirty."""
    seen = []

    def stop_after_one():
        if seen:
            raise KeyboardInterrupt("Cancelled.")

    with pytest.raises(KeyboardInterrupt):
        sub.across(
            [str(repo), str(repo), str(repo)],
            on_repo=seen.append,
            check_cancel=stop_after_one,
        )
    assert len(seen) == 1


def test_every_repository_looked_at_is_counted(repo):
    """The denominator of "3 of 14 repositories use this"."""
    fleet = sub.across([str(repo), str(repo)])
    assert len(fleet.scanned) == 2
