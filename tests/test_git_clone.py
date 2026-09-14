"""Cloning and creating repositories, against real repositories on this disk.

Every source is a local folder, so nothing here needs the network; a depth is
honoured all the same, because a shallow clone of a folder goes over file://.
Git is not mocked where its answer is the point.
"""

import stat
import subprocess
import sys
import threading
import time

import pytest

from git_assistant import git_ops

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(repo, *args, stdin=None):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        input=stdin,
        creationflags=_NO_WINDOW,
        check=True,
    )


def _out(repo, *args):
    return _git(repo, *args).stdout.decode().strip()


@pytest.fixture
def source(tmp_path):
    """Three commits on main, and a branch one commit behind it."""
    path = tmp_path / "source"
    subprocess.run(
        ["git", "init", "-q", "--initial-branch=main", str(path)],
        capture_output=True,
        creationflags=_NO_WINDOW,
        check=True,
    )
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    for n in range(3):
        (path / "f.txt").write_text(f"{n}\n", encoding="utf-8")
        _git(path, "add", "f.txt")
        _git(path, "commit", "-qm", f"commit {n}")
    _git(path, "branch", "other", "HEAD~1")
    return path


@pytest.fixture
def no_process(monkeypatch):
    """Fail the test if anything tries to start git."""

    def started(*args, **kwargs):
        raise AssertionError(f"a process was started: {args}")

    monkeypatch.setattr(subprocess, "Popen", started)


def _python(code):
    return [sys.executable, "-c", code]


# ---- cloning -------------------------------------------------------------------------
def test_a_full_clone_has_the_whole_history(source, tmp_path):
    dest = tmp_path / "copy"

    result = git_ops.clone(str(source), dest)

    assert result.ok, result.stderr
    assert result.destination == str(dest)
    assert _out(dest, "rev-list", "--count", "HEAD") == "3"
    assert _out(dest, "rev-parse", "--is-shallow-repository") == "false"


def test_a_shallow_clone_has_the_tip_of_every_branch(source, tmp_path):
    """Git's own shallow clone would take main alone, and narrow the fetch
    refspec so that no later fetch brings another branch either."""
    dest = tmp_path / "copy"

    result = git_ops.clone(str(source), dest, depth=1)

    assert result.ok, result.stderr
    assert _out(dest, "rev-parse", "--is-shallow-repository") == "true"
    assert _out(dest, "rev-list", "--count", "HEAD") == "1"
    assert "origin/other" in _out(dest, "branch", "-r").split()
    assert _out(dest, "config", "--get-all", "remote.origin.fetch") == (
        "+refs/heads/*:refs/remotes/origin/*"
    )


def test_git_reports_progress_line_by_line_while_it_clones(source, tmp_path):
    lines, percents = [], []

    result = git_ops.clone(
        str(source),
        tmp_path / "copy",
        depth=1,
        on_progress=lines.append,
        on_percent=percents.append,
    )

    assert result.ok, result.stderr
    assert lines and not any("\r" in line or "\n" in line for line in lines)
    assert percents and all(0 <= p <= 100 for p in percents)
    assert 100 in percents


def test_a_clone_that_fails_leaves_nothing_behind_and_says_why(tmp_path):
    dest = tmp_path / "copy"

    result = git_ops.clone(str(tmp_path / "no-such-repository"), dest)

    assert not result.ok
    assert result.stderr.startswith("fatal:")
    assert not dest.exists()
    assert not result.cancelled and not result.checkout_failed


@pytest.mark.skipif(sys.platform != "win32", reason="aux is only reserved on Windows")
def test_a_checkout_that_fails_keeps_the_repository_it_fetched(source, tmp_path):
    """A name Windows cannot create stands in for the usual cause, a long path."""
    blob = _git(source, "hash-object", "-w", "--stdin", stdin=b"x\n").stdout
    unprotected = ("-c", "core.protectNTFS=false")
    _git(
        source,
        *unprotected,
        "update-index",
        "--add",
        "--cacheinfo",
        f"100644,{blob.decode().strip()},aux.txt",
    )
    _git(source, *unprotected, "commit", "-qm", "unwritable")
    dest = tmp_path / "copy"

    result = git_ops.clone(str(source), dest)

    assert not result.ok
    assert result.checkout_failed
    assert (dest / ".git").is_dir(), "the repository is whole; only its files are not"
    assert "checkout failed" in result.stderr


# ---- refused before git starts -------------------------------------------------------
def test_a_folder_that_already_has_something_in_it_is_refused(tmp_path, no_process):
    dest = tmp_path / "copy"
    dest.mkdir()
    (dest / "mine.txt").write_text("mine\n", encoding="utf-8")

    result = git_ops.clone("https://example.com/org/repo.git", dest)

    assert not result.ok
    assert "not an empty folder" in result.stderr
    assert (dest / "mine.txt").read_text(encoding="utf-8") == "mine\n"


@pytest.mark.parametrize("url", ["", "   ", "--upload-pack=evil", "-x"])
def test_no_url_or_one_that_reads_as_an_option_is_refused(tmp_path, no_process, url):
    assert not git_ops.clone(url, tmp_path / "copy").ok


# ---- cancelling ----------------------------------------------------------------------
def test_cancelling_removes_what_the_clone_wrote(source, tmp_path):
    dest = tmp_path / "copy"
    asked = threading.Event()

    result = git_ops.clone(
        str(source),
        dest,
        depth=1,
        on_progress=lambda line: asked.set(),
        is_cancelled=asked.is_set,
    )

    assert result.cancelled and not result.ok
    assert not dest.exists()
    assert result.leftover == ""


