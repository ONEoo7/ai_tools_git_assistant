"""A diff cannot run a command through an agent CLI's command line -- shown with
real batch files and a real payload.

Windows starts a batch file through cmd.exe, and cmd.exe reads the arguments as a
command line of its own. Python escapes a quote in an argument as ``\\"``, which
cmd.exe does not understand, so a quote and an ``&`` in a prompt make the rest
of it a command -- and the prompt is a diff, which is whatever a repository
contains. The first test shows the payload working on a batch file; the others
show the client never letting a prompt near one.
"""

import json
import subprocess
import sys

import pytest

from git_assistant.agent_cli import detect, resolved
from git_assistant.agent_cli.client import CliClient, CliError, _folded

from conftest import npm_install

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="batch files and cmd.exe are Windows"
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: Plays the CLI: records what it was given, and answers in both CLIs' shapes.
RECORDER = """\
import json, sys
args = sys.argv[1:]
system = ""
if "--system-prompt-file" in args:
    with open(args[args.index("--system-prompt-file") + 1], "rb") as held:
        system = held.read().decode("utf-8")
stdin = sys.stdin.buffer.read().decode("utf-8")
with open(sys.argv[0] + ".record.json", "w", encoding="utf-8") as out:
    json.dump({"args": args, "stdin": stdin, "system": system}, out)
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False, "result": "feat: ok",
    "status": "SUCCESS", "response": "feat: ok", "usage": {},
}))
"""


@pytest.fixture(autouse=True)
def no_model_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(resolved, "user_config_dir", lambda *a, **k: str(tmp_path))


@pytest.fixture
def marker(tmp_path):
    """The file the payload creates if cmd.exe ever reads it as a command."""
    return tmp_path / "INJECTED"


def _payload(marker):
    return f'a line of a diff" & echo pwned>{marker} & rem "'


def _echo_shim(folder, name="claude"):
    shim = folder / f"{name}.cmd"
    shim.write_text("@echo off\r\necho %*\r\n", encoding="utf-8")
    return shim


def test_the_payload_runs_a_command_when_a_batch_file_is_handed_it(tmp_path, marker):
    """The premise, shown on this machine: what the client must never do."""
    shim = _echo_shim(tmp_path)

    subprocess.run(
        [str(shim), "-p", _payload(marker)],
        capture_output=True,
        creationflags=_NO_WINDOW,
    )

    assert marker.exists()


def test_a_batch_file_is_never_handed_a_prompt(tmp_path, marker, monkeypatch):
    shim = _echo_shim(tmp_path)
    monkeypatch.setattr(detect, "locate", lambda name: str(shim))
    payload = _payload(marker)

    with pytest.raises(CliError, match="batch file"):
        CliClient("claude").chat("sonnet", payload, payload, 64)

    assert not marker.exists()


@pytest.mark.parametrize("cli", ["claude", "agy"])
def test_an_npm_installed_cli_gets_the_prompt_whole_and_runs_nothing(
    tmp_path, marker, monkeypatch, cli
):
    """The shim is not run at all: node is started on the shim's script."""
    shim, script = npm_install(tmp_path / "npm", cli, RECORDER)
    monkeypatch.setattr(detect, "locate", lambda name: str(shim))
    # Python plays node, and runs the script that plays the CLI.
    monkeypatch.setattr(detect, "node_program", lambda: sys.executable)
    # Lines and a non-ASCII character too: what arrives must be what was sent.
    payload = f"+first line\n{_payload(marker)}\n-ünïcode line\n"

    reply = CliClient(cli).chat("sonnet", payload, payload, 64)

    record = json.loads(
        (script.parent / "cli.js.record.json").read_text(encoding="utf-8")
    )
    assert reply == "feat: ok"
    assert not marker.exists(), "the payload ran as a command"
    if cli == "claude":
        assert record["stdin"] == payload
        assert record["system"] == payload
        assert not any("diff" in arg for arg in record["args"])
    else:
        # agy reads no stdin, so its prompt is an argument -- one, and intact.
        assert _folded(payload, payload) in record["args"]
