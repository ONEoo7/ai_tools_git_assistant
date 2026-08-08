"""What the server offers, and what each one does.

Every tool is one function over code that already exists and is already tested:
``git_ops`` for the repository, ``CommitGenerator`` for a message, ``agents``
for an audit. Nothing here re-implements any of it.

The five tools that change a repository are declared but withheld: without
``--allow-writes`` they are absent from ``tools/list`` entirely, so a model
cannot be tempted by a tool it has no way to see, and calling one by name says
which checkbox turns it on.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from git_assistant import git_ops, repo_config
from git_assistant.mcp import schema
from git_assistant.mcp.context import NO_REPOSITORIES, ToolContext, ToolError

REPO_ARGUMENT = {
    "type": "string",
    "description": (
        "Path or label of a configured repository. Omit for the active one."
    ),
}
#: A diff big enough to be useless in a conversation is worse than a truncated
#: one that says it was truncated.
DEFAULT_DIFF_BYTES = 100_000


@dataclass
class Tool:
    name: str
    title: str
    description: str
    run: Callable
    input_schema: dict = field(default_factory=dict)
    output_schema: dict | None = None
    writes: bool = False  # needs --allow-writes
    slow: bool = False  # runs on the pool: minutes, cancellable

    def definition(self) -> dict:
        out = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": self.input_schema
            or {"type": "object", "additionalProperties": False},
            "annotations": {
                "readOnlyHint": not self.writes,
                "destructiveHint": self.writes,
                "openWorldHint": self.name == "generate_commit_message",
            },
        }
        if self.output_schema:
            out["outputSchema"] = self.output_schema
        return out


def _object(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def text(body: str, structured=None) -> dict:
    result = {
        "resultType": "complete",
        "content": [{"type": "text", "text": body}],
        "isError": False,
    }
    if structured is not None:
        result["structuredContent"] = structured
    return result


def failure(message: str) -> dict:
    """A tool that could not do its job. Actionable, so the model can retry."""
    return {
        "resultType": "complete",
        "content": [{"type": "text", "text": message}],
        "isError": True,
    }


# ---- reading a repository -------------------------------------------------
def _describe(path: str, active: str) -> dict:
    return {
        "path": path,
        "branch": git_ops.current_branch(path),
        "dirty": git_ops.has_uncommitted_changes(path),
        "active": path == active,
    }


def _list_repos(ctx: ToolContext, args: dict, **_kw) -> dict:
    settings = ctx.settings()
    paths = ctx._known(settings)
    # Two git processes per repository, and a machine can have fifty of them.
    # They are all waiting on the same disk, so run them together.
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda p: _describe(p, settings.active_repo), paths))
    if not rows:
        # Same words as every other tool's version of this: one condition, one
        # answer, and the answer says what to do about it.
        return failure(NO_REPOSITORIES)
    lines = [
        f"{'* ' if r['active'] else '  '}{r['path']}  [{r['branch']}]"
        f"{'  (uncommitted changes)' if r['dirty'] else ''}"
        for r in rows
    ]
    return text("\n".join(lines), {"repositories": rows})


def _repo_status(ctx: ToolContext, args: dict, **_kw) -> dict:
    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    ahead = git_ops.unpushed_count(repo)
    data = {
        "path": repo,
        "branch": git_ops.current_branch(repo),
        "upstream": git_ops.get_upstream(repo) or "",
        "unpushed": ahead if ahead is not None else -1,
        "dirty": git_ops.has_uncommitted_changes(repo),
        "staged": git_ops.get_diffstat(repo, "cached").strip(),
        "unstaged": git_ops.get_diffstat(repo, "working").strip(),
        "submodules": len(git_ops.find_submodules(repo)),
    }
    lines = [
        f"{repo}",
        f"branch {data['branch']}"
        + (f", tracking {data['upstream']}" if data["upstream"] else ", no upstream"),
        f"{data['unpushed']} unpushed commit(s)" if data["unpushed"] >= 0 else "",
        "staged:\n" + (data["staged"] or "  nothing staged"),
        "uncommitted:\n" + (data["unstaged"] or "  nothing"),
    ]
    return text("\n".join(l for l in lines if l), data)


def _get_diff(ctx: ToolContext, args: dict, **_kw) -> dict:
    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    mode = args.get("mode") or repo_config.bind(settings, repo).diff_mode
    cap = int(args.get("max_bytes") or DEFAULT_DIFF_BYTES)
    diff = git_ops.get_diff(repo, mode)
    if not diff.strip():
        return text(f"Nothing to show: no {mode} changes in {repo}.")
    body = diff[:cap]
    if len(diff) > cap:
        body += f"\n\n[truncated: {cap} of {len(diff)} bytes]"
    return text(body)


def _list_branches(ctx: ToolContext, args: dict, **_kw) -> dict:
    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    current = git_ops.current_branch(repo)
    branches = git_ops.list_branches(repo)
    lines = [f"{'* ' if b == current else '  '}{b}" for b in branches]
    return text(
        "\n".join(lines) or "This repository has no branches yet.",
        {"branches": branches, "current": current},
    )


def _list_tags(ctx: ToolContext, args: dict, **_kw) -> dict:
    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    limit = int(args.get("limit") or 20)
    tags = git_ops.list_tags_with_dates(repo)[:limit]
    rows = [{"name": name, "date": date} for name, date in tags]
    lines = [f"{name}  {date}" for name, date in tags]
    return text("\n".join(lines) or "No tags.", {"tags": rows})


def _describe_push_auth(ctx: ToolContext, args: dict, **_kw) -> dict:
    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    auth = git_ops.describe_push_auth(repo)
    body = auth.summary()
    if auth.warning():
        body += f"\n{auth.warning()}"
    return text(body)


# ---- the things that need the model ----------------------------------------
def _generate_commit_message(ctx: ToolContext, args: dict, *, progress, is_cancelled) -> dict:
    from git_assistant.commit_generator import CommitGenerator
    from git_assistant import usage
    from git_assistant.llm import build_client

    settings = ctx.settings()  # a private copy: the generator reads these
    settings.active_repo = ctx.resolve(settings, args.get("repo"))
    if args.get("template"):
        settings.set_repo_template(settings.active_repo, args["template"])
    # What to send is the repository's answer, who to send it to is the user's.
    # A `mode` in the arguments answers for this call and is written nowhere:
    # one client asking for the working tree must not reconfigure the
    # repository for the window.
    bound = repo_config.bind(
        settings,
        settings.active_repo,
        **({"diff_mode": args["mode"]} if args.get("mode") else {}),
    )

    from git_assistant import tracing

    client = build_client(bound, feature=usage.COMMIT)
    try:
        result = CommitGenerator(bound, client).generate(
            progress=progress, is_cancelled=is_cancelled
        )
    finally:
        tracing.close(client)  # the end of the run, and so of its trace
    # The message alone, so it can be used verbatim. Everything about how it was
    # produced is in the structured half for a caller that wants it.
    from git_assistant import commit_style

    measured = commit_style.measure(result.message, commit_style.Limits.of(bound))
    return text(
        result.message,
        {
            "message": result.message,
            "strategy": result.strategy,
            "input_tokens": result.input_tokens,
            "context_window": result.context_window,
            "dropped_files": result.dropped_files,
            # Reported, not enforced: a client that cares can shorten it, and
            # one that does not gets a message rather than a refusal.
            "subject_length": measured.subject,
            "body_length": measured.body,
            "too_long": measured.too_long,
        },
    )


def _run_agent(ctx: ToolContext, args: dict, *, progress, is_cancelled) -> dict:
    from git_assistant import agents
    from git_assistant.agents import report as report_mod

    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    report = agents.run(
        args["agent"],
        settings,
        repo=repo,
        progress=progress,
        is_cancelled=is_cancelled,
        fast=bool(args.get("fast")),
        narrate=args.get("narrate", True),
    )
    return text(report_mod.to_markdown(report))


def _list_agent_runs(ctx: ToolContext, args: dict, **_kw) -> dict:
    from git_assistant.agents import history

    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    runs = history.list_runs(repo, args.get("agent") or "")
    rows = [
        {
            "run_id": run.run_id,
            "agent": run.agent_id,
            "when": run.started_at,
            "commit": run.commit_label(),
        }
        for run in runs
    ]
    lines = [f"{r['when']}  {r['agent']}  {r['commit']}  {r['run_id']}" for r in rows]
    return text("\n".join(lines) or f"No recorded runs for {repo}.", {"runs": rows})


def _get_agent_run(ctx: ToolContext, args: dict, **_kw) -> dict:
    from git_assistant.agents import history
    from git_assistant.agents import report as report_mod

    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    run = _find_run(repo, args["run_id"])
    return text(report_mod.to_markdown(run.report))


def _compare_agent_runs(ctx: ToolContext, args: dict, **_kw) -> dict:
    from git_assistant.agents import compare

    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    before = _find_run(repo, args["before_run_id"])
    after = _find_run(repo, args["after_run_id"])
    difference = compare.diff(before, after)
    if difference is None:
        return failure("Those two runs are from different agents.")
    return text(f"{difference.summary()}\n\n{compare.to_markdown(difference)}")


def _find_run(repo: str, run_id: str):
    from git_assistant.agents import history

    for run in history.list_runs(repo):
        if run.run_id == run_id:
            loaded = history.load_run(run)
            if loaded is None:
                raise ToolError(f"the stored run {run_id} could not be read")
            return loaded
    raise ToolError(f"no recorded run {run_id!r} for {repo}; try list_agent_runs")


# ---- changing a repository (only with --allow-writes) ----------------------
def _result_of(action, note: str) -> dict:
    data = {
        "ok": action.ok,
        "returncode": action.returncode,
        "stdout": action.stdout.strip(),
        "stderr": action.stderr.strip(),
    }
    if not action.ok:
        return failure(f"{note} failed: {action.stderr.strip() or action.stdout.strip()}")
    return text(f"{note}.", data)


def _commit(ctx: ToolContext, args: dict, **_kw) -> dict:
    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    if not git_ops.has_changes(repo, "cached"):
        return failure(f"Nothing is staged in {repo}, so there is nothing to commit.")
    return _result_of(git_ops.commit(repo, args["message"]), "Committed")


def _push(ctx: ToolContext, args: dict, **_kw) -> dict:
    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    return _result_of(git_ops.push(repo, args.get("remote") or "origin"), "Pushed")


def _create_tag(ctx: ToolContext, args: dict, **_kw) -> dict:
    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    if git_ops.tag_exists(repo, args["name"]):
        return failure(f"{args['name']} already exists in {repo}.")
    return _result_of(
        git_ops.create_tag(repo, args["name"], args.get("message") or ""), "Tag created"
    )


def _push_tag(ctx: ToolContext, args: dict, **_kw) -> dict:
    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    return _result_of(
        git_ops.push_tag(repo, args["name"], args.get("remote") or "origin"), "Tag pushed"
    )


def _switch_branch(ctx: ToolContext, args: dict, **_kw) -> dict:
    settings = ctx.settings()
    repo = ctx.resolve(settings, args.get("repo"))
    if git_ops.has_uncommitted_changes(repo):
        return failure(
            f"{repo} has uncommitted changes; they would come along. Commit or "
            "stash them first."
        )
    return _result_of(
        git_ops.switch_branch(repo, args["name"]), f"Switched to {args['name']}"
    )


# ---- the catalogue ---------------------------------------------------------
_STRING = {"type": "string"}

TOOLS: tuple[Tool, ...] = (
    Tool(
        "list_repos",
        "List repositories",
        "The repositories configured in Git Assistant, with their branch and "
        "whether they have uncommitted changes. Start here: every other tool "
        "names one of these.",
        _list_repos,
        output_schema=_object({"repositories": {"type": "array", "items": _object({
            "path": _STRING, "branch": _STRING,
            "dirty": {"type": "boolean"}, "active": {"type": "boolean"},
        })}}),
    ),
    Tool(
        "repo_status",
        "Repository status",
        "Branch, upstream, unpushed commits, and what is staged and unstaged.",
        _repo_status,
        _object({"repo": REPO_ARGUMENT}),
        _object({
            "path": _STRING, "branch": _STRING, "upstream": _STRING,
            "unpushed": {"type": "integer"}, "dirty": {"type": "boolean"},
            "staged": _STRING, "unstaged": _STRING,
            "submodules": {"type": "integer"},
        }),
    ),
    Tool(
        "get_diff",
        "Read the diff",
        "The staged diff ('cached') or everything uncommitted ('working'), "
        "truncated to max_bytes with the truncation marked.",
        _get_diff,
        _object({
            "repo": REPO_ARGUMENT,
            "mode": {"type": "string", "enum": ["cached", "working"]},
            "max_bytes": {"type": "integer", "minimum": 1000, "maximum": 2_000_000},
        }),
    ),
    Tool(
        "list_branches",
        "List branches",
        "Local branches, most recently committed to first, with the checked-out "
        "one marked.",
        _list_branches,
        _object({"repo": REPO_ARGUMENT}),
        _object({"branches": {"type": "array", "items": _STRING}, "current": _STRING}),
    ),
    Tool(
        "list_tags",
        "List tags",
        "Tags with their dates, newest first.",
        _list_tags,
        _object({
            "repo": REPO_ARGUMENT,
            "limit": {"type": "integer", "minimum": 1, "maximum": 500},
        }),
        _object({"tags": {"type": "array", "items": _object({"name": _STRING, "date": _STRING})}}),
    ),
    Tool(
        "describe_push_auth",
        "Explain how a push would authenticate",
        "Which remote, which credential helper and which identity a push would "
        "use. Worth checking before enabling write tools.",
        _describe_push_auth,
        _object({"repo": REPO_ARGUMENT}),
    ),
    Tool(
        "generate_commit_message",
        "Write a commit message",
        "Generate a Conventional Commits message for what is staged, using the "
        "inference provider configured in Git Assistant. Returns the message "
        "alone, ready to use. Takes as long as that provider does.",
        _generate_commit_message,
        _object({
            "repo": REPO_ARGUMENT,
            "mode": {"type": "string", "enum": ["cached", "working"]},
            "template": {"type": "string", "description": "A named prompt template."},
        }),
        _object({
            "message": _STRING, "strategy": _STRING,
            "input_tokens": {"type": "integer"}, "context_window": {"type": "integer"},
            "dropped_files": {"type": "array", "items": _STRING},
        }),
        slow=True,
    ),
    Tool(
        "run_agent",
        "Audit a repository",
        "Run an audit and return its report. 'size-audit' reports where the "
        ".git bytes went and what can be reclaimed; 'config-audit' checks Git "
        "LFS coverage, line-ending configuration and related hygiene. The size "
        "audit reads every object in history and can take minutes.",
        _run_agent,
        _object({
            "repo": REPO_ARGUMENT,
            "agent": {"type": "string", "enum": ["size-audit", "config-audit"]},
            "fast": {"type": "boolean", "description": "Skip the per-file history scan."},
            "narrate": {"type": "boolean", "description": "Let the provider write the prose."},
        }, ["agent"]),
        slow=True,
    ),
    Tool(
        "list_agent_runs",
        "List recorded audits",
        "Audits already recorded for a repository, newest first.",
        _list_agent_runs,
        _object({
            "repo": REPO_ARGUMENT,
            "agent": {"type": "string", "enum": ["size-audit", "config-audit"]},
        }),
        _object({"runs": {"type": "array", "items": _object({
            "run_id": _STRING, "agent": _STRING, "when": _STRING, "commit": _STRING,
        })}}),
    ),
    Tool(
        "get_agent_run",
        "Read a recorded audit",
        "The full report of one recorded audit.",
        _get_agent_run,
        _object({"repo": REPO_ARGUMENT, "run_id": _STRING}, ["run_id"]),
    ),
    Tool(
        "compare_agent_runs",
        "Compare two audits",
        "What changed between two recorded audits of the same repository: which "
        "checks were fixed or regressed, and which measurements moved.",
        _compare_agent_runs,
        _object({
            "repo": REPO_ARGUMENT,
            "before_run_id": _STRING,
            "after_run_id": _STRING,
        }, ["before_run_id", "after_run_id"]),
    ),
    # ---- write tools: absent unless the server was started with --allow-writes
    Tool(
        "commit",
        "Commit what is staged",
        "Commit the staged changes with the given message. Stages nothing "
        "itself.",
        _commit,
        _object({"repo": REPO_ARGUMENT, "message": _STRING}, ["message"]),
        _object({
            "ok": {"type": "boolean"}, "returncode": {"type": "integer"},
            "stdout": _STRING, "stderr": _STRING,
        }),
        writes=True,
    ),
    Tool(
        "push",
        "Push the current branch",
        "Push the checked-out branch to its remote, publishing the commits on it.",
        _push,
        _object({"repo": REPO_ARGUMENT, "remote": _STRING}),
        _object({
            "ok": {"type": "boolean"}, "returncode": {"type": "integer"},
            "stdout": _STRING, "stderr": _STRING,
        }),
        writes=True,
    ),
    Tool(
        "create_tag",
        "Create a tag",
        "Create a local tag; annotated when a message is given.",
        _create_tag,
        _object({"repo": REPO_ARGUMENT, "name": _STRING, "message": _STRING}, ["name"]),
        _object({
            "ok": {"type": "boolean"}, "returncode": {"type": "integer"},
            "stdout": _STRING, "stderr": _STRING,
        }),
        writes=True,
    ),
    Tool(
        "push_tag",
        "Push a tag",
        "Publish a local tag to the remote. Anyone who fetches it will have it.",
        _push_tag,
        _object({"repo": REPO_ARGUMENT, "name": _STRING, "remote": _STRING}, ["name"]),
        _object({
            "ok": {"type": "boolean"}, "returncode": {"type": "integer"},
            "stdout": _STRING, "stderr": _STRING,
        }),
        writes=True,
    ),
    Tool(
        "switch_branch",
        "Switch branch",
        "Check out an existing local branch. Refused when the work tree has "
        "uncommitted changes.",
        _switch_branch,
        _object({"repo": REPO_ARGUMENT, "name": _STRING}, ["name"]),
        _object({
            "ok": {"type": "boolean"}, "returncode": {"type": "integer"},
            "stdout": _STRING, "stderr": _STRING,
        }),
        writes=True,
    ),
)


def available(allow_writes: bool) -> tuple[Tool, ...]:
    return tuple(t for t in TOOLS if allow_writes or not t.writes)


def catalogue(allow_writes: bool = False) -> list[dict]:
    return [t.definition() for t in available(allow_writes)]


def find(name, allow_writes: bool) -> Tool | None:
    return next((t for t in available(allow_writes) if t.name == name), None)


def explain_missing(name) -> str:
    """Why a tool the caller named is not there -- gated, or simply unknown."""
    gated = next((t for t in TOOLS if t.name == name), None)
    if gated is not None and gated.writes:
        return (
            f"{name} changes the repository and this server was started without "
            "write access. Enable 'Allow write operations' in Git Assistant's "
            "MCP Server tab and register the server again."
        )
    return f"unknown tool: {name}"


def check_arguments(tool: Tool, arguments: dict) -> list[str]:
    return schema.problems(tool.input_schema, arguments)


def run(tool: Tool, arguments: dict, ctx: ToolContext, *, is_cancelled, progress) -> dict:
    try:
        return tool.run(ctx, arguments, progress=progress, is_cancelled=is_cancelled)
    except ToolError as exc:
        return failure(str(exc))