def test_a_cancelled_clone_into_an_empty_folder_leaves_the_folder(source, tmp_path):
    dest = tmp_path / "copy"
    dest.mkdir()
    asked = threading.Event()

    result = git_ops.clone(
        str(source),
        dest,
        depth=1,
        on_progress=lambda line: asked.set(),
        is_cancelled=asked.is_set,
    )

    assert result.cancelled
    assert dest.is_dir() and not any(dest.iterdir())


def test_cancel_works_while_the_process_says_nothing(tmp_path):
    """A stalled network is silence, and Cancel has to get through it."""
    asked = threading.Event()
    threading.Timer(0.3, asked.set).start()
    started = time.monotonic()

    code, lines = git_ops._stream(
        _python("import time; time.sleep(60)"),
        env=None,
        on_line=lambda line: None,
        is_cancelled=asked.is_set,
    )

    assert code is None
    assert time.monotonic() - started < 10


def test_lines_end_at_carriage_returns_as_well_as_newlines():
    seen = []
    script = (
        "import sys; sys.stdout.write("
        "'Receiving objects:  1%\\rReceiving objects: 50%\\r"
        "Receiving objects: 100%, done.\\nResolving deltas\\n')"
    )

    code, lines = git_ops._stream(
        _python(script), env=None, on_line=seen.append, is_cancelled=lambda: False
    )

    assert code == 0
    assert seen == [
        "Receiving objects:  1%",
        "Receiving objects: 50%",
        "Receiving objects: 100%, done.",
        "Resolving deltas",
    ]
    assert lines == seen


# ---- cleaning up after a cancel ------------------------------------------------------
def _read_only_tree(root):
    objects = root / ".git" / "objects" / "pack"
    objects.mkdir(parents=True)
    pack = objects / "pack-1.pack"
    pack.write_bytes(b"PACK")
    pack.chmod(stat.S_IREAD)
    (root / "top.txt").write_text("x", encoding="utf-8")
    (root / "top.txt").chmod(stat.S_IREAD)


def test_read_only_files_do_not_stop_the_clean_up(tmp_path):
    dest = tmp_path / "copy"
    _read_only_tree(dest)

    assert git_ops._remove_partial(dest, keep_folder=False) == ""
    assert not dest.exists()


def test_a_folder_that_existed_before_is_emptied_not_removed(tmp_path):
    dest = tmp_path / "copy"
    _read_only_tree(dest)

    assert git_ops._remove_partial(dest, keep_folder=True) == ""
    assert dest.is_dir() and not any(dest.iterdir())


@pytest.mark.skipif(sys.platform != "win32", reason="an open file blocks deletion")
def test_a_file_held_open_is_handed_back_as_left_behind(tmp_path, monkeypatch):
    monkeypatch.setattr(git_ops, "_REMOVE_FOR", 0.3)
    dest = tmp_path / "copy"
    dest.mkdir()
    held = (dest / "busy.txt").open("w", encoding="utf-8")
    try:
        assert git_ops._remove_partial(dest, keep_folder=False) == str(dest)
    finally:
        held.close()


def test_git_is_told_not_to_prompt_and_not_to_follow_another_repository(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "D:/elsewhere/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "D:/elsewhere")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")

    env = git_ops._clone_env()

    assert "GIT_DIR" not in env and "GIT_WORK_TREE" not in env
    assert env["GIT_TERMINAL_PROMPT"] == "0"


# ---- naming the folder ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("url", "name"),
    [
        ("https://github.com/ONEoo7/git_assistant.git", "git_assistant"),
        ("https://github.com/ONEoo7/git_assistant", "git_assistant"),
        ("https://github.com/ONEoo7/git_assistant/", "git_assistant"),
        ("git@github.com:ONEoo7/payments.git", "payments"),
        ("ssh://git@example.com:2222/team/payments.git", "payments"),
        ("file:///D:/work/payments", "payments"),
        ("D:\\work\\payments", "payments"),
        ("D:\\work\\payments\\.git", "payments"),
        ("  https://example.com/a/b.git  ", "b"),
        ("", ""),
    ],
)
def test_the_folder_is_named_the_way_git_would_name_it(url, name):
    assert git_ops.repo_name_from_url(url) == name


# ---- creating ------------------------------------------------------------------------
def test_a_new_repository_starts_on_the_branch_asked_for(tmp_path):
    target = tmp_path / "parents" / "that do not exist" / "project"

    result = git_ops.init(target, initial_branch="trunk")

    assert result.ok, result.stderr
    assert _out(target, "symbolic-ref", "HEAD") == "refs/heads/trunk"


def test_a_folder_that_is_already_a_repository_is_not_created_again(source):
    """Git would call it reinitializing and succeed, which reads as a new one."""
    result = git_ops.init(source, initial_branch="main")

    assert not result.ok
    assert "already a git repository" in result.stderr


def test_the_default_branch_is_read_from_global_then_system_config(monkeypatch):
    answers = {"--global": "", "--system": "master"}

    def run_global(args):
        value = answers[args[1]]
        return git_ops.GitResult(bool(value), value + "\n", "", 0 if value else 1)

    monkeypatch.setattr(git_ops, "_run_global", run_global)
    assert git_ops.default_branch_name() == "master"

    answers["--global"] = "main"
    assert git_ops.default_branch_name() == "main"

    answers.update({"--global": "", "--system": ""})
    assert git_ops.default_branch_name() == ""


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("main", True),
        ("feature/login", True),
        ("", False),
        ("-x", False),
        ("a..b", False),
        ("with space", False),
        ("HEAD", False),
    ],
)
def test_branch_names_are_checked_by_git_itself(name, valid):
    assert git_ops.valid_branch_name(name) is valid
