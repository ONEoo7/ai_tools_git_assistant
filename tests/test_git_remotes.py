"""A repository's remotes, and which one a branch tracks -- against real repositories."""

import subprocess
import sys

import pytest

from git_assistant import git_ops

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


def _bare(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "--bare", str(path)],
        capture_output=True,
        creationflags=_NO_WINDOW,
        check=True,
    )
    return path


def _branches_in(bare):
    listed = subprocess.run(
        ["git", "--git-dir", str(bare), "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )
    return listed.stdout.split()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """One commit on main, and nothing inherited from the machine's git config."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "absent-global"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "absent-system"))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "--initial-branch=main")
    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    (work / "a.txt").write_text("a\n", encoding="utf-8")
    _git(work, "add", "a.txt")
    _git(work, "commit", "-q", "-m", "initial")
    return work


# ---- which there are ---------------------------------------------------------------
def test_remotes_are_listed_by_name_with_where_they_fetch_from(repo, tmp_path):
    """A folder with a space in it included: the name ends at a tab, not a space."""
    folder = _bare(tmp_path / "with space" / "backup.git")
    _git(repo, "remote", "add", "origin", "https://github.com/ONEoo7/thing.git")
    _git(repo, "remote", "add", "Backup", str(folder))

    assert git_ops.list_remotes(repo) == [
        git_ops.Remote("Backup", str(folder)),
        git_ops.Remote("origin", "https://github.com/ONEoo7/thing.git"),
    ]


def test_a_repository_without_remotes_lists_none(repo):
    assert git_ops.list_remotes(repo) == []
    assert git_ops.push_remote(repo) == ""


@pytest.mark.parametrize(
    "name, accepted",
    [
        ("upstream", True),
        ("team/fork", True),
        ("", False),
        ("two words", False),
        ("-x", False),
        ("a..b", False),
        ("colon:name", False),
    ],
)
def test_a_remote_name_is_one_git_accepts(repo, name, accepted):
    assert git_ops.valid_remote_name(repo, name) is accepted


# ---- adding and removing -------------------------------------------------------------
def test_adding_a_remote_fetches_nothing(repo, tmp_path):
    """A network call nobody asked for, and a slow one on a large repository."""
    other = _bare(tmp_path / "other.git")
    _git(repo, "push", "-q", str(other), "main")

    assert git_ops.add_remote(repo, "other", str(other)).ok

    assert [r.name for r in git_ops.list_remotes(repo)] == ["other"]
    assert _git(repo, "branch", "-r").stdout.strip() == ""


def test_a_name_already_taken_is_refused(repo):
    assert git_ops.add_remote(repo, "origin", "https://example.com/a.git").ok

    result = git_ops.add_remote(repo, "origin", "https://example.com/b.git")

    assert not result.ok and result.stderr.strip()
    assert git_ops.list_remotes(repo) == [
        git_ops.Remote("origin", "https://example.com/a.git")
    ]


def test_removing_a_remote_stops_a_branch_tracking_it(repo):
    git_ops.add_remote(repo, "upstream", "https://example.com/up.git")
    git_ops.set_tracking_remote(repo, "main", "upstream")

    assert git_ops.remove_remote(repo, "upstream").ok

    assert git_ops.list_remotes(repo) == []
    assert git_ops.tracking_remote(repo, "main") == ""


# ---- which one a branch tracks -------------------------------------------------------
def test_a_branch_never_pushed_can_be_set_to_track_a_remote(repo):
    """What `--set-upstream-to` refuses: the remote has no copy of the branch yet."""
    git_ops.add_remote(repo, "upstream", "https://example.com/up.git")

    assert git_ops.set_tracking_remote(repo, "main", "upstream").ok

    assert git_ops.tracking_remote(repo, "main") == "upstream"
    assert _git(repo, "config", "--get", "branch.main.merge").stdout.strip() == (
        "refs/heads/main"
    )


def test_choosing_another_remote_keeps_the_branch_it_tracks_there(repo):
    """A branch tracking trunk goes on tracking trunk, on whichever remote."""
    git_ops.add_remote(repo, "upstream", "https://example.com/up.git")
    _git(repo, "config", "branch.main.merge", "refs/heads/trunk")

    git_ops.set_tracking_remote(repo, "main", "upstream")

    assert _git(repo, "config", "--get", "branch.main.merge").stdout.strip() == (
        "refs/heads/trunk"
    )


def test_a_branch_tracking_nothing_says_so(repo):
    assert git_ops.tracking_remote(repo, "main") == ""
    assert git_ops.tracking_remote(repo, "") == ""


def test_a_push_goes_to_the_tracked_remote_then_origin_then_the_first(repo):
    for name in ("backup", "origin", "upstream"):
        git_ops.add_remote(repo, name, f"https://example.com/{name}.git")
    assert git_ops.push_remote(repo) == "origin"

    git_ops.set_tracking_remote(repo, "main", "upstream")
    assert git_ops.push_remote(repo) == "upstream"

    git_ops.remove_remote(repo, "upstream")
    git_ops.remove_remote(repo, "origin")
    assert git_ops.push_remote(repo) == "backup"


# ---- where a first push goes ---------------------------------------------------------
def test_a_first_push_goes_to_the_remote_the_branch_tracks(repo, tmp_path):
    origin, backup = _bare(tmp_path / "origin.git"), _bare(tmp_path / "backup.git")
    git_ops.add_remote(repo, "origin", str(origin))
    git_ops.add_remote(repo, "backup", str(backup))
    git_ops.set_tracking_remote(repo, "main", "backup")

    result = git_ops.push(repo)

    assert result.ok, result.stderr
    assert _branches_in(backup) == ["main"]
    assert _branches_in(origin) == []
    assert git_ops.get_upstream(repo) == "backup/main"


def test_a_remote_named_for_the_push_is_where_it_goes(repo, tmp_path):
    """What the MCP tool does when it is given one."""
    origin, backup = _bare(tmp_path / "origin.git"), _bare(tmp_path / "backup.git")
    git_ops.add_remote(repo, "origin", str(origin))
    git_ops.add_remote(repo, "backup", str(backup))
    git_ops.set_tracking_remote(repo, "main", "backup")

    assert git_ops.push(repo, "origin").ok

    assert _branches_in(origin) == ["main"]
    assert _branches_in(backup) == []


def test_a_first_push_of_a_branch_tracking_nothing_still_goes_to_origin(repo, tmp_path):
    origin, backup = _bare(tmp_path / "origin.git"), _bare(tmp_path / "backup.git")
    git_ops.add_remote(repo, "backup", str(backup))
    git_ops.add_remote(repo, "origin", str(origin))

    assert git_ops.push(repo).ok

    assert _branches_in(origin) == ["main"]


# ---- what authenticates it -----------------------------------------------------------
def test_what_authenticates_a_push_follows_the_tracked_remote(repo):
    git_ops.add_remote(repo, "origin", "https://ONEoo7@github.com/ONEoo7/thing.git")
    git_ops.add_remote(repo, "work", "git@gitlab.example.com:team/thing.git")
    assert (git_ops.describe_push_auth(repo).remote, git_ops.describe_push_auth(repo).host) == (
        "origin",
        "github.com",
    )

    git_ops.set_tracking_remote(repo, "main", "work")

    auth = git_ops.describe_push_auth(repo)
    assert (auth.remote, auth.kind, auth.host) == ("work", "ssh", "gitlab.example.com")


def test_a_remote_that_is_a_folder_is_called_one(repo, tmp_path):
    """It used to read "no remote", which it is not."""
    folder = _bare(tmp_path / "backup.git")
    git_ops.add_remote(repo, "backup", str(folder))

    auth = git_ops.describe_push_auth(repo)

    assert auth.remote == "backup"
    assert auth.destination() == git_ops.LOCAL_REMOTE
    assert auth.summary() == f"push: {git_ops.LOCAL_REMOTE}"
    assert auth.warning() == ""
