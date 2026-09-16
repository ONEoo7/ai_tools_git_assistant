"""Reading a history for a graph of it, and what a commit is part of -- from real repositories."""

import subprocess
import sys

import pytest

from git_assistant import git_ops
from git_assistant.git_ops import REF_BRANCH, REF_HEAD, REF_REMOTE, REF_TAG, RefLabel

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

AUTHORED = 1767315845  # 2026-01-02T01:04:05Z


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


def _commit(repo, name, message):
    (repo / name).write_text(message, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture(autouse=True)
def git_home(tmp_path, monkeypatch):
    config = tmp_path / "global.gitconfig"
    config.write_text(
        "[user]\n\tname = Bogdan Taloi\n\temail = bogdan@example.com\n"
        "[init]\n\tdefaultBranch = master\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_DATE", f"{AUTHORED} +0000")


@pytest.fixture
def history(tmp_path):
    """master: first, second (tagged), a merged feature, pushed to origin; one unmerged branch."""
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(remote)],
        capture_output=True,
        creationflags=_NO_WINDOW,
        check=True,
    )
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    first = _commit(repo, "a.txt", "first")
    second = _commit(repo, "a.txt", "second\n\nwith a body\nof two lines")
    _git(repo, "tag", "-a", "v1.0", "-m", "release", second)
    _git(repo, "tag", "light", second)
    _git(repo, "checkout", "-q", "-b", "feature")
    feature = _commit(repo, "f.txt", "feature work")
    _git(repo, "checkout", "-q", "master")
    third = _commit(repo, "a.txt", "third")
    _git(repo, "merge", "-q", "--no-ff", "feature", "-m", "merge feature")
    merge = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "origin", "master")
    _git(repo, "remote", "set-head", "origin", "master")
    _git(repo, "checkout", "-q", "-b", "unmerged", first)
    unmerged = _commit(repo, "u.txt", "unmerged work")
    _git(repo, "checkout", "-q", "master")
    return {
        "repo": repo,
        "first": first,
        "second": second,
        "feature": feature,
        "third": third,
        "merge": merge,
        "unmerged": unmerged,
    }


def test_every_commit_comes_after_its_children_with_what_a_graph_needs(history):
    commits = git_ops.commit_log(history["repo"])
    by_hash = {commit.hash: commit for commit in commits}
    order = [commit.hash for commit in commits]

    assert set(order) == {history[k] for k in ("first", "second", "feature", "third", "merge", "unmerged")}
    for commit in commits:
        for parent in commit.parents:
            assert order.index(parent) > order.index(commit.hash)
    merge = by_hash[history["merge"]]
    assert merge.parents == (history["third"], history["feature"])
    assert by_hash[history["first"]].parents == ()
    second = by_hash[history["second"]]
    assert (second.subject, second.message) == ("second", "second\n\nwith a body\nof two lines")
    assert (second.author, second.email, second.authored) == (
        "Bogdan Taloi",
        "bogdan@example.com",
        AUTHORED,
    )


def test_the_names_on_a_commit_are_labelled_by_what_they_are(history):
    by_hash = {commit.hash: commit for commit in git_ops.commit_log(history["repo"])}

    assert by_hash[history["merge"]].refs == (
        RefLabel("master", REF_BRANCH, current=True),
        RefLabel("origin/master", REF_REMOTE),
    )  # and not origin/HEAD, which only says which branch origin considers its main one
    assert set(by_hash[history["second"]].refs) == {RefLabel("v1.0", REF_TAG), RefLabel("light", REF_TAG)}
    assert by_hash[history["unmerged"]].refs == (RefLabel("unmerged", REF_BRANCH),)
    assert by_hash[history["merge"]].is_head()
    assert not by_hash[history["unmerged"]].is_head()


def test_a_detached_head_is_labelled_as_head(history):
    _git(history["repo"], "checkout", "-q", "--detach", history["third"])

    by_hash = {commit.hash: commit for commit in git_ops.commit_log(history["repo"])}

    assert by_hash[history["third"]].refs == (RefLabel("HEAD", REF_HEAD),)
    assert by_hash[history["third"]].is_head()
    assert RefLabel("master", REF_BRANCH, current=False) in by_hash[history["merge"]].refs


def test_the_checked_out_branch_alone_leaves_the_others_out(history):
    hashes = {commit.hash for commit in git_ops.commit_log(history["repo"], all_branches=False)}

    assert history["unmerged"] not in hashes
    assert history["feature"] in hashes  # merged, so part of this branch's history


def test_a_history_is_read_to_its_limit(history):
    commits = git_ops.commit_log(history["repo"], limit=2)

    assert len(commits) == 2


def test_a_repository_with_no_commit_has_no_history(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")

    assert git_ops.commit_log(empty) == []


def test_a_message_holding_a_separator_is_read_whole(tmp_path):
    repo = tmp_path / "odd"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "a.txt", "subject\n\nbody with a \x1f in it")
    _git(repo, "tag", "t1")

    [commit] = git_ops.commit_log(repo)

    assert commit.message == "subject\n\nbody with a \x1f in it"
    assert RefLabel("t1", REF_TAG) in commit.refs


def test_what_a_commit_is_part_of_and_the_tag_it_derives_from(history):
    repo = history["repo"]

    assert set(git_ops.branches_containing(repo, history["second"])) == {"master", "feature", "origin/master"}
    assert git_ops.branches_containing(repo, history["unmerged"]) == ["unmerged"]
    assert set(git_ops.tags_containing(repo, history["first"])) == {"v1.0", "light"}
    assert git_ops.tags_containing(repo, history["third"]) == []
    assert git_ops.nearest_tag(repo, history["merge"]) in ("v1.0", "light")
    assert git_ops.nearest_tag(repo, history["first"]) == ""
