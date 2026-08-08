"""Branches and fetching, against real repositories and a real local remote.

Nothing here is mocked. A branch that git refuses to delete is the whole point
of the function that asks it to, and a stub would agree to anything.
"""

import os
import subprocess
import sys

import pytest

from git_assistant import git_ops

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args, env=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
        env={**os.environ, **env} if env else None,
    )


def _init(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    return path


def _commit(repo, text, message):
    (repo / "f.txt").write_text(text, encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", message)


@pytest.fixture
def repo(tmp_path):
    """One repository with one commit."""
    path = _init(tmp_path / "work")
    _commit(path, "one\n", "initial")
    return path


@pytest.fixture
def remote(tmp_path, repo):
    """The same repository, with a local bare remote as origin."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(bare)],
        capture_output=True,
        creationflags=_NO_WINDOW,
        check=True,
    )
    _git(repo, "remote", "add", "origin", str(bare))
    return repo


# ---- creating ---------------------------------------------------------------------
def test_a_branch_is_created_and_checked_out_in_one_go(repo):
    result = git_ops.create_branch(repo, "dev/rem/sg/thing")

    assert result.ok, result.stderr
    assert git_ops.current_branch(repo) == "dev/rem/sg/thing"
    assert git_ops.branch_exists(repo, "dev/rem/sg/thing")


def test_a_branch_can_be_made_without_leaving_the_one_you_are_on(repo):
    before = git_ops.current_branch(repo)

    assert git_ops.create_branch(repo, "later", switch=False).ok

    assert git_ops.current_branch(repo) == before
    assert git_ops.branch_exists(repo, "later")


def test_a_branch_starts_where_it_is_told_to(repo):
    first = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _commit(repo, "two\n", "second")

    git_ops.create_branch(repo, "from-first", start_point=first)

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == first


def test_creating_a_branch_that_exists_fails_rather_than_moving_it(repo):
    """`git branch -C` would move it, and this is offered from a text field."""
    git_ops.create_branch(repo, "taken", switch=False)
    kept = _git(repo, "rev-parse", "taken").stdout.strip()
    _commit(repo, "two\n", "second")

    result = git_ops.create_branch(repo, "taken", switch=False)

    assert not result.ok
    assert _git(repo, "rev-parse", "taken").stdout.strip() == kept


def test_a_branch_with_no_name_is_refused_before_git_is_asked(repo):
    result = git_ops.create_branch(repo, "")
    assert not result.ok and "No branch name" in result.stderr


def test_a_name_git_will_not_have_is_a_failure_not_a_crash(repo):
    result = git_ops.create_branch(repo, "no spaces allowed")
    assert not result.ok
    assert not git_ops.branch_exists(repo, "no spaces allowed")


# ---- listing ----------------------------------------------------------------------
def test_branches_report_what_they_track_and_how_far_they_have_drifted(remote):
    git_ops.push(remote)
    _commit(remote, "two\n", "second")

    current = next(b for b in git_ops.list_branch_info(remote) if b.current)

    assert current.name == git_ops.current_branch(remote)
    assert current.upstream.startswith("origin/")
    assert (current.ahead, current.behind) == (1, 0)
    assert current.subject == "second"
    assert "1 ahead" in current.tracking_label()


def test_a_branch_that_tracks_nothing_says_so(repo):
    git_ops.create_branch(repo, "lonely", switch=False)

    lonely = next(b for b in git_ops.list_branch_info(repo) if b.name == "lonely")

    assert lonely.upstream == ""
    assert lonely.tracking_label() == "no upstream"
    assert lonely.current is False


def test_a_branch_level_with_its_upstream_says_that_rather_than_nothing(remote):
    git_ops.push(remote)
    current = next(b for b in git_ops.list_branch_info(remote) if b.current)
    assert current.tracking_label() == "up to date"


def test_a_commit_subject_cannot_forge_a_column(repo):
    """The fields are split on a unit separator, so a subject holding one..."""
    _commit(repo, "two\n", "feat: a | b \x1f c")

    rows = git_ops.list_branch_info(repo)

    assert len(rows) == 1
    assert rows[0].name == git_ops.current_branch(repo)


def test_listing_the_branches_of_something_that_is_not_a_repository_is_empty(tmp_path):
    assert git_ops.list_branch_info(tmp_path / "nope") == []


# ---- deleting -----------------------------------------------------------------------
def test_a_merged_branch_is_deleted(repo):
    git_ops.create_branch(repo, "spare", switch=False)

    assert git_ops.delete_branch(repo, "spare").ok

    assert not git_ops.branch_exists(repo, "spare")


def _unmerged(repo, name="unmerged"):
    main = git_ops.current_branch(repo)
    git_ops.create_branch(repo, name)
    (repo / "only-here.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "only-here.txt")
    _git(repo, "commit", "-m", "work")
    _git(repo, "switch", main)
    return name


def test_a_branch_whose_commits_exist_nowhere_else_is_refused(repo):
    """That refusal is the last thing between a button and somebody's afternoon."""
    _unmerged(repo)

    result = git_ops.delete_branch(repo, "unmerged")

    assert not result.ok
    assert git_ops.branch_exists(repo, "unmerged")


def test_an_unmerged_branch_goes_only_when_the_caller_says_so(repo):
    _unmerged(repo)

    assert git_ops.delete_branch(repo, "unmerged", force=True).ok

    assert not git_ops.branch_exists(repo, "unmerged")


def test_deleting_a_local_branch_leaves_the_remote_one(remote):
    git_ops.create_branch(remote, "shared")
    git_ops.push_branch(remote, "shared")
    _git(remote, "switch", "-")

    git_ops.delete_branch(remote, "shared", force=True)

    assert not git_ops.branch_exists(remote, "shared")
    assert "shared" in _git(remote, "ls-remote", "--heads", "origin").stdout


def test_a_remote_branch_can_be_deleted_on_its_own(remote):
    git_ops.create_branch(remote, "shared")
    git_ops.push_branch(remote, "shared")

    assert git_ops.delete_remote_branch(remote, "shared").ok

    assert "shared" not in _git(remote, "ls-remote", "--heads", "origin").stdout
    assert git_ops.branch_exists(remote, "shared")  # the local one stays


# ---- pushing one branch --------------------------------------------------------------
def test_pushing_a_branch_publishes_it_and_sets_its_upstream(remote):
    git_ops.create_branch(remote, "dev/rem/sg/thing")

    result = git_ops.push_branch(remote, "dev/rem/sg/thing")

    assert result.ok, result.stderr
    assert git_ops.branch_upstream(remote, "dev/rem/sg/thing") == (
        "origin/dev/rem/sg/thing"
    )


def test_pushing_without_setting_an_upstream_leaves_it_untracked(remote):
    git_ops.create_branch(remote, "untracked")

    assert git_ops.push_branch(remote, "untracked", set_upstream=False).ok

    assert git_ops.branch_upstream(remote, "untracked") == ""


def test_a_branch_that_already_tracks_something_keeps_tracking_it(remote):
    """Re-pointing an upstream is a different act from pushing."""
    git_ops.create_branch(remote, "tracked")
    git_ops.push_branch(remote, "tracked")
    before = git_ops.branch_upstream(remote, "tracked")
    _commit(remote, "two\n", "second")

    assert git_ops.push_branch(remote, "tracked").ok

    assert git_ops.branch_upstream(remote, "tracked") == before


def test_pushing_a_branch_that_does_not_exist_is_a_failure_not_a_crash(remote):
    assert not git_ops.push_branch(remote, "never-existed").ok


# ---- fetching ------------------------------------------------------------------------
def _clone(source, target, *, depth=None):
    """Clone over ``file://``, which is the only way a depth is honoured.

    git says so itself for a plain path: "--depth is ignored in local clones;
    use file:// instead". A test that cloned by path would quietly get the
    whole history and prove nothing about shallowness.
    """
    args = ["git", "clone"]
    if depth is not None:
        args.append(f"--depth={depth}")
    args += [source.as_uri() if depth is not None else str(source), str(target)]
    subprocess.run(args, capture_output=True, creationflags=_NO_WINDOW, check=True)
    _git(target, "config", "user.email", "other@example.com")
    _git(target, "config", "user.name", "Other")
    return target


def test_fetching_updates_the_refs_without_touching_the_working_tree(remote, tmp_path):
    git_ops.push(remote)
    other = _clone(tmp_path / "remote.git", tmp_path / "other")
    (other / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    _git(other, "add", "theirs.txt")
    _git(other, "commit", "-m", "from elsewhere")
    _git(other, "push")

    result = git_ops.fetch(remote)

    assert result.ok, result.stderr
    assert not (remote / "theirs.txt").exists()  # nothing was checked out
    current = next(b for b in git_ops.list_branch_info(remote) if b.current)
    assert current.behind == 1


def test_fetching_prunes_what_the_remote_no_longer_has(remote):
    git_ops.create_branch(remote, "temporary")
    git_ops.push_branch(remote, "temporary")
    _git(remote, "switch", "-")
    git_ops.delete_remote_branch(remote, "temporary")

    git_ops.fetch(remote, prune=True)

    refs = _git(remote, "for-each-ref", "refs/remotes/origin").stdout
    assert "temporary" not in refs


def test_fetching_leaves_alone_what_the_next_merge_would_mean(remote):
    """FETCH_HEAD belongs to whatever the user last did by hand."""
    git_ops.push(remote)
    fetch_head = remote / ".git" / "FETCH_HEAD"
    fetch_head.write_text("mine\n", encoding="utf-8")

    git_ops.fetch(remote)

    assert fetch_head.read_text(encoding="utf-8") == "mine\n"


def test_fetching_a_repository_with_no_remote_is_not_an_error(repo):
    """git's own answer, checked rather than assumed: it exits 0 and does nothing.

    Pinned because the obvious guess is the opposite, and a caller that treated
    this as a failure would report one to someone who has no remote and does
    not need telling twice.
    """
    result = git_ops.fetch(repo)

    assert result.ok
    assert result.returncode == 0


# ---- how much history --------------------------------------------------------------
@pytest.fixture
def deep(tmp_path):
    """An origin with five commits, to be fetched shallowly from."""
    origin = _init(tmp_path / "origin")
    for i in range(5):
        _commit(origin, f"{i}\n", f"commit {i}")
    return origin


def test_a_shallow_clone_is_recognised_as_one(deep, tmp_path):
    shallow = _clone(deep, tmp_path / "shallow", depth=1)

    assert git_ops.is_shallow(shallow)
    assert git_ops.is_shallow(deep) is False


def test_a_shallow_fetch_takes_only_what_it_was_told_to(deep, tmp_path):
    """Cloning a large history to read one branch is a wait nobody needs."""
    shallow = _clone(deep, tmp_path / "shallow", depth=1)
    assert int(_git(shallow, "rev-list", "--count", "HEAD").stdout.strip()) == 1

    assert git_ops.fetch(shallow, depth=3).ok

    assert int(_git(shallow, "rev-list", "--count", "HEAD").stdout.strip()) == 3
    assert git_ops.is_shallow(shallow)


def test_a_full_fetch_asks_for_no_depth_and_stays_full(deep, tmp_path):
    whole = _clone(deep, tmp_path / "whole")

    assert git_ops.fetch(whole).ok

    assert not git_ops.is_shallow(whole)
    assert int(_git(whole, "rev-list", "--count", "HEAD").stdout.strip()) == 5


def test_a_shallow_repository_can_be_filled_in(deep, tmp_path):
    shallow = _clone(deep, tmp_path / "shallow", depth=1)

    assert git_ops.unshallow(shallow).ok

    assert not git_ops.is_shallow(shallow)
    assert int(_git(shallow, "rev-list", "--count", "HEAD").stdout.strip()) == 5


def test_filling_in_a_repository_that_is_already_whole_says_so_plainly(deep, tmp_path):
    """git's own answer is "does not make sense", which is not an answer."""
    whole = _clone(deep, tmp_path / "whole")

    result = git_ops.unshallow(whole)

    assert not result.ok
    assert "whole history" in result.stderr


def test_a_depth_of_nothing_is_still_a_fetch_of_something(deep, tmp_path):
    """`--depth=0` is an error from git; the caller meant "one"."""
    shallow = _clone(deep, tmp_path / "shallow", depth=1)

    assert git_ops.fetch(shallow, depth=0).ok
