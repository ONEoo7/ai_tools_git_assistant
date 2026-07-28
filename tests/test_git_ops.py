import os
import subprocess
import sys

import pytest

from git_assistant import git_ops
from git_assistant.commit_generator import render_template

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args, stdin=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin,
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    return tmp_path


def test_is_git_repo(repo, tmp_path):
    assert git_ops.is_git_repo(repo)
    assert not git_ops.is_git_repo(tmp_path / "does-not-exist")


def test_staged_diff_and_commit(repo):
    (repo / "hello.py").write_text("print('hi')\n", encoding="utf-8")
    _git(repo, "add", "hello.py")

    assert git_ops.has_changes(repo, "cached")
    stat = git_ops.get_diffstat(repo, "cached")
    assert "hello.py" in stat
    diff = git_ops.get_diff(repo, "cached")
    assert "print('hi')" in diff

    result = git_ops.commit(repo, "feat: add hello\n\nInitial script.")
    assert result.ok, result.stderr

    log = _git(repo, "log", "-1", "--pretty=%s").stdout.strip()
    assert log == "feat: add hello"


def test_no_changes_when_clean(repo):
    assert not git_ops.has_changes(repo, "cached")


def test_find_git_repos_discovers_multiple(tmp_path):
    for name in ("alpha", "beta"):
        (tmp_path / name / ".git").mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    # nested repo below a repo boundary is NOT returned
    (tmp_path / "alpha" / "nested" / ".git").mkdir(parents=True)
    # a repo one level down under a plain grouping dir IS returned
    (tmp_path / "group" / "gamma" / ".git").mkdir(parents=True)

    found = git_ops.find_git_repos(tmp_path)
    names = sorted(os.path.basename(p) for p in found)
    assert names == ["alpha", "beta", "gamma"]


def test_find_git_repos_prunes_noise_dirs(tmp_path):
    (tmp_path / "real" / ".git").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / ".git").mkdir(parents=True)
    found = [os.path.basename(p) for p in git_ops.find_git_repos(tmp_path)]
    assert found == ["real"]


def test_has_git_dir(tmp_path):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    assert git_ops.has_git_dir(tmp_path / "repo")
    assert not git_ops.has_git_dir(tmp_path / "plain")
    assert not git_ops.has_git_dir(tmp_path / "does-not-exist")


def test_find_git_repos_root_is_repo(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "child" / ".git").mkdir(parents=True)
    found = git_ops.find_git_repos(tmp_path)
    assert found == [os.path.normpath(str(tmp_path))]


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://github.com/ONEoo7/ai_tools.git", ("ONEoo7", "ai_tools")),
        ("https://github.com/ONEoo7/ai_tools", ("ONEoo7", "ai_tools")),
        ("git@github.com:ONEoo7/ai_tools.git", ("ONEoo7", "ai_tools")),
        ("ssh://git@github.com/ONEoo7/ai_tools.git", ("ONEoo7", "ai_tools")),
        ("https://gitlab.com/grp/sub/repo.git", ("sub", "repo")),
        ("", (None, None)),
        ("not-a-url", (None, "not-a-url")),
    ],
)
def test_parse_owner_repo(url, expected):
    assert git_ops.parse_owner_repo(url) == expected


def test_repo_owner_from_real_remote(repo):
    _git(repo, "remote", "add", "origin", "https://github.com/ONEoo7/ai_tools.git")
    assert git_ops.repo_owner(repo) == "ONEoo7"


def test_repo_owner_none_without_remote(repo):
    assert git_ops.repo_owner(repo) is None


def test_resolve_repo_meta_with_remote(repo):
    _git(repo, "remote", "add", "origin", "git@github.com:ONEoo7/x.git")
    assert git_ops.resolve_repo_meta(repo) == ("ONEoo7", False)


def test_resolve_repo_meta_no_remote(repo):
    # No remote, but accessible -> owner empty, not blocked.
    assert git_ops.resolve_repo_meta(repo) == ("", False)


def test_trust_all_repositories_isolated(tmp_path, monkeypatch):
    # Redirect the *global* git config to a temp file so the real one is untouched.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    assert git_ops.safe_directory_is_all() is False

    first = git_ops.trust_all_repositories()
    assert first.ok
    assert first.stdout.strip() != "already trusted"
    assert git_ops.safe_directory_is_all() is True

    # Idempotent: a second call detects it and does not add a duplicate.
    second = git_ops.trust_all_repositories()
    assert second.ok and second.stdout.strip() == "already trusted"


def test_render_template_handles_braces():
    tmpl = "branch={branch}\nstat={diffstat}\ndiff={diff}"
    out = render_template(
        tmpl, branch="main", diffstat="1 file", diff="code with { braces }"
    )
    assert "branch=main" in out
    assert "code with { braces }" in out


# ---- push -------------------------------------------------------------------
@pytest.fixture
def repo_with_remote(repo, tmp_path):
    """A repo with one commit and a local bare remote as 'origin'."""
    bare = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(bare)],
        capture_output=True, creationflags=_NO_WINDOW, check=True,
    )
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(bare))
    return repo


