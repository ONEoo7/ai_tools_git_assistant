"""Is this repository configured the way it needs to be?

Runs the checks in ``checks.py`` against one probe of the repository and turns
their verdicts into a report: what is broken, what is risky, what is fine, and
the command that fixes each one. Nothing here changes the repository -- the
useful fixes rewrite history or delete objects, and that is a decision, not a
button.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from git_assistant.agents import checks, probe as probe_mod
from git_assistant.agents.base import (
    AgentContext,
    AgentInfo,
    CheckResult,
    Report,
    Section,
    Status,
    Table,
)
from git_assistant.agents.facts import count_fact, fact, human_bytes

DESCRIPTION = (
    "Checks the configuration a repository carries with it: whether large and "
    "binary files are routed through Git LFS, whether line endings are decided "
    "in .gitattributes rather than by each machine's git config, and a dozen "
    "other things that are cheap to get right and expensive to discover late."
)

#: Sections are numbered from here; one per check, under Findings.
_FINDINGS = "2"


def _large_mb(ctx: AgentContext) -> int:
    """What counts as a large file here, from the settings in force."""
    from git_assistant import repo_config

    return repo_config.for_repo(ctx.settings, ctx.repo).audit.large_file_mb


class ConfigAuditAgent:
    info = AgentInfo(
        id="config-audit",
        label="Configuration",
        description=DESCRIPTION,
        cost_hint="Seconds. Reads the index and config; changes nothing.",
    )

    def collect(self, ctx: AgentContext) -> Report:
        probe = probe_mod.collect(ctx)
        ctx.check_cancel()
        ctx.say("Running checks...")
        results = checks.run_all(probe, large_mb=_large_mb(ctx))
        results.sort(key=lambda r: r.sort_key())
        return _build(ctx, probe, results)


def _counts(results: list[CheckResult]) -> dict[Status, int]:
    return {
        status: sum(1 for r in results if r.status is status) for status in Status
    }


def _build(ctx: AgentContext, probe, results: list[CheckResult]) -> Report:
    tally = _counts(results)
    failed = [r for r in results if r.status is Status.FAIL]
    warned = [r for r in results if r.status is Status.WARN]

    summary = Section(
        number="1",
        title="Executive summary",
        slot="exec_summary",
        facts=[
            count_fact("checks_run", "Checks run", len(results) - tally[Status.SKIP]),
            count_fact("checks_failed", "Failed", tally[Status.FAIL]),
            count_fact("checks_warned", "Warnings", tally[Status.WARN]),
            count_fact("checks_passed", "Passed", tally[Status.PASS]),
            count_fact("checks_skipped", "Not applicable", tally[Status.SKIP]),
            count_fact("tracked_files", "Tracked files", len(probe.tracked)),
        ],
        tables=[
            Table(
                title="Findings",
                columns=["Check", "Result", "Finding"],
                rows=[[r.id, r.status.label(), r.headline] for r in results],
            )
        ],
    )
    findings = Section(number=_FINDINGS, title="Findings in detail")
    for index, result in enumerate(results, start=1):
        findings.sections.append(_finding_section(f"{_FINDINGS}.{index}", result))

    next_steps = Section(
        number="3",
        title="Recommended next steps",
        slot="next_steps",
        facts=[
            fact("worst", "Most serious finding", failed[0].headline if failed else "none"),
            count_fact("to_fix", "Findings needing action", len(failed) + len(warned)),
        ],
        commands=[
            (f"{r.id} — {r.title}", r.remediation)
            for r in (*failed, *warned)
            if r.remediation
        ],
    )

    repo_facts = Section(
        number="4",
        title="Repository facts",
        facts=[
            fact("repo_path", "Path", ctx.repo),
            # Same key as the summary's: one measurement, said twice for the
            # reader, counted once by anything comparing two runs.
            count_fact("tracked_files", "Tracked files", len(probe.tracked)),
            fact(
                "index_size",
                "Content in the index",
                human_bytes(sum(probe.sizes.values())),
            ),
            count_fact("attributes_files", "'.gitattributes' files", len(probe.attributes_files)),
            count_fact("lfs_paths", "Paths routed through LFS", len(probe.lfs_paths())),
            fact("lfs_version", "git-lfs", probe.lfs_version or "not installed"),
            fact("core_autocrlf", "core.autocrlf", _scoped(probe, "core.autocrlf")),
            fact("core_eol", "core.eol", _scoped(probe, "core.eol")),
            fact("core_filemode", "core.filemode", _scoped(probe, "core.filemode")),
        ],
    )

    return Report(
        agent_id=ConfigAuditAgent.info.id,
        title="Git repository configuration audit",
        subtitle=f"{Path(ctx.repo).name or ctx.repo} — findings and recommendations",
        generated_at=datetime.now().strftime("%d %B %Y %H:%M"),
        repo_path=ctx.repo,
        sections=[summary, findings, next_steps, repo_facts],
        # The same verdicts the sections show, kept in a form a later run can be
        # compared against without parsing prose back out of a table.
        checks=results,
    )


def _scoped(probe, key: str) -> str:
    """``true (system), false (global)`` -- every scope, because that is the point."""
    entries = probe.values(key)
    if not entries:
        return "not set"
    return ", ".join(f"{value or '(empty)'} ({scope})" for scope, value in entries)


def _finding_section(number: str, result: CheckResult) -> Section:
    section = Section(
        number=number,
        title=f"{result.id} — {result.title}",
        facts=[
            fact(f"{result.id}_status", "Result", result.status.label()),
            fact(f"{result.id}_finding", "Finding", result.headline),
        ],
    )
    if result.evidence:
        section.tables.append(
            Table(title="Evidence", columns=["Detail"], rows=[[e] for e in result.evidence])
        )
    if result.remediation and result.status in (Status.FAIL, Status.WARN):
        section.commands.append(("How to fix it:", result.remediation))
    return section
