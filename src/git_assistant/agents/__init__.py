"""Agents: read a repository, report on it.

An agent measures one repository with git and returns a :class:`Report`. The
configured inference provider then writes the prose around those measurements,
and only that -- see ``narrator``.

The registry is a static tuple rather than a scan of this package: PyInstaller
follows imports it can see, and a dynamically discovered agent would be missing
from the packaged build.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from git_assistant import git_ops
from git_assistant.agents import narrator
from git_assistant.agents.base import (
    INDETERMINATE,
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
from git_assistant.agents.consistency_audit import ConsistencyAuditAgent
from git_assistant.model_runtime import ModelRuntime
from git_assistant.agents.size_audit import SizeAuditAgent
from git_assistant import llm_log, tracing, usage
from git_assistant.llm import build_client
from git_assistant.llm_log import RecordingClient
from git_assistant.parallel import CancelledError as ParallelCancelled
from git_assistant.parallel import effective_parallel, run_parallel

AGENTS: tuple[Agent, ...] = (
    SizeAuditAgent(),
    ConfigAuditAgent(),
    ConsistencyAuditAgent(),
)

__all__ = [
    "AGENTS",
    "Agent",
    "AgentInfo",
    "AuditRun",
    "CancelledError",
    "Report",
    "audit_workers",
    "get",
    "infos",
    "run",
    "run_many",
]


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


def _collect(
    agent_id: str,
    settings,
    *,
    repo: str,
    progress,
    progress_pct,
    is_cancelled,
    fast: bool,
) -> tuple[Report, AgentContext]:
    """Measure the repository. No provider is contacted here."""
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
    return report, ctx


def _narration_client(settings, on_call):
    """The configured provider, wrapped only when someone is listening."""
    client = build_client(settings, feature=usage.AUDIT)
    if on_call is None:
        return client
    client = RecordingClient(client, on_call=on_call)
    client.phase = llm_log.NARRATE
    return client


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
    report, ctx = _collect(
        agent_id,
        settings,
        repo=repo,
        progress=progress,
        progress_pct=progress_pct,
        is_cancelled=is_cancelled,
        fast=fast,
    )
    if not narrate:
        return report

    client = None
    try:
        client = _narration_client(settings, on_call)
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


# ---- several audits over one repository -------------------------------------
class _SharedProgress:
    """One status line and one bar for several audits running at once.

    The line names whichever audit last said something, because two audits
    reporting into an unlabelled line reads as one audit changing its mind. The
    bar is their mean: separate audits finish at their own pace, and a bar that
    jumped back to 10% every time a different one spoke would be worse than no
    bar at all.
    """

    def __init__(self, progress, progress_pct, labels: dict[str, str]) -> None:
        self._progress = progress
        self._progress_pct = progress_pct
        self._labels = labels
        self._named = len(labels) > 1
        # Keyed by agent id, not by label: two audits are two audits even if
        # something ever gives them the same name.
        self._pct: dict[str, int | None] = {key: None for key in labels}
        self._lock = threading.Lock()

    def progress_for(self, agent_id: str):
        label = self._labels[agent_id]

        def say(message: str) -> None:
            self._progress(f"{label}: {message}" if self._named else message)

        return say

    def pct_for(self, agent_id: str):
        def report(value: int) -> None:
            with self._lock:
                if value != INDETERMINATE:
                    self._pct[agent_id] = value
                known = [v for v in self._pct.values() if v is not None]
                # An audit that has not reported yet counts as nothing done,
                # not as absent: otherwise the first one to speak drags the bar
                # to its own figure and the run looks nearly finished.
                total = sum(known) // len(self._pct) if known else INDETERMINATE
            self._progress_pct(total)

        return report


def audit_workers(settings, count: int, *, narrate: bool, context: int) -> int:
    """How many of ``count`` audits may run at once.

    Audits share nothing with each other -- each measures the repository with
    its own git commands and builds its own report -- so what bounds them is
    the provider, and only when there is one. A narration is one call at a
    time, so N audits in flight is N requests in flight, and N is what
    ``effective_parallel`` allows. Written from the measurements alone, nothing
    is in flight and they are all free to run.
    """
    if count <= 1:
        return 1
    if not narrate:
        return count
    return max(1, min(effective_parallel(settings, context), count))


@dataclass
class AuditRun:
    """One audit's outcome in a run of several.

    A failure is carried rather than raised: three audits are three independent
    readings of the repository, and the one that could not be taken must not
    throw away the two that were.
    """

    agent_id: str
    label: str
    report: Report | None = None
    problem: str = ""

    @property
    def ok(self) -> bool:
        return self.report is not None


def run_many(
    agent_ids: list[str],
    settings,
    *,
    repo: str = "",
    progress=_noop,
    progress_pct=_noop_pct,
    is_cancelled=_never,
    fast: bool = False,
    narrate: bool = True,
    on_call=None,
) -> list[AuditRun]:
    """Run several audits over one repository, side by side. Order is kept.

    One client for the whole run rather than one each: the calls pane numbers
    them in the order they happened, the trace is one trace, and the window they
    are sized against is looked up once. That window is then divided between
    them -- see ``ModelRuntime.slots`` -- because a local server divides it
    between the requests in flight whether or not the caller planned for it.

    A failure belongs to the audit it happened in, and comes back on that
    audit's :class:`AuditRun` rather than as an exception -- one repository
    that git could not answer about must not throw away the audits that
    finished. Cancellation is the exception, and stops everything.
    """
    ids = list(dict.fromkeys(agent_ids))  # a repeat is the same run twice
    if not ids:
        raise ValueError("No audit is selected.")
    # An unknown id fails here, before anything is measured or sent.
    labels = {agent_id: get(agent_id).info.label for agent_id in ids}

    client = None
    runtime = None
    problem = ""
    if narrate:
        try:
            client = _narration_client(settings, on_call)
            runtime = ModelRuntime(settings, client)
            # Asked before the fan-out, so the lazy lookup behind both happens
            # on this thread and the workers only ever read the answer.
            context = runtime.context_window()
            cold = runtime.is_cold()
        except Exception as exc:
            problem = str(exc)
            tracing.close(client)
            client = runtime = None
    if runtime is None:
        context, cold = 0, False

    workers = audit_workers(
        settings, len(ids), narrate=runtime is not None, context=context
    )
    if runtime is not None:
        runtime.slots = workers
    shared = _SharedProgress(progress, progress_pct, labels)

    def one(agent_id: str) -> AuditRun:
        outcome = AuditRun(agent_id=agent_id, label=labels[agent_id])
        try:
            report, ctx = _collect(
                agent_id,
                settings,
                repo=repo,
                progress=shared.progress_for(agent_id),
                progress_pct=shared.pct_for(agent_id),
                is_cancelled=is_cancelled,
                fast=fast,
            )
        except (CancelledError, ParallelCancelled):
            raise  # cancelling one audit cancels the run
        except Exception as exc:
            outcome.problem = str(exc)
            return outcome
        if problem:
            report.warnings.append(f"Written without the model: {problem}")
        if runtime is not None:
            # Its own failures are already warnings on the report -- narration
            # cannot lose a scan that took minutes.
            narrator.narrate(report, runtime, ctx)
        outcome.report = report
        return outcome

    try:
        return run_parallel(
            ids,
            one,
            workers=workers,
            # The first call loads the model; the rest would meet a server that
            # is still loading and be refused. Only worth paying when the server
            # says it has not loaded it yet.
            cold_start=cold,
            progress=progress,
            is_cancelled=is_cancelled,
            label="auditing",
        )
    finally:
        tracing.close(client)  # the end of the run, and so of its trace
