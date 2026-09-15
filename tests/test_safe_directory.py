"""Which repositories git trusts, and its owner check skipped where that is settled.

Every answer is checked against git's: the same list is written to a global config
file of the test's own, and git is asked whether it will work in the repository
with the owner check off -- which leaves safe.directory as the only thing deciding.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from git_assistant import git_ops

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(*args, cwd=None, env=None):
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        cwd=cwd,
        env=env,
    )


def _init(path):
    path.mkdir(parents=True, exist_ok=True)
    assert _git("init", "-q", str(path)).returncode == 0
    return path


def _git_accepts(repo):
    """Whether git works in ``repo`` when only safe.directory can say yes."""
    env = {**os.environ, git_ops.SKIP_OWNER_CHECK: "1"}
    return _git("-C", str(repo), "rev-parse", "--git-dir", env=env).returncode == 0


@pytest.fixture
def listed(tmp_path, monkeypatch):
    """Make the values given the whole safe.directory list, in a file of this test's.

    A new file each time: the list is read once for the configuration it is read
    under, as the application reads it, and a new file is a new configuration.
    """
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    made = []

    def make(*values):
        path = tmp_path / f"global-{len(made)}.gitconfig"
        path.write_text("", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(path))
        for value in values:
            added = _git("config", "--global", "--add", "safe.directory", value, cwd=tmp_path)
            assert added.returncode == 0, added.stderr
        made.append(path)
        return path

    make()
    return make


@pytest.fixture
def place(tmp_path):
    """Repositories to ask about, in a folder that is not one."""
    for rel in ("holder/repo", "holder/repo/sub", "holder-other/repo"):
        _init(tmp_path / rel)
    return tmp_path


def _home():
    return os.environ.get("HOME") or os.path.expanduser("~")


#: Ways of listing a repository, by what they are. ``real`` is the temporary folder
#: as it is on disk; ``given`` as the system names it, which on Windows can carry a
#: short name -- ``STEFAN~1`` for a long one -- that git resolves for a path and not
#: for a folder ending in ``/*``.
FORMS = {
    "the path": lambda real, given: [f"{real}/holder/repo"],
    "the path in other case": lambda real, given: [f"{real}/holder/repo".swapcase()],
    "the path with backslashes": lambda real, given: [f"{real}/holder/repo".replace("/", "\\")],
    "the path with a slash after": lambda real, given: [f"{real}/holder/repo/"],
    "the path as the system gives it": lambda real, given: [f"{given}/holder/repo"],
    "the folder above": lambda real, given: [f"{real}/holder/*"],
    "the folder above in other case": lambda real, given: [f"{real}/holder/*".swapcase()],
    "the folder above with backslashes": lambda real, given: [
        f"{real}/holder/*".replace("/", "\\")
    ],
    "the folder above as the system gives it": lambda real, given: [f"{given}/holder/*"],
    "a folder further up": lambda real, given: [f"{real}/*"],
    "the repository as a folder": lambda real, given: [f"{real}/holder/repo/*"],
    "a name that starts the same": lambda real, given: [f"{real}/hold*"],
    "everything": lambda real, given: ["*"],
    "taken back": lambda real, given: [f"{real}/holder/repo", ""],
    "taken back, then listed": lambda real, given: ["", f"{real}/holder/repo"],
    "a relative path": lambda real, given: ["holder/repo"],
    "nothing": lambda real, given: [],
}


@pytest.mark.parametrize("target", ["holder/repo", "holder/repo/sub", "holder-other/repo"])
@pytest.mark.parametrize("form", sorted(FORMS))
def test_a_listed_repository_is_one_git_accepts(place, listed, form, target):
    real = Path(os.path.realpath(place)).as_posix()
    listed(*FORMS[form](real, place.as_posix()))
    repo = place / target

    assert git_ops.is_safe_directory(repo) is _git_accepts(repo)


@pytest.mark.parametrize("form", ["~/{rel}/holder/repo", "~/{rel}/holder/*"])
def test_the_home_folder_can_be_spelt_with_a_tilde(place, listed, form):
    home = Path(os.path.realpath(_home()))
    real = Path(os.path.realpath(place))
    if not real.is_relative_to(home):
        pytest.skip("the temporary folder is not in the home folder here")
    listed(form.format(rel=real.relative_to(home).as_posix()))
    repo = place / "holder" / "repo"

    assert git_ops.is_safe_directory(repo) is _git_accepts(repo) is True


def test_a_repository_cannot_list_itself(place, listed, monkeypatch):
    """Only the system, global and command-line configuration count."""
    repo = place / "holder" / "repo"
    listed()
    assert _git("-C", str(repo), "config", "safe.directory", repo.as_posix()).returncode == 0
    assert git_ops.is_safe_directory(repo) is _git_accepts(repo) is False

    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", os.path.realpath(repo))
    assert git_ops.is_safe_directory(repo) is _git_accepts(repo) is True


def test_the_list_is_what_git_reads_after_the_last_empty_value(listed):
    listed("D:/a", "", "D:/b/*", "D:/c")

    assert git_ops.safe_directories() == ["D:/b/*", "D:/c"]


def test_everything_under_a_folder(place, listed):
    real = Path(os.path.realpath(place)).as_posix()

    assert git_ops.trusts_everything_under(place / "holder", [f"{real}/*"])
    assert git_ops.trusts_everything_under(place / "holder", [f"{real}/holder/*"])
    assert git_ops.trusts_everything_under(place / "holder", ["*"])
    assert not git_ops.trusts_everything_under(place / "holder", [f"{real}/holder/repo/*"])
    assert not git_ops.trusts_everything_under(place / "holder", [f"{real}/holder"])
    assert not git_ops.trusts_everything_under(place / "holder-other", [f"{real}/holder/*"])


# ---- marking ------------------------------------------------------------------------
def test_a_folder_is_one_line_spelt_as_git_spells_what_is_in_it(place, listed):
    """In the folder's own case and long name, whatever the folder was picked as."""
    config = listed()
    given = str(place / "holder").swapcase() if sys.platform == "win32" else str(place / "holder")

    marked = git_ops.mark_safe(given, folder=True)

    line = Path(os.path.realpath(place / "holder")).as_posix() + "/*"
    assert marked == git_ops.Marked(added=[line])
    assert _git("config", "--file", str(config), "--get-all", "safe.directory").stdout.split(
        "\n"
    )[:-1] == [line]
    assert _git_accepts(place / "holder" / "repo")
    assert _git_accepts(place / "holder" / "repo" / "sub")
    assert not _git_accepts(place / "holder-other" / "repo")


