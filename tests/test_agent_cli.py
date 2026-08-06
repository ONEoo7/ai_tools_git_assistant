"""Agent CLIs as backends. No real process is ever spawned here.

The recipes are pinned argument-by-argument, because the flags that keep these
programs from touching a repository are the whole safety case: `--tools ""` for
claude, an empty working directory for both. A flag dropped in a refactor would
not fail anything at run time -- it would quietly hand an agent the user's code.
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from git_assistant import agent_cli, llm, providers, usage
from git_assistant.agent_cli import client as client_mod
from git_assistant.agent_cli import detect, resolved
from git_assistant.agent_cli.client import CliClient, CliError
from git_assistant.config import Settings

CLAUDE_JSON = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "feat: add the thing",
        "total_cost_usd": 0.000396,
        "usage": {
            "input_tokens": 112,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 8,
            "output_tokens": 4,
        },
        "modelUsage": {"claude-sonnet-4-6": {"contextWindow": 200000}},
    }
)
AGY_JSON = json.dumps(
    {
        "conversation_id": "abc",
        "status": "SUCCESS",
        "response": "feat: add the thing\n",
        "usage": {"input_tokens": 17087, "output_tokens": 5, "total_tokens": 17092},
    }
)


@pytest.fixture(autouse=True)
def no_usage_file(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "user_config_dir", lambda *a, **k: str(tmp_path))
    monkeypatch.setattr(resolved, "user_config_dir", lambda *a, **k: str(tmp_path))


@pytest.fixture
def installed(monkeypatch):
    """Both CLIs present, at a path nothing will try to run."""
    monkeypatch.setattr(detect, "locate", lambda name: f"C:/fake/{name}.exe")
    return "C:/fake"


class FakeProcess:
    """Stands in for Popen. Records the call; never starts anything."""

    last: dict = {}

    def __init__(self, args, **kwargs):
        FakeProcess.last = {"args": list(args), **kwargs}
        self.returncode = FakeProcess.code

    def communicate(self, timeout=None):
        return FakeProcess.stdout, FakeProcess.stderr

    def kill(self):
        FakeProcess.last["killed"] = True


def _spawn(monkeypatch, stdout="", stderr="", code=0):
    FakeProcess.stdout, FakeProcess.stderr, FakeProcess.code = stdout, stderr, code
    FakeProcess.last = {}
    monkeypatch.setattr(subprocess, "Popen", FakeProcess)
    return FakeProcess


# ---- the provider list ----------------------------------------------------------
def test_both_clis_are_offered():
    keys = [p.key for p in providers.PROVIDERS]
    assert "claude-cli" in keys and "agy-cli" in keys


def test_both_are_flagged_experimental():
    for key in ("claude-cli", "agy-cli"):
        assert providers.get(key).experimental
        assert providers.get(key).display().endswith("(experimental)")


def test_an_ordinary_provider_is_not_flagged():
    assert providers.get("lmstudio").display() == "LM Studio"


def test_a_cli_provider_asks_for_no_key_and_no_address():
    """It has its own login and no endpoint; offering fields would be a lie."""
    for key in ("claude-cli", "agy-cli"):
        provider = providers.get(key)
        assert not provider.needs_api_key and not provider.needs_endpoint


def test_what_experimental_costs_is_said_not_implied():
    assert "17,000 tokens" in providers.get("agy-cli").key_help
    assert "five seconds" in providers.get("claude-cli").key_help


def test_build_client_returns_a_cli_client(monkeypatch):
    s = Settings()
    s.save = lambda: None
    s.provider = "claude-cli"
    built = llm.build_client(s, feature="Commit message")
    assert isinstance(built, CliClient)


# ---- the flags that keep an agent away from the repository -------------------------
def test_claude_is_asked_with_no_tools_at_all(installed, monkeypatch):
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    CliClient("claude").chat("sonnet", "be brief", "the diff", 512)

    args = FakeProcess.last["args"]
    assert args[args.index("--tools") + 1] == "", "an agent with tools, in a repo"


def test_claude_always_replaces_its_own_system_prompt(installed, monkeypatch):
    """Measured: 3,408 cached tokens with the default, 112 with this."""
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    CliClient("claude").chat("sonnet", "be brief", "the diff", 512)

    args = FakeProcess.last["args"]
    assert args[args.index("--system-prompt") + 1] == "be brief"


def test_claude_ignores_the_user_s_own_settings_and_project_files(
    installed, monkeypatch
):
    """A CLAUDE.md redirecting the prompt would be invisible from here."""
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    CliClient("claude").chat("sonnet", "s", "u", 512)

    args = FakeProcess.last["args"]
    assert args[args.index("--setting-sources") + 1] == ""
    assert "--disable-slash-commands" in args


def test_every_call_runs_in_an_empty_directory_not_the_repository(
    installed, monkeypatch
):
    """`agy` cannot be told to leave a workspace alone; this is the only fence."""
    _spawn(monkeypatch, stdout=AGY_JSON)
    CliClient("agy").chat("gemini-3.6-flash-low", "s", "u", 512)

    where = Path(FakeProcess.last["cwd"])
    assert where.name.startswith("git-assistant-cli-")
    assert not any(where.iterdir()) if where.exists() else True


def test_a_directory_the_cli_still_holds_does_not_lose_the_answer(
    installed, monkeypatch
):
    """Observed live: WinError 32 from __exit__, after a good reply was read."""
    real = tempfile.TemporaryDirectory
    seen = {}

    def watched(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(tempfile, "TemporaryDirectory", watched)
    _spawn(monkeypatch, stdout=CLAUDE_JSON)

    assert CliClient("claude").chat("sonnet", "s", "u", 512) == "feat: add the thing"
    assert seen.get("ignore_cleanup_errors") is True


def test_nothing_is_read_from_stdin(installed, monkeypatch):
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    CliClient("claude").chat("sonnet", "s", "u", 512)
    assert FakeProcess.last["stdin"] is subprocess.DEVNULL


# ---- reading the answers ------------------------------------------------------------
def test_claude_s_reply_and_its_tokens_come_back(installed, monkeypatch):
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    reply = CliClient("claude", provider_key="claude-cli").chat("sonnet", "s", "u", 512)

    assert reply == "feat: add the thing"
    event = usage.last_event()
    # 112 + 20 cache-creation + 8 cache-read: all of it was paid for.
    assert (event.input_tokens, event.output_tokens) == (140, 4)
    assert event.provider == "claude-cli"


def test_agy_s_reply_and_its_tokens_come_back(installed, monkeypatch):
    _spawn(monkeypatch, stdout=AGY_JSON)
    reply = CliClient("agy", provider_key="agy-cli").chat("gemini", "s", "u", 512)

    assert reply == "feat: add the thing"
    assert usage.last_event().input_tokens == 17087


def test_a_line_of_noise_before_the_json_is_tolerated(installed, monkeypatch):
    _spawn(monkeypatch, stdout="warning: something\n" + CLAUDE_JSON)
    assert CliClient("claude").chat("sonnet", "s", "u", 512) == "feat: add the thing"


def test_an_error_result_is_reported_not_returned(installed, monkeypatch):
    _spawn(monkeypatch, stdout=json.dumps({"is_error": True, "result": "rate limited"}))
    with pytest.raises(CliError, match="rate limited"):
        CliClient("claude").chat("sonnet", "s", "u", 512)


def test_an_empty_reply_is_an_error_rather_than_an_empty_message(
    installed, monkeypatch
):
    """An agent that decided to act instead of answering must not read as clean."""
    _spawn(monkeypatch, stdout=json.dumps({"subtype": "success", "result": ""}))
    with pytest.raises(CliError, match="no text"):
        CliClient("claude").chat("sonnet", "s", "u", 512)


def test_printing_nothing_at_all_says_so(installed, monkeypatch):
    """Exactly Copilot's failure: exit 1, nothing on either stream."""
    _spawn(monkeypatch, stdout="", stderr="", code=1)
    with pytest.raises(CliError, match="printed nothing"):
        CliClient("claude").chat("sonnet", "s", "u", 512)


