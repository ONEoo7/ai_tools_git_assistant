"""The tools themselves, against real repositories."""

import re
import subprocess
import sys

import pytest

from git_assistant.config import RepoEntry, Settings
from git_assistant.mcp import schema, tools
from git_assistant.mcp.context import ToolContext, ToolError

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
NAME_RULE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, creationflags=_NO_WINDOW, check=False,
    )


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@e.example")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-m", "first")
    return tmp_path


@pytest.fixture
def ctx(repo, monkeypatch):
    settings = Settings(repos=[RepoEntry(str(repo), label="demo")], active_repo=str(repo))
    context = ToolContext()
    monkeypatch.setattr(context, "settings", lambda: Settings.from_dict(settings.to_dict()))
    monkeypatch.setattr(context.settings_cache, "current", lambda: settings)
    return context


def call(tool_name, ctx, /, **arguments):
    # Positional-only: several tools take an argument called `name`.
    tool = tools.find(tool_name, allow_writes=True)
    assert tool is not None, tool_name
    return tools.run(tool, arguments, ctx, is_cancelled=lambda: False, progress=lambda _m: None)


def body(result) -> str:
    return result["content"][0]["text"]


# ---- the catalogue is well formed ------------------------------------------------
def test_every_tool_name_is_legal():
    for tool in tools.TOOLS:
        assert NAME_RULE.match(tool.name), tool.name


def test_every_tool_declares_an_object_input_schema():
    for definition in tools.catalogue(allow_writes=True):
        assert definition["inputSchema"]["type"] == "object"
        assert definition["description"]
        assert definition["title"]


def test_read_only_and_destructive_hints_agree_with_the_gate():
    for tool in tools.TOOLS:
        hints = tool.definition()["annotations"]
        assert hints["readOnlyHint"] is not tool.writes
        assert hints["destructiveHint"] is tool.writes


def test_tool_names_are_unique():
    names = [t.name for t in tools.TOOLS]
    assert len(names) == len(set(names))


# ---- reading ----------------------------------------------------------------------
def test_list_repos_reports_the_configured_ones(ctx, repo):
    result = call("list_repos", ctx)
    assert str(repo) in body(result)
    assert result["structuredContent"]["repositories"][0]["active"] is True


def test_repo_status_describes_the_branch_and_what_is_staged(ctx, repo):
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    _git(repo, "add", "b.txt")

    result = call("repo_status", ctx)

    assert "b.txt" in result["structuredContent"]["staged"]
    assert result["structuredContent"]["branch"]


def test_get_diff_marks_where_it_truncated(ctx, repo):
    (repo / "big.txt").write_text("x" * 50_000, encoding="utf-8")
    _git(repo, "add", "big.txt")

    result = call("get_diff", ctx, mode="cached", max_bytes=1000)

    assert "[truncated:" in body(result)
    assert len(body(result)) < 1200


def test_get_diff_says_so_when_there_is_nothing(ctx):
    assert "Nothing to show" in body(call("get_diff", ctx, mode="cached"))


def test_list_branches_marks_the_current_one(ctx, repo):
    _git(repo, "branch", "feature")
    result = call("list_branches", ctx)
    assert set(result["structuredContent"]["branches"]) >= {"feature"}
    assert result["structuredContent"]["current"] in body(result)


def test_list_tags_is_empty_without_tags(ctx):
    assert call("list_tags", ctx)["structuredContent"]["tags"] == []


# ---- which repository ---------------------------------------------------------------
def test_a_repository_can_be_named_by_label(ctx, repo):
    assert str(repo) in body(call("repo_status", ctx, repo="demo"))


def test_a_repository_can_be_named_by_its_folder(ctx, repo):
    assert str(repo) in body(call("repo_status", ctx, repo=repo.name))


def test_an_unconfigured_path_is_refused_with_the_choices(ctx):
    result = call("repo_status", ctx, repo="D:\\somewhere\\else")
    assert result["isError"] is True
    assert "not a configured repository" in body(result)


def test_no_repositories_at_all_says_where_to_add_one(monkeypatch):
    context = ToolContext()
    monkeypatch.setattr(context, "settings", lambda: Settings())
    monkeypatch.setattr(context.settings_cache, "current", Settings)
    result = call("list_repos", context)
    assert result["isError"] is True
    assert "Repositories tab" in body(result)


# ---- the gate -------------------------------------------------------------------------
def test_write_tools_are_not_in_the_read_only_catalogue():
    names = [t["name"] for t in tools.catalogue(allow_writes=False)]
    assert not ({"commit", "push", "create_tag", "push_tag", "switch_branch"} & set(names))


def test_the_gate_message_says_what_to_do():
    assert "MCP Server tab" in tools.explain_missing("push")
    assert "unknown tool" in tools.explain_missing("nonsense")


# ---- writing (only reachable with the flag) ---------------------------------------------
def test_commit_refuses_when_nothing_is_staged(ctx):
    result = call("commit", ctx, message="nothing here")
    assert result["isError"] is True
    assert "nothing to commit" in body(result)


def test_commit_commits_what_is_staged(ctx, repo):
    (repo / "c.txt").write_text("three\n", encoding="utf-8")
    _git(repo, "add", "c.txt")

    result = call("commit", ctx, message="feat: add c")

    assert result["isError"] is False
    assert _git(repo, "log", "-1", "--pretty=%s").stdout.strip() == "feat: add c"


def test_switching_with_uncommitted_changes_is_refused(ctx, repo):
    _git(repo, "branch", "feature")
    (repo / "a.txt").write_text("edited\n", encoding="utf-8")

    result = call("switch_branch", ctx, name="feature")

    assert result["isError"] is True
    assert "uncommitted changes" in body(result)


def test_create_tag_refuses_a_name_that_exists(ctx, repo):
    _git(repo, "tag", "v1")
    result = call("create_tag", ctx, name="v1")
    assert result["isError"] is True


def test_a_failing_git_command_is_reported_not_raised(ctx):
    result = call("push", ctx)  # no remote in a fresh repo
    assert result["isError"] is True
    assert body(result)


# ---- schemas describe what the handlers actually return ------------------------------
def test_structured_results_match_their_declared_output_schema(ctx, repo):
    _git(repo, "branch", "feature")
    cases = [
        ("list_repos", {}),
        ("repo_status", {}),
        ("list_branches", {}),
        ("list_tags", {}),
        ("list_agent_runs", {}),
    ]
    for name, arguments in cases:
        tool = tools.find(name, allow_writes=True)
        result = call(name, ctx, **arguments)
        assert "structuredContent" in result, name
        problems = schema.problems(tool.output_schema, result["structuredContent"])
        assert not problems, f"{name}: {problems}"


# ---- argument checking ------------------------------------------------------------------
def test_an_unknown_argument_is_rejected():
    tool = tools.find("repo_status", allow_writes=False)
    assert tools.check_arguments(tool, {"nonsense": 1})


def test_a_number_out_of_range_is_rejected():
    tool = tools.find("get_diff", allow_writes=False)
    assert tools.check_arguments(tool, {"max_bytes": 5})


def test_a_tool_error_becomes_a_failed_result(ctx):
    tool = tools.find("get_agent_run", allow_writes=False)
    result = tools.run(
        tool, {"run_id": "nope"}, ctx, is_cancelled=lambda: False, progress=lambda _m: None
    )
    assert result["isError"] is True
    assert "list_agent_runs" in body(result)
