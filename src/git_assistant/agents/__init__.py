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
from git_assistant.agents.model_runtime import ModelRuntime
from git_assistant.agents.size_audit import SizeAuditAgent
from git_assistant.llm import build_client

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
) -> Report:
    """Collect, then narrate.

    Collection is the part that cannot be redone cheaply -- it can take minutes
    on a large repository -- so a provider that is missing, misconfigured or
    down is recorded as a warning on a finished report rather than being allowed
    to throw it away.
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

    try:
        runtime = ModelRuntime(settings, build_client(settings))
    except Exception as exc:
        report.warnings.append(f"Written without the model: {exc}")
        return report
    narrator.narrate(report, runtime, ctx)
    return report