def test_unreadable_json_is_refused_rather_than_guessed(installed, monkeypatch):
    _spawn(monkeypatch, stdout="{not json at all")
    with pytest.raises(CliError, match="cannot read"):
        CliClient("claude").chat("sonnet", "s", "u", 512)


def test_a_cli_that_is_not_installed_says_where_to_get_it(monkeypatch):
    monkeypatch.setattr(detect, "locate", lambda name: "")
    with pytest.raises(CliError, match="Connection & Model"):
        CliClient("claude").chat("sonnet", "s", "u", 512)


# ---- the prompt a CLI with no system prompt gets ------------------------------------
def test_agy_gets_the_two_halves_folded_and_labelled(installed, monkeypatch):
    """A bare join would leave the model guessing which half is the instruction."""
    _spawn(monkeypatch, stdout=AGY_JSON)
    CliClient("agy").chat("gemini", "You are terse.", "the diff", 512)

    prompt = FakeProcess.last["args"][2]
    assert prompt.startswith("You are terse.")
    assert "---" in prompt and prompt.endswith("the diff")


def test_claude_keeps_them_apart(installed, monkeypatch):
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    CliClient("claude").chat("sonnet", "You are terse.", "the diff", 512)
    assert FakeProcess.last["args"][2] == "the diff"


