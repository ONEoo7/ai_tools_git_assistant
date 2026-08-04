"""Registering with a client, and above all not damaging its config on the way."""

import json
import subprocess

import pytest

from git_assistant.mcp import SERVER_NAME, clients, launch

COMMAND = ["C:\\Program Files\\Git Assistant\\GitAssistantMcp.exe", "--mcp"]

#: A config shaped like the real one: a server entry is a small key in a file
#: that holds everything else the user has ever set there.
EXISTING = {
    "coworkUserFilesPath": "C:\\Users\\someone\\Claude",
    "preferences": {
        "menuBarEnabled": False,
        "remoteSessionFolderGrants": {"session_1": ["D:\\projects\\thing"]},
        "epitaxyPrefs": {"starred-cowork-spaces": [], "dframe-local-slice": {"pinnedOrder": []}},
    },
}


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(EXISTING, indent=2), encoding="utf-8")
    monkeypatch.setattr(clients, "desktop_config_path", lambda: path)
    return path


class Runner:
    """The claude CLI, recorded rather than run."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls: list[list[str]] = []
        self._result = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, argv):
        self.calls.append(argv)
        return self._result


# ---- Claude Desktop -------------------------------------------------------------
def test_registering_keeps_every_other_setting(config):
    clients.register_desktop(COMMAND)

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["coworkUserFilesPath"] == EXISTING["coworkUserFilesPath"]
    assert data["preferences"] == EXISTING["preferences"]
    assert data["mcpServers"][SERVER_NAME] == {
        "command": COMMAND[0],
        "args": ["--mcp"],
    }


def test_registering_twice_changes_nothing_the_second_time(config):
    clients.register_desktop(COMMAND)
    first = config.read_text(encoding="utf-8")
    clients.register_desktop(COMMAND)
    assert config.read_text(encoding="utf-8") == first


def test_removing_puts_the_document_back_as_it_was(config):
    original = config.read_text(encoding="utf-8")
    clients.register_desktop(COMMAND)

    clients.unregister_desktop()

    assert json.loads(config.read_text(encoding="utf-8")) == json.loads(original)


def test_a_backup_is_taken_once(config):
    clients.register_desktop(COMMAND)
    backup = config.with_name(config.name + ".git-assistant.bak")
    assert json.loads(backup.read_text(encoding="utf-8")) == EXISTING

    clients.register_desktop(["other.exe", "--mcp"])
    assert json.loads(backup.read_text(encoding="utf-8")) == EXISTING  # still the first


def test_a_file_that_will_not_parse_is_refused_not_overwritten(config):
    config.write_text("{ this is not json", encoding="utf-8")

    with pytest.raises(clients.ClientError, match="not valid JSON"):
        clients.register_desktop(COMMAND)

    assert config.read_text(encoding="utf-8") == "{ this is not json"


def test_a_missing_config_is_created(tmp_path, monkeypatch):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    monkeypatch.setattr(clients, "desktop_config_path", lambda: path)

    clients.register_desktop(COMMAND)

    assert json.loads(path.read_text(encoding="utf-8"))["mcpServers"][SERVER_NAME]


def test_the_write_flag_reaches_the_registered_command(config):
    clients.register_desktop([*COMMAND, "--allow-writes"])
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["mcpServers"][SERVER_NAME]["args"] == ["--mcp", "--allow-writes"]


def test_status_reports_what_is_registered(config):
    assert clients.desktop_status(COMMAND).present is False

    clients.register_desktop(COMMAND)
    status = clients.desktop_status(COMMAND)

    assert status.present is True
    assert "read-only" in status.describe(COMMAND)


def test_status_notices_a_command_that_no_longer_matches(config):
    clients.register_desktop(["C:\\old\\path.exe", "--mcp"])
    assert "different command" in clients.desktop_status(COMMAND).describe(COMMAND)


def test_removing_when_nothing_is_registered_says_so(config):
    assert "was not registered" in clients.unregister_desktop()


# ---- Claude Code -------------------------------------------------------------------
def test_the_add_command_puts_the_server_argv_after_the_separator():
    argv = clients.add_argv("claude.exe", COMMAND, "user")
    assert argv == [
        "claude.exe", "mcp", "add", SERVER_NAME, "--scope", "user",
        "--", COMMAND[0], "--mcp",
    ]
    # Everything after -- belongs to the server, which is what stops --mcp from
    # being read as a flag for the CLI.
    assert argv.index("--") < argv.index("--mcp")


def test_registering_replaces_rather_than_duplicating(monkeypatch):
    monkeypatch.setattr(clients, "claude_cli", lambda: "claude.exe")
    runner = Runner()

    clients.register_code(COMMAND, "user", runner=runner)

    assert runner.calls[0][:3] == ["claude.exe", "mcp", "remove"]
    assert runner.calls[1][:3] == ["claude.exe", "mcp", "add"]


def test_the_default_scope_is_every_project_not_just_this_one():
    assert clients.DEFAULT_SCOPE == "user"


def test_an_unknown_scope_is_refused(monkeypatch):
    monkeypatch.setattr(clients, "claude_cli", lambda: "claude.exe")
    with pytest.raises(clients.ClientError, match="scope must be"):
        clients.register_code(COMMAND, "everywhere", runner=Runner())


def test_a_failing_cli_is_reported_with_its_own_words(monkeypatch):
    monkeypatch.setattr(clients, "claude_cli", lambda: "claude.exe")
    runner = Runner(returncode=1, stderr="already exists")

    with pytest.raises(clients.ClientError, match="already exists"):
        clients.register_code(COMMAND, "user", runner=runner)


def test_no_cli_says_what_to_do_instead(monkeypatch):
    monkeypatch.setattr(clients, "claude_cli", lambda: None)
    with pytest.raises(clients.ClientError, match="copy the command"):
        clients.register_code(COMMAND)


# ---- the snippet, for anything else ---------------------------------------------------
def test_the_snippet_is_the_shape_every_client_reads():
    data = json.loads(clients.snippet(COMMAND))
    assert data["mcpServers"][SERVER_NAME]["command"] == COMMAND[0]
    assert data["mcpServers"][SERVER_NAME]["args"] == ["--mcp"]


# ---- the command line ------------------------------------------------------------------
def test_a_source_checkout_runs_the_module(monkeypatch):
    monkeypatch.setattr(launch.sys, "frozen", False, raising=False)
    command = launch.server_command()
    assert command[1:] == ["-m", "git_assistant", "--mcp"]


def test_the_write_flag_is_only_there_when_asked(monkeypatch):
    monkeypatch.setattr(launch.sys, "frozen", False, raising=False)
    assert "--allow-writes" not in launch.server_command()
    assert "--allow-writes" in launch.server_command(allow_writes=True)


def test_a_frozen_build_uses_the_executable_beside_it(tmp_path, monkeypatch):
    (tmp_path / "GitAssistantMcp.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(launch.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launch.sys, "executable", str(tmp_path / "GitAssistant.exe"))

    command = launch.server_command()

    assert command[0].endswith("GitAssistantMcp.exe")
    assert command[1] == "--mcp"


def test_a_frozen_build_without_a_companion_admits_it(tmp_path, monkeypatch):
    """Better than registering a command that fails silently inside a client."""
    monkeypatch.setattr(launch.sys, "frozen", True, raising=False)
    monkeypatch.setattr(launch.sys, "executable", str(tmp_path / "GitAssistant.exe"))
    assert launch.server_command() is None


def test_a_path_with_spaces_is_quoted_for_display():
    shown = launch.display(COMMAND)
    assert shown.startswith('"C:\\Program Files')
    assert shown.endswith("--mcp")