def test_a_folder_already_covered_is_not_written_again(place, listed):
    listed()
    git_ops.mark_safe(place / "holder", folder=True)

    assert git_ops.mark_safe(place / "holder", folder=True) == git_ops.Marked()
    assert git_ops.mark_safe(place / "holder" / "repo", folder=True) == git_ops.Marked()
    assert git_ops.mark_safe(place / "holder" / "repo", folder=False) == git_ops.Marked()


def test_a_folder_that_is_a_repository_gets_a_line_of_its_own(place, listed):
    """``/*`` is what is beneath a folder, and not the folder."""
    listed()
    repo = place / "holder" / "repo"

    marked = git_ops.mark_safe(repo, folder=True)

    real = Path(os.path.realpath(repo)).as_posix()
    assert marked.added == [real, real + "/*"]
    assert _git_accepts(repo) and _git_accepts(repo / "sub")


def test_a_repository_is_a_line_for_it_and_one_for_each_submodule(place, listed):
    listed()
    repo = place / "holder" / "repo"
    (repo / ".gitmodules").write_text(
        '[submodule "sub"]\n\tpath = sub\n\turl = https://example.invalid/sub.git\n',
        encoding="utf-8",
    )

    marked = git_ops.mark_safe(repo, folder=False)

    assert marked.added == [
        Path(os.path.realpath(repo)).as_posix(),
        Path(os.path.realpath(repo / "sub")).as_posix(),
    ]
    assert _git_accepts(repo) and _git_accepts(repo / "sub")
    assert not _git_accepts(place / "holder-other" / "repo")
    assert git_ops.mark_safe(repo, folder=False) == git_ops.Marked()


def test_a_line_git_cannot_write_is_said(place, listed, monkeypatch):
    listed()
    unwritable = place / "a-folder-not-a-file"
    unwritable.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(unwritable))

    marked = git_ops.mark_safe(place / "holder", folder=True)

    assert marked.added == [] and marked.problem