# ---- models and context --------------------------------------------------------------
def test_agy_lists_its_models_offline(installed, monkeypatch):
    _spawn(monkeypatch, stdout="gemini-3.6-flash-low\nclaude-sonnet-4-6\n")
    listed = CliClient("agy").list_models()

    assert [m.id for m in listed] == ["gemini-3.6-flash-low", "claude-sonnet-4-6"]
    assert FakeProcess.last["args"][1] == "models"


# ---- saying which model an alias actually is ------------------------------------------
def test_the_alias_is_what_gets_sent(installed, monkeypatch):
    """It tracks whichever model is current; pinning today's would go stale."""
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    CliClient("claude").chat("sonnet", "s", "u", 512)

    args = FakeProcess.last["args"]
    assert args[args.index("--model") + 1] == "sonnet"


def test_the_model_that_answered_is_what_the_usage_table_names(installed, monkeypatch):
    """"sonnet" is all anyone would otherwise see of a month's spending."""
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    CliClient("claude", provider_key="claude-cli").chat("sonnet", "s", "u", 512)

    assert usage.last_event().model == "claude-sonnet-4-6"


def test_the_resolution_is_remembered_for_next_time(installed, monkeypatch):
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    CliClient("claude").chat("sonnet", "s", "u", 512)

    assert resolved.get("claude", "sonnet") == "claude-sonnet-4-6"


def test_the_model_list_says_what_last_answered_not_what_the_alias_is(
    installed, monkeypatch
):
    """Measured: the same alias was served by sonnet once and haiku once. An
    unqualified "sonnet (claude-haiku-4-5)" would read as an equivalence."""
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    CliClient("claude").chat("sonnet", "s", "u", 512)

    listed = {m.id: m.label() for m in CliClient("claude").list_models()}

    assert listed["sonnet"] == "sonnet  (last: claude-sonnet-4-6)"
    assert listed["opus"] == "opus", "not yet used, so nothing is claimed about it"


def test_a_later_call_replaces_what_is_remembered(installed, monkeypatch):
    """The routing can change between calls; the label follows the last one."""
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    CliClient("claude").chat("sonnet", "s", "u", 512)
    _spawn(
        monkeypatch,
        stdout=CLAUDE_JSON.replace("claude-sonnet-4-6", "claude-haiku-4-5-20251001"),
    )
    CliClient("claude").chat("sonnet", "s", "u", 512)

    assert resolved.get("claude", "sonnet") == "claude-haiku-4-5-20251001"


def test_an_alias_nobody_has_used_is_not_guessed_at(installed, monkeypatch):
    listed = {m.id: m.label() for m in CliClient("claude").list_models()}
    assert listed == {"sonnet": "sonnet", "opus": "opus", "haiku": "haiku"}


def test_a_cache_that_cannot_be_written_does_not_fail_the_call(
    installed, monkeypatch
):
    monkeypatch.setattr(
        resolved, "_write", lambda known: (_ for _ in ()).throw(OSError("read only"))
    )
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    assert CliClient("claude").chat("sonnet", "s", "u", 512) == "feat: add the thing"


def test_a_corrupt_cache_reads_as_empty(monkeypatch, tmp_path):
    (tmp_path / resolved.CACHE_FILE).write_text("{not json", encoding="utf-8")
    assert resolved.load() == {}


def test_agy_names_its_model_in_full_already(installed, monkeypatch):
    """Asked by full id, so there is nothing to resolve and nothing is invented."""
    _spawn(monkeypatch, stdout=AGY_JSON)
    CliClient("agy", provider_key="agy-cli").chat("gemini-3.6-flash-low", "s", "u", 512)

    assert usage.last_event().model == "gemini-3.6-flash-low"
    assert resolved.load().get("agy", {}) == {}


