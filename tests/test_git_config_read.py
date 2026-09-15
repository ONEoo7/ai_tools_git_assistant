"""A repository's configuration read in one git command, answering as git answers.

Each answer is compared with git's own, asked the old way: one ``git config``
or ``git remote`` for each question.
"""

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
        encoding="utf-8",
        creationflags=_NO_WINDOW,
    )


def _get(repo, *args):
    res = _git(repo, "config", *args)
    return res.stdout.strip() if res.returncode == 0 else ""


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A global config of the test's own, and no system one."""
    config = tmp_path / "global.gitconfig"
    config.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return config


@pytest.fixture
def repo(tmp_path, home):
    work = tmp_path / "work"
    work.mkdir()
    assert _git(work, "init", "-q").returncode == 0
    return work


def _write(path, text):
    path.write_text(text, encoding="utf-8")


def test_the_identity_is_the_one_git_would_commit_as(repo, home, tmp_path):
    _write(home, "[user]\n\tname = Global Name\n\temail = global@example.com\n")
    assert git_ops.get_identity(repo) == ("Global Name", "global@example.com")
    assert git_ops.get_local_identity(repo) == ("", "")

    _git(repo, "config", "user.email", "local@example.com")
    assert git_ops.get_identity(repo) == ("Global Name", "local@example.com")
    assert git_ops.get_local_identity(repo) == ("", "local@example.com")


def test_a_conditional_include_is_followed(repo, home, tmp_path):
    """How one folder of repositories commits as a work account."""
    work = tmp_path / "work.gitconfig"
    _write(work, "[user]\n\tname = Work Name\n\temail = work@example.com\n")
    _write(
        home,
        "[user]\n\tname = Home Name\n"
        f'[includeIf "gitdir/i:{repo.as_posix()}/"]\n\tpath = {work.as_posix()}\n',
    )

    config = git_ops.read_config(repo)

    assert config.identity() == (_get(repo, "user.name"), _get(repo, "user.email"))
    assert config.identity() == ("Work Name", "work@example.com")


@pytest.mark.parametrize(
    "written",
    ["\tgpgsign\n", "\tgpgsign = true\n", "\tgpgsign = yes\n", "\tgpgsign = 1\n",
     "\tgpgsign = false\n", "\tgpgsign = off\n", "\tgpgsign = 0\n", "\tgpgsign = true\n\tgpgsign = no\n",
     ""],
)
def test_signing_is_on_as_git_reads_a_boolean(repo, written):
    _write(repo / ".git" / "config", (repo / ".git" / "config").read_text() + "[commit]\n" + written)

    expected = _get(repo, "--type=bool", "commit.gpgsign") == "true"
    assert git_ops.signing_enabled(repo) is expected


def test_the_last_value_of_a_key_set_twice_is_the_one(repo, home):
    _write(home, "[user]\n\tsigningkey = first\n")
    _git(repo, "config", "user.signingkey", "second")

    assert git_ops.get_signingkey(repo) == _get(repo, "user.signingkey") == "second"


def test_remotes_are_the_ones_git_lists(repo, home, tmp_path):
    """Rewritten by ``insteadOf`` -- the longest match -- and the first URL of several."""
    _write(
        home,
        '[url "https://github.com/"]\n\tinsteadOf = gh:\n'
        '[url "https://mirror.example.com/ONEoo7/"]\n\tinsteadOf = gh:ONEoo7/\n',
    )
    _git(repo, "remote", "add", "origin", "gh:ONEoo7/thing.git")
    _git(repo, "remote", "add", "Upstream", "gh:someone/thing.git")
    _git(repo, "remote", "add", "folder", str(tmp_path / "with space" / "b.git"))
    _git(repo, "config", "--add", "remote.folder.url", "https://second.example.com/b.git")

    listed = {}
    for line in _git(repo, "remote", "-v").stdout.splitlines():
        name, _tab, rest = line.partition("\t")
        if rest.endswith(" (fetch)"):
            listed.setdefault(name, rest[: -len(" (fetch)")])

    remotes = git_ops.list_remotes(repo)
    assert {r.name: r.url for r in remotes} == listed
    assert [r.name for r in remotes] == ["folder", "origin", "Upstream"]
    assert listed["origin"] == "https://mirror.example.com/ONEoo7/thing.git"


def test_what_a_branch_tracks_is_read_with_the_rest(repo):
    _git(repo, "config", "branch.Feature/One.remote", "work")

    config = git_ops.read_config(repo)

    assert config.tracking_remote("Feature/One") == "work"
    assert config.tracking_remote("feature/one") == ""  # a branch name keeps its case
    assert config.tracking_remote("") == ""


def test_a_credential_for_one_host_is_found_by_its_address(repo, home):
    """A subsection holding dots, and a name written in any case."""
    _write(
        home,
        '[credential "https://github.com"]\n\tuseHttpPath = true\n\tUserName = octo\n',
    )
    _git(repo, "remote", "add", "origin", "https://github.com/ONEoo7/thing.git")

    config = git_ops.read_config(repo)

    assert config.first(["credential.https://github.com.username", "credential.username"]) == "octo"
    assert config.get("credential.https://github.com.useHttpPath") == "true"
    assert config.get("credential.https://GITHUB.com.useHttpPath") == ""
    auth = git_ops.describe_push_auth(repo)
    assert (auth.account, auth.shared) == ("octo", False)


def test_a_value_over_several_lines_is_kept_whole(repo):
    _git(repo, "config", "commit.template-note", "one\ntwo")

    assert git_ops.read_config(repo).get("commit.template-note") == "one\ntwo"


def test_a_folder_that_is_no_repository_reads_as_nothing_set(tmp_path, home):
    assert git_ops.read_config(tmp_path / "missing").entries == []


def test_every_answer_comes_from_one_git_command(repo, monkeypatch):
    _git(repo, "remote", "add", "origin", "https://github.com/ONEoo7/thing.git")
    config_runs = []
    real = git_ops._run_bytes
    monkeypatch.setattr(
        git_ops, "_run_bytes", lambda r, args, **k: (config_runs.append(args), real(r, args, **k))[1]
    )
    monkeypatch.setattr(git_ops, "_run", lambda *a, **k: pytest.fail(f"a git command: {a}"))

    config = git_ops.read_config(repo)
    config.identity(), config.local_identity(), config.remotes(), config.get_bool("commit.gpgsign")
    git_ops.push_auth_from(config, "main")

    assert config_runs == [["config", "--list", "-z", "--show-scope"]]