def test_tests_never_mark_anything_in_the_real_global_config(place, monkeypatch):
    """What conftest makes of a test that forgets to point git elsewhere."""
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    written = []
    real = git_ops._run_global

    def run(args):
        if "--add" in args:  # recorded, and never let through
            written.append(args)
            return git_ops.GitResult(ok=True, stdout="", stderr="", returncode=0)
        return real(args)

    monkeypatch.setattr(git_ops, "_run_global", run)

    assert git_ops.add_safe_lines(["D:/anything/*"]) == git_ops.Marked()
    assert git_ops.mark_safe(place / "holder" / "repo", folder=False) == git_ops.Marked()
    assert written == []


# ---- the owner check skipped ----------------------------------------------------------
@pytest.mark.parametrize(
    "args, verb",
    [
        (["status", "--porcelain"], "status"),
        (["-c", "core.quotepath=off", "diff", "--cached"], "diff"),
        (["--literal-pathspecs", "add", "-A"], "add"),
        (["-C", "elsewhere", "log"], "log"),
        (["--version"], ""),
    ],
)
def test_the_command_is_the_first_word_that_is_not_an_option(args, verb):
    assert git_ops._verb(args) == verb


def test_the_check_is_skipped_only_where_the_list_covers_the_repository(place, listed):
    real = Path(os.path.realpath(place)).as_posix()
    listed(f"{real}/holder/*")

    covered = git_ops._env_for(place / "holder" / "repo", ["status"])
    assert covered is not None and covered[git_ops.SKIP_OWNER_CHECK] == "1"
    assert git_ops._env_for(place / "holder-other" / "repo", ["status"]) is None


@pytest.mark.parametrize("verb", ["commit", "push", "fetch", "pull", "merge", "switch", "checkout", "ls-remote", "submodule"])
def test_a_command_that_runs_hooks_or_reaches_elsewhere_keeps_the_check(place, listed, verb):
    """A hook or a fetch from a folder inherits the skip, for repositories it may not cover."""
    listed("*")

    assert git_ops._env_for(place / "holder" / "repo", [verb]) is None


def _launches(monkeypatch):
    seen = []
    real = subprocess.run

    def run(argv, *args, **kwargs):
        if argv[:1] == ["git"] and "-C" in argv:
            env = kwargs.get("env")
            seen.append(bool(env and env.get(git_ops.SKIP_OWNER_CHECK)))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(git_ops.subprocess, "run", run)
    return seen


def test_commands_in_a_covered_repository_skip_the_check(place, listed, monkeypatch):
    real = Path(os.path.realpath(place)).as_posix()
    listed(f"{real}/holder/*")
    launches = _launches(monkeypatch)

    assert git_ops.is_git_repo(place / "holder" / "repo")
    assert git_ops.read_config(place / "holder" / "repo").entries

    assert launches == [True, True]


def test_the_list_is_read_once_for_every_command(place, listed, monkeypatch):
    real = Path(os.path.realpath(place)).as_posix()
    listed(f"{real}/holder/*")
    reads = []
    real_global = git_ops._run_global
    monkeypatch.setattr(
        git_ops, "_run_global", lambda args: (reads.append(args), real_global(args))[1]
    )

    for _ in range(3):
        git_ops.is_git_repo(place / "holder" / "repo")
        git_ops.is_git_repo(place / "holder-other" / "repo")

    assert len(reads) == 1


def test_a_repository_git_refuses_all_the_same_is_asked_the_ordinary_way(
    place, listed, monkeypatch
):
    """The list changed after it was read: once more with the check, and from then on."""
    real = Path(os.path.realpath(place)).as_posix()
    config = listed(f"{real}/holder/*")
    repo = place / "holder" / "repo"
    assert git_ops.is_safe_directory(repo)
    config.write_text("", encoding="utf-8")  # behind the application's back
    launches = _launches(monkeypatch)

    assert git_ops.is_git_repo(repo)
    assert launches == [True, False]

    assert git_ops.is_git_repo(repo)
    assert launches == [True, False, False]


def test_marking_something_asks_again_where_git_refused(place, listed, monkeypatch):
    real = Path(os.path.realpath(place)).as_posix()
    config = listed(f"{real}/holder/*")
    repo = place / "holder" / "repo"
    assert git_ops.is_safe_directory(repo)
    config.write_text("", encoding="utf-8")
    assert git_ops.is_git_repo(repo)  # refused under the skip, and remembered

    git_ops.mark_safe(place / "holder", folder=True)
    launches = _launches(monkeypatch)

    assert git_ops.is_git_repo(repo)
    assert launches == [True]