def test_claude_offers_its_aliases_without_spending_anything(installed, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("listing claude's models must not run it")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert [m.id for m in CliClient("claude").list_models()] == [
        "sonnet",
        "opus",
        "haiku",
    ]


def test_agy_s_window_is_reported_minus_what_it_spends_on_itself():
    """A budget that ignores the 17k plans a prompt that will not fit."""
    agy = CliClient("agy").context_length_for("gemini")
    claude = CliClient("claude").context_length_for("sonnet")
    assert agy < claude
    assert claude - agy >= 17_000


# ---- the environment a CLI is given ---------------------------------------------------
def test_the_nested_session_guard_is_removed(monkeypatch):
    """claude refuses to start inside another claude session."""
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    env = detect.child_env()
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env


def test_the_child_gets_the_live_path(monkeypatch):
    monkeypatch.setattr(detect, "registry_path", lambda: "C:/from/registry")
    assert "C:/from/registry" in detect.child_env()["PATH"]


def test_the_search_path_survives_a_registry_that_will_not_answer(monkeypatch):
    monkeypatch.setattr(detect, "registry_path", lambda: "")
    assert detect.search_path() == os.environ.get("PATH", "")


def test_a_known_location_is_found_when_path_does_not_have_it(monkeypatch, tmp_path):
    """What makes an install detectable in the session that performed it."""
    monkeypatch.setattr(detect.shutil, "which", lambda *a, **k: None)
    monkeypatch.setattr(detect.Path, "home", staticmethod(lambda: tmp_path))
    where = tmp_path / "AppData/Local/agy/bin"
    where.mkdir(parents=True)
    (where / "agy.exe").write_text("", encoding="utf-8")

    assert detect.locate("agy") == str(where / "agy.exe")


# ---- installing ------------------------------------------------------------------------
def test_the_documented_installers_are_what_runs():
    assert agent_cli.install_command("claude") == "irm https://claude.ai/install.ps1 | iex"
    assert (
        agent_cli.install_command("agy")
        == "irm https://antigravity.google/cli/install.ps1 | iex"
    )


def test_nothing_is_installed_for_a_cli_we_do_not_ship():
    assert agent_cli.install_command("copilot") == ""
    assert detect.install("copilot").problem


def test_after_installing_it_is_looked_for_again(monkeypatch):
    """The whole point: this process's PATH cannot have changed."""
    monkeypatch.setattr(detect.sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
    )
    looked = []
    monkeypatch.setattr(
        detect, "probe", lambda name: looked.append(name) or detect.Found(name, "C:/x")
    )

    found = detect.install("claude")

    assert looked == ["claude"] and found.installed


def test_an_installer_that_leaves_nothing_behind_says_so(monkeypatch):
    monkeypatch.setattr(detect.sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "access denied"),
    )
    monkeypatch.setattr(detect, "probe", lambda name: detect.Found(name))

    found = detect.install("claude")

    assert not found.installed and "still not where it was expected" in found.problem


def test_installing_never_raises(monkeypatch):
    monkeypatch.setattr(detect.sys, "platform", "win32")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("no shell"))
    )
    assert "could not be run" in detect.install("claude").problem


# ---- one call at a time -----------------------------------------------------------------
def test_a_cli_provider_is_capped_to_one_call_at_a_time():
    """Four processes starting at once buys nothing: the seconds are start-up."""
    from git_assistant.parallel import effective_parallel

    s = Settings()
    s.parallel_calls = 8
    for key in ("claude-cli", "agy-cli"):
        s.provider = key
        assert providers.get(key).max_parallel == 1
        assert effective_parallel(s, 200_000) == 1


def test_an_ordinary_provider_still_fans_out():
    from git_assistant.parallel import effective_parallel

    s = Settings()
    s.parallel_calls = 8
    s.provider = "lmstudio"
    assert effective_parallel(s, 200_000) == 8


def test_the_cap_cannot_be_raised_from_settings():
    """It is a property of the backend, not a preference."""
    from git_assistant.parallel import effective_parallel

    s = Settings()
    s.provider = "agy-cli"
    s.parallel_calls = 64
    assert effective_parallel(s, 1_000_000) == 1


def test_concurrent_calls_do_not_take_each_other_s_process(installed, monkeypatch):
    """Reproduced live with four threads: one thread's `finally` cleared the
    handle another was still reading, and the run died on an AttributeError."""
    import threading

    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    shared = CliClient("claude")
    failures: list = []

    def one():
        try:
            shared.chat("sonnet", "s", "u", 512)
        except Exception as exc:  # noqa: BLE001 - the point is that none escape
            failures.append(exc)

    threads = [threading.Thread(target=one) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert failures == []


def test_cancel_stops_everything_in_flight(installed, monkeypatch):
    _spawn(monkeypatch, stdout=CLAUDE_JSON)
    client = CliClient("claude")
    first, second = FakeProcess([], stdin=None), FakeProcess([], stdin=None)
    client._running = {first, second}

    client.cancel()

    assert FakeProcess.last.get("killed") is True
    assert client._cancelled


# ---- no temperature to give -----------------------------------------------------------
def test_a_cli_client_reports_no_temperature():
    """Neither CLI accepts one; recording a number nobody used would be a lie."""
    assert CliClient("claude").temperature is None
    assert client_mod.CliClient("agy").temperature is None
