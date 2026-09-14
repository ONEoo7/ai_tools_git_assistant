"""Ending a process together with the processes it started -- with real ones.

What this application starts is often a launcher: the git on PATH runs the real
git, a CLI installed with npm is a .cmd shim that runs node, and a virtual
environment's python.exe runs the base interpreter. Each test builds that shape
out of Python -- something that starts a child, which writes a heartbeat to a
file and nothing to stdout -- and checks that the heartbeat stops.
"""

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from git_assistant import processes
from git_assistant.agent_cli import detect
from git_assistant.agent_cli.client import CliClient, CliError
from git_assistant.agents import gitstream

from conftest import npm_install

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="a .cmd shim is how npm installs a CLI on Windows"
)

#: Writes its pid, then a dot every 50 ms, for a minute at most: a test that fails
#: must not leave it running for good.
BEAT = (
    "import os, sys, time\n"
    "with open(sys.argv[1] + '.pid', 'w') as f:\n"
    "    f.write(str(os.getpid()))\n"
    "end = time.monotonic() + 60\n"
    "while time.monotonic() < end:\n"
    "    with open(sys.argv[1], 'a') as f:\n"
    "        f.write('.')\n"
    "    time.sleep(0.05)\n"
)


@pytest.fixture
def heartbeat(tmp_path):
    """The file the child beats into; any child still beating is ended after."""
    beat = tmp_path / "heartbeat"
    (tmp_path / "beat.py").write_text(BEAT, encoding="utf-8")
    yield beat
    pid_file = tmp_path / "heartbeat.pid"
    if pid_file.exists():
        try:
            os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGTERM)
        except (OSError, ValueError):
            pass


def _launcher(beat):
    """A parent that starts the child doing the work, and then just waits."""
    parent = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        "time.sleep(60)\n"
    )
    return [sys.executable, "-c", parent, str(beat.parent / "beat.py"), str(beat)]


def _npm_cli(beat, monkeypatch):
    """A CLI as npm installs one, whose script starts a child and waits.

    The shim itself is never run (see detect.command): node is started on its
    script, and Python plays node.
    """
    script_text = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(beat.parent / 'beat.py')!r}, "
        f"{str(beat)!r}])\n"
        "time.sleep(60)\n"
    )
    shim, _script = npm_install(beat.parent / "npm", "claude", script_text)
    monkeypatch.setattr(detect, "locate", lambda name: str(shim))
    monkeypatch.setattr(detect, "node_program", lambda: sys.executable)


def _beating(beat, timeout=20.0):
    deadline = time.monotonic() + timeout
    while not (beat.exists() and beat.stat().st_size > 2):
        assert time.monotonic() < deadline, "the heartbeat never started"
        time.sleep(0.05)


def _stopped(beat) -> bool:
    assert beat.exists(), "the heartbeat never started"
    time.sleep(0.5)  # a write already under way may still land
    size = beat.stat().st_size
    time.sleep(0.6)
    return beat.stat().st_size == size


# ---- the helper ----------------------------------------------------------------------
def test_killing_a_process_ends_everything_it_started(heartbeat):
    proc = subprocess.Popen(_launcher(heartbeat), **processes.killable())
    _beating(heartbeat)

    processes.kill_tree(proc)

    assert _stopped(heartbeat), "the child is still running"


def test_killing_only_the_process_started_leaves_its_child_running(heartbeat):
    """The premise, checked: this is what every caller used to do."""
    proc = subprocess.Popen(_launcher(heartbeat), **processes.killable())
    _beating(heartbeat)

    proc.kill()

    assert not _stopped(heartbeat)


def test_a_process_that_has_exited_is_not_touched(monkeypatch):
    """Its id may belong to somebody else by now; a tree walked from it would too."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"], **processes.killable())
    proc.wait()

    def started(*args, **kwargs):
        raise AssertionError("taskkill was run for a process that had exited")

    monkeypatch.setattr(subprocess, "run", started)
    processes.kill_tree(proc)


# ---- an agent CLI installed with npm -------------------------------------------------
def _ask(client, raised):
    try:
        client.chat("sonnet", "the system prompt", "the diff", 64)
    except CliError as exc:
        raised.append(str(exc))


@WINDOWS_ONLY
def test_cancelling_an_npm_installed_cli_ends_it_and_comes_back(heartbeat, monkeypatch):
    """Killing the process started alone left its child running with the output
    pipe open, so the call went on waiting and Cancel never returned."""
    _npm_cli(heartbeat, monkeypatch)
    client = CliClient("claude")
    raised: list[str] = []
    call = threading.Thread(target=_ask, args=(client, raised), daemon=True)
    call.start()
    _beating(heartbeat)

    client.cancel()
    call.join(timeout=20)

    assert not call.is_alive(), "the call did not come back after cancel()"
    assert raised == ["Cancelled."]
    assert _stopped(heartbeat), "the CLI is still running"


@WINDOWS_ONLY
def test_an_npm_installed_cli_that_runs_out_of_time_is_ended(heartbeat, monkeypatch):
    _npm_cli(heartbeat, monkeypatch)
    client = CliClient("claude", timeout=5.0)

    with pytest.raises(CliError, match="did not answer within"):
        client.chat("sonnet", "the system prompt", "the diff", 64)

    assert _stopped(heartbeat), "the CLI is still running"


# ---- a git stream left early ---------------------------------------------------------
def test_a_stream_left_early_does_not_leave_the_real_git_working(heartbeat):
    """A child that is not writing never hits the closed pipe, so closing it is
    not enough on its own: the git on PATH is a launcher too."""
    proc = subprocess.Popen(
        _launcher(heartbeat),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        **processes.killable(),
    )
    _beating(heartbeat)
    started = time.monotonic()

    gitstream._kill(proc)

    assert time.monotonic() - started < 10
    assert _stopped(heartbeat), "the child is still running"


def test_a_stream_read_to_the_end_is_not_killed(tmp_path, monkeypatch):
    """A git that has finished only needs its pipes closed; nothing is started
    to end it."""
    repo = tmp_path / "repo"
    for args in (
        ["init", "-q", str(repo)],
        ["-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@e.c",
         "commit", "-q", "--allow-empty", "-m", "one"],
    ):
        subprocess.run(["git", *args], check=True, capture_output=True,
                       creationflags=_NO_WINDOW)
    ended = []
    monkeypatch.setattr(processes, "kill_tree", ended.append)

    with gitstream.streamed(repo, ["log", "--format=%s"]) as lines:
        assert list(lines) == ["one"]

    assert ended == []