def test_no_upstream_before_first_push(repo_with_remote):
    assert git_ops.get_upstream(repo_with_remote) is None
    assert git_ops.unpushed_count(repo_with_remote) is None


def test_push_sets_upstream_and_publishes(repo_with_remote):
    result = git_ops.push(repo_with_remote)
    assert result.ok, result.stderr
    upstream = git_ops.get_upstream(repo_with_remote)
    assert upstream is not None and upstream.startswith("origin/")
    assert git_ops.unpushed_count(repo_with_remote) == 0


def test_unpushed_count_tracks_new_commits(repo_with_remote):
    git_ops.push(repo_with_remote)
    (repo_with_remote / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo_with_remote, "add", "b.txt")
    _git(repo_with_remote, "commit", "-m", "second")
    assert git_ops.unpushed_count(repo_with_remote) == 1
    assert git_ops.push(repo_with_remote).ok
    assert git_ops.unpushed_count(repo_with_remote) == 0


def test_push_reports_failure_without_remote(repo):
    (repo / "x.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "x.txt")
    _git(repo, "commit", "-m", "c")
    result = git_ops.push(repo)  # no 'origin' configured
    assert not result.ok
    assert result.stderr.strip()


# ---- tags -------------------------------------------------------------------
def test_tag_lifecycle(repo):
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "init")

    assert git_ops.list_tags(repo) == []
    assert not git_ops.tag_exists(repo, "v0.1.0")

    assert git_ops.create_tag(repo, "v0.1.0").ok
    assert git_ops.tag_exists(repo, "v0.1.0")
    assert "v0.1.0" in git_ops.list_tags(repo)

    # annotated tag carries a message
    assert git_ops.create_tag(repo, "v0.2.0", "second release").ok
    kind = _git(repo, "cat-file", "-t", "v0.2.0").stdout.strip()
    assert kind == "tag"  # annotated tags are real objects

    # newest-version-first ordering
    assert git_ops.list_tags(repo)[0] == "v0.2.0"

    assert git_ops.delete_tag(repo, "v0.1.0").ok
    assert not git_ops.tag_exists(repo, "v0.1.0")


def test_create_tag_lightweight_has_no_object(repo):
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "init")
    git_ops.create_tag(repo, "plain")
    assert _git(repo, "cat-file", "-t", "plain").stdout.strip() == "commit"


def test_push_tag_publishes_to_remote(repo_with_remote):
    git_ops.create_tag(repo_with_remote, "v1.0.0", "release")
    result = git_ops.push_tag(repo_with_remote, "v1.0.0")
    assert result.ok, result.stderr
    remote_tags = _git(repo_with_remote, "ls-remote", "--tags", "origin").stdout
    assert "v1.0.0" in remote_tags


def test_list_tags_with_dates(repo):
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "init")
    git_ops.create_tag(repo, "v0.1.0")
    git_ops.create_tag(repo, "v0.2.0", "annotated release")

    dated = git_ops.list_tags_with_dates(repo)
    names = [n for n, _ in dated]
    assert names == ["v0.2.0", "v0.1.0"]  # newest version first
    for _name, created in dated:
        # "YYYY-MM-DD HH:MM"
        assert len(created) == 16 and created[4] == "-" and created[10] == " "


def test_list_tags_with_dates_empty(repo):
    assert git_ops.list_tags_with_dates(repo) == []


def test_remote_tag_exists_false_without_remote(repo):
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "init")
    git_ops.create_tag(repo, "v0.1.0")
    # No remote configured at all -> definitively "never pushed", not "unknown".
    assert git_ops.remote_tag_exists(repo, "v0.1.0") is False


def test_remote_tag_exists_tracks_push(repo_with_remote):
    git_ops.create_tag(repo_with_remote, "v1.0.0")
    assert git_ops.remote_tag_exists(repo_with_remote, "v1.0.0") is False
    assert git_ops.push_tag(repo_with_remote, "v1.0.0").ok
    assert git_ops.remote_tag_exists(repo_with_remote, "v1.0.0") is True


def test_remote_tag_exists_unknown_when_remote_unreachable(repo):
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "init")
    git_ops.create_tag(repo, "v0.1.0")
    # A configured but unreachable remote must report None, not False: deleting
    # on a failed lookup would be deleting on a guess.
    _git(repo, "remote", "add", "origin", str(repo / "does-not-exist.git"))
    assert git_ops.remote_tag_exists(repo, "v0.1.0") is None


def test_delete_tag_removes_only_that_tag(repo):
    (repo / "f.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-m", "init")
    git_ops.create_tag(repo, "v0.1.0")
    git_ops.create_tag(repo, "v0.2.0")
    assert git_ops.delete_tag(repo, "v0.1.0").ok
    assert git_ops.list_tags(repo) == ["v0.2.0"]
