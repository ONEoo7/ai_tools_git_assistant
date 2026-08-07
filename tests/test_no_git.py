"""What happens on a machine with no git at all.

This is the 0.3.16 winget crash, found by installing that build in Windows
Sandbox and reading the start-up log it now writes:

    File "git_assistant\\identities.py", line 172, in _from_git
    File "git_assistant\\git_ops.py", line 322, in _run_global
    FileNotFoundError: [WinError 2] The system cannot find the file specified

There is no git on a clean Windows. Every git call in the application went
through two unguarded ``subprocess.run`` calls, so the first one raised -- and
it raised inside a Qt slot, which PyQt turns into ``qFatal()``. That is a
process abort reported as ``Qt6Core.dll`` / ``c0000409``, with the actual cause
nowhere in it.

The rule these tests hold: **a missing git is a failed GitResult, never an
exception.** `GitResult` already carries failure, so every caller's existing
``if not res.ok`` handles it without one of them being touched.
"""

import subprocess

import pytest

from git_assistant import git_ops
from git_assistant.agents import gitstream


@pytest.fixture
def no_git(monkeypatch):
    """Exactly what Windows raises when git.exe is not on PATH."""

    def missing(*args, **kwargs):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(subprocess, "run", missing)
    monkeypatch.setattr(subprocess, "Popen", missing)


# ---- the runners -------------------------------------------------------------
def test_a_repository_command_fails_rather_than_raising(no_git):
    result = git_ops._run("/x/repo", ["status"])

    assert not result.ok
    assert result.returncode == git_ops.NOT_INSTALLED
    assert git_ops.GIT_MISSING in result.stderr


def test_a_global_command_fails_rather_than_raising(no_git):
    """The one that actually fired: the identity bootstrap, before any window."""
    result = git_ops._run_global(["config", "--global", "user.email"])

    assert not result.ok
    assert git_ops.GIT_MISSING in result.stderr


def test_the_streaming_runner_raises_the_error_agents_already_handle(no_git):
    with pytest.raises(git_ops.GitError, match="not installed"):
        gitstream._popen("/x/repo", ["cat-file", "--batch-all-objects"])


def test_the_message_names_what_to_do_about_it():
    """It is shown to the person who has to fix it."""
    assert "not installed" in git_ops.GIT_MISSING
    assert "PATH" in git_ops.GIT_MISSING


# ---- what the callers see ------------------------------------------------------
def test_the_identity_bootstrap_survives_it(no_git, tmp_path, monkeypatch):
    """The exact path in the traceback that killed 0.3.16:
    settings window -> IdentityStore.bootstrap -> _from_git -> _run_global."""
    from git_assistant import identities

    monkeypatch.setattr(
        identities, "identities_path", lambda: tmp_path / "committer_identities.json"
    )

    store = identities.IdentityStore.bootstrap()

    assert store.identities == [], "no git, so nothing to import from it"


def test_reading_an_identity_returns_nothing_rather_than_exploding(no_git):
    name, email = git_ops.get_global_identity()
    assert (name, email) == ("", "")


def test_listing_branches_is_empty_rather_than_fatal(no_git):
    assert git_ops.list_branches("/x/repo") == []


def test_asking_for_a_diff_reports_the_failure(no_git):
    """Callers that raise GitError on failure still do; they just do not crash."""
    try:
        git_ops.get_diff("/x/repo", "cached")
    except git_ops.GitError as exc:
        assert "not installed" in str(exc)


# ---- is there a git at all -------------------------------------------------------
def test_it_can_be_asked_directly(no_git):
    assert git_ops.git_available() is False


def test_on_this_machine_there_is_one():
    """A machine running these tests has git; this is the control."""
    assert git_ops.git_available() is True


def test_a_git_that_errors_is_still_a_git(monkeypatch):
    """Only "cannot start it" means absent. A non-zero exit is git answering."""
    monkeypatch.setattr(
        git_ops,
        "_run_global",
        lambda args: git_ops.GitResult(ok=False, stdout="", stderr="nope", returncode=1),
    )
    assert git_ops.git_available() is True


# ---- and the application says so ---------------------------------------------------
def test_start_up_refuses_with_a_sentence_rather_than_a_blank_window():
    """Without git this application is not degraded, it is inert -- every tab
    would show nothing, with nothing saying why."""
    from pathlib import Path

    source = Path(git_ops.__file__).with_name("app.py").read_text(encoding="utf-8")
    body = source[source.index("def main("):]

    assert "git_ops.git_available()" in body
    assert "git-scm.com" in body, "it says where to get it"
    # Before the tray is built: nothing should come up and then die.
    assert body.index("git_available()") < body.index("TrayApp(app)")
