"""Agents: read a repository, report on it.

An agent measures one repository with git and returns a :class:`Report`. The
configured inference provider then writes the prose around those measurements,
and only that -- see ``narrator``.

The registry is a static tuple rather than a scan of this package: PyInstaller
follows imports it can see, and a dynamically discovered agent would be missing
from the packaged build.
"""

from __future__ import annotations

from git_assistant import git_ops
from git_assistant.agents import narrator
from git_assistant.agents.base import (
    Agent,
    AgentContext,
    AgentInfo,
    CancelledError,
    Report,
    _never,
    _noop,
    _noop_pct,
)
from git_assistant.agents.config_audit import ConfigAuditAgent
from git_assistant.model_runtime import ModelRuntime
from git_assistant.agents.size_audit import SizeAuditAgent
from git_assistant import llm_log, tracing, usage
from git_assistant.llm import build_client
from git_assistant.llm_log import RecordingClient

AGENTS: tuple[Agent, ...] = (SizeAuditAgent(), ConfigAuditAgent())

__all__ = ["AGENTS", "Agent", "AgentInfo", "CancelledError", "Report", "get", "infos", "run"]


def infos() -> list[AgentInfo]:
    return [agent.info for agent in AGENTS]


def _sample_point(repo: str) -> tuple[str, str, bool]:
    """``(head, branch, dirty)`` for the repository, or blanks if git cannot say."""
    head = git_ops._run(repo, ["rev-parse", "HEAD"])
    return (
        head.stdout.strip() if head.ok else "",
        git_ops.current_branch(repo),
        git_ops.has_uncommitted_changes(repo),
    )


def get(agent_id: str) -> Agent:
    for agent in AGENTS:
        if agent.info.id == agent_id:
            return agent
    raise KeyError(f"no agent named {agent_id!r}")


def run(
    agent_id: str,
    settings,
    *,
    repo: str = "",
    progress=_noop,
    progress_pct=_noop_pct,
    is_cancelled=_never,
    fast: bool = False,
    narrate: bool = True,
    on_call=None,
) -> Report:
    """Collect, then narrate.

    Collection is the part that cannot be redone cheaply -- it can take minutes
    on a large repository -- so a provider that is missing, misconfigured or
    down is recorded as a warning on a finished report rather than being allowed
    to throw it away.

    ``on_call`` is handed every exchange with the model as it finishes, for the
    tab that shows them. The client is wrapped only when someone is listening,
    so nothing pays for a recorder it will not read.
    """
    agent = get(agent_id)
    ctx = AgentContext(
        repo=repo or settings.active_repo,
        settings=settings,
        progress=progress,
        progress_pct=progress_pct,
        is_cancelled=is_cancelled,
        fast=fast,
    )
    if not ctx.repo:
        raise ValueError("No repository is selected.")

    # Sampled before the scan, not after: reading every object in a large
    # repository takes minutes, and the report has to name the state it started
    # from rather than whatever HEAD happens to be when it finishes.
    where = _sample_point(ctx.repo)
    report = agent.collect(ctx)
    report.head, report.branch, report.dirty = where
    narrator.fill_deterministic(report)
    if not narrate:
        return report

    client = None
    try:
        client = build_client(settings, feature=usage.AUDIT)
        if on_call is not None:
            client = RecordingClient(client, on_call=on_call)
            client.phase = llm_log.NARRATE
        runtime = ModelRuntime(settings, client)
    except Exception as exc:
        report.warnings.append(f"Written without the model: {exc}")
        tracing.close(client)
        return report
    try:
        narrator.narrate(report, runtime, ctx)
    finally:
        tracing.close(client)  # the end of the run, and so of its trace
    return report
