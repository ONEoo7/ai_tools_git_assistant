"""What each submodule of a repository is at -- read from real repositories."""

import subprocess
import sys

import pytest

from git_assistant import git_ops

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture(autouse=True)
def git_home(tmp_path, monkeypatch):
    """An identity, a fixed clock, and local folders allowed as submodule sources."""
    config = tmp_path / "global.gitconfig"
    config.write_text(
        '[protocol "file"]\n\tallow = always\n'
        "[user]\n\tname = Test Author\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = main\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-02T03:04:05+02:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-05T06:07:08+02:00")


def library(path, *messages):
    """A repository with a commit for each of ``messages``, the last one checked out."""
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    for number, message in enumerate(messages):
        (path / "f.txt").write_text(f"{number}\n", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", message)
    return path


def add_submodule(top, source, path, *extra):
    _git(top, "submodule", "add", "-q", *extra, str(source), path)
    _git(top, "commit", "-q", "-m", f"add {path}")


def head(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def can(tmp_path):
    source = library(tmp_path / "sources" / "can", "first frames", "second frames")
    _git(source, "tag", "-a", "v2.0", "-m", "release", "HEAD")
    _git(source, "tag", "release/stable", "HEAD")
    _git(source, "tag", "v1.0", "HEAD~1")
    return source


def test_each_submodule_is_listed_with_the_commit_it_is_at(tmp_path, can):
    top = library(tmp_path / "top", "start")
    add_submodule(top, can, "libs/can")

    [state] = git_ops.submodule_states(top)

    assert (state.path, state.name, state.url) == ("libs/can", "libs/can", str(can))
    commit = state.checked_out
    assert commit.hash == head(can) == state.recorded_hash
    assert commit.short == _git(can, "rev-parse", "--short", "HEAD").stdout.strip()
    assert commit.subject == "second frames"
    assert set(commit.tags) == {"v2.0", "release/stable"}  # annotated and lightweight
    assert (commit.date, commit.authored) == ("2026-01-05 06:07", "2026-01-02 03:04")
    assert commit.author == "Test Author"
    assert state.recorded == commit
    assert state.problem == ""


def test_a_submodule_checked_out_elsewhere_has_both_commits(tmp_path, can):
    top = library(tmp_path / "top", "start")
    add_submodule(top, can, "libs/can")
    _git(top / "libs" / "can", "checkout", "-q", "HEAD~1")

    [state] = git_ops.submodule_states(top)

    assert state.checked_out.subject == "first frames"
    assert state.checked_out.tags == ("v1.0",)
    assert state.recorded.subject == "second frames"
    assert state.recorded_hash == state.recorded.hash != state.checked_out.hash


def test_a_submodule_never_checked_out_is_listed_with_what_is_recorded(tmp_path, can):
    top = library(tmp_path / "top", "start")
    add_submodule(top, can, "libs/can")
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", "-q", str(top), str(clone)],
        capture_output=True,
        creationflags=_NO_WINDOW,
        check=True,
    )

    [state] = git_ops.submodule_states(clone)

    assert state.problem == git_ops.NOT_CHECKED_OUT
    assert state.checked_out is None and state.recorded is None
    assert state.recorded_hash == head(can)


def test_a_commit_the_checkout_does_not_have_is_recorded_without_its_details(tmp_path, can):
    """The repository was updated and the submodule was not: common, after a pull."""
    top = library(tmp_path / "top", "start")
    add_submodule(top, can, "libs/can")
    unknown = "0123456789abcdef0123456789abcdef01234567"
    _git(top, "update-index", "--cacheinfo", f"160000,{unknown},libs/can")
    _git(top, "commit", "-q", "-m", "move can")

    [state] = git_ops.submodule_states(top)

    assert state.recorded_hash == unknown
    assert state.recorded is None
    assert state.checked_out.hash == head(can)


def test_nested_submodules_follow_the_one_they_are_in_and_every_level_is_by_path(tmp_path, can):
    tiny = library(tmp_path / "sources" / "tiny", "tiny")
    vendor = library(tmp_path / "sources" / "vendor", "vendored")
    add_submodule(vendor, tiny, "deps/tiny")
    core = library(tmp_path / "sources" / "core", "core")
    add_submodule(core, vendor, "third_party/vendor")
    alpha = library(tmp_path / "sources" / "alpha", "alpha")
    top = library(tmp_path / "top", "start")
    # Added in an order that is neither the order of path nor its reverse.
    add_submodule(top, can, "libs/Can")
    add_submodule(top, core, "libs/core")
    add_submodule(top, alpha, "libs/alpha")
    _git(top, "submodule", "update", "-q", "--init", "--recursive")

    states = git_ops.submodule_states(top)

    assert [s.path for s in states] == [
        "libs/alpha",
        "libs/Can",
        "libs/core",
        "libs/core/third_party/vendor",
        "libs/core/third_party/vendor/deps/tiny",
    ]
    nested = states[3]
    assert nested.checked_out.subject == "add deps/tiny"  # adding tiny was its last commit
    assert nested.recorded_hash == head(vendor)
    assert states[4].checked_out.subject == "tiny"


def test_a_name_that_is_not_its_path_and_a_path_with_a_space(tmp_path, can):
    top = library(tmp_path / "top", "start")
    add_submodule(top, can, "Can Module", "--name", "the.can")

    [state] = git_ops.submodule_states(top)

    assert (state.path, state.name) == ("Can Module", "the.can")
    assert state.checked_out.subject == "second frames"


def test_a_repository_without_submodules_has_none(tmp_path):
    assert git_ops.submodule_states(library(tmp_path / "plain", "start")) == []


def test_a_submodule_at_its_recorded_commit_is_read_once(tmp_path, can, monkeypatch):
    """One git command for its commit, not one for each of its two names for it."""
    top = library(tmp_path / "top", "start")
    add_submodule(top, can, "libs/can")
    logs = []
    real = git_ops._run

    def run(repo, args, **kwargs):
        if args[:1] == ["log"]:
            logs.append(args[-2])
        return real(repo, args, **kwargs)

    monkeypatch.setattr(git_ops, "_run", run)

    git_ops.submodule_states(top)

    assert logs == ["HEAD"]
