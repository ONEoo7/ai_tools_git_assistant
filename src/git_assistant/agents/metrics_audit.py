"""How much code is here, and what it is written in.

The counting itself is ``git_assistant.metrics`` and predates this: tracked
files only, via ``git ls-files``, so build output and anything ignored is not
counted as somebody's work. Binary files are skipped rather than counted as
enormous single lines.

An audit rather than a window of its own, because everything that made it a
window of its own is answered better here: which repository (the picker), when
it was measured (the run), and whether it went up or down since (the
comparison). A line count is only interesting against another line count.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from git_assistant import metrics as metrics_mod
from git_assistant.agents.base import AgentContext, AgentInfo, Report, Section, Table
from git_assistant.agents.facts import count_fact, human_count, percent

DESCRIPTION = (
    "Counts the lines in everything this repository tracks, grouped by file "
    "type: how much of it there is, how much is blank, and which types it is "
    "mostly made of. Tracked files only, so build output and anything ignored "
    "is not counted as work; binary files are skipped rather than counted."
)

#: Types listed one by one. Past this the table is a long tail of one-file
#: extensions, and the total already says what they add up to.
TOP_TYPES = 20


class MetricsAgent:
    info = AgentInfo(
        id="metrics",
        label="Metrics",
        description=DESCRIPTION,
        cost_hint="Seconds; longer on a repository of many thousands of files.",
    )

    def collect(self, ctx: AgentContext) -> Report:
        ctx.say("Listing tracked files...", 5)
        measured = metrics_mod.analyze_repo(ctx.repo)
        ctx.check_cancel()
        ctx.say("Counting lines...", 60)
        return _build(ctx, measured)


def _build(ctx: AgentContext, measured) -> Report:
    report = Report(
        agent_id=MetricsAgent.info.id,
        title="Repository metrics",
        subtitle=Path(ctx.repo).name or ctx.repo,
        generated_at=datetime.now().astimezone().strftime("%d %B %Y %H:%M"),
        repo_path=ctx.repo,
    )
    if not measured.ok:
        report.warnings.append(measured.error or "The repository could not be read.")

    totals = measured.totals
    report.sections.append(
        Section(
            number="1",
            title="Executive summary",
            slot="metrics_summary",
            facts=[
                count_fact("files", "Tracked text files", totals.files),
                count_fact("lines", "Lines in total", totals.lines),
                count_fact("code_lines", "Lines that are not blank", totals.code),
                count_fact("blank_lines", "Blank lines", totals.blank),
                count_fact("file_types", "File types", len(measured.by_ext)),
            ],
        )
    )
    report.sections.append(_by_type(measured, totals))
    ctx.say("Writing the report...", 95)
    return report


def _by_type(measured, totals) -> Section:
    """The types, largest first, with what each is of the whole."""
    ranked = sorted(
        measured.by_ext.items(), key=lambda kv: kv[1].lines, reverse=True
    )
    shown = ranked[:TOP_TYPES]
    rows = [
        [
            ext,
            human_count(stat.files),
            human_count(stat.lines),
            human_count(stat.code),
            percent(stat.lines, totals.lines),
        ]
        for ext, stat in shown
    ]
    note = ""
    if len(ranked) > len(shown):
        rest = sum(stat.lines for _ext, stat in ranked[len(shown) :])
        # Said rather than left off: a table that stops at twenty rows without
        # saying so reads as a repository with twenty file types in it.
        note = (
            f"{len(ranked) - len(shown)} further type(s) hold "
            f"{human_count(rest)} line(s) between them."
        )

    section = Section(
        number="2",
        title="By file type",
        slot="metrics_types",
        tables=[
            Table(
                title="Largest first",
                columns=["Type", "Files", "Lines", "Code", "Share"],
                rows=rows,
                note=note,
            )
        ],
    )
    if shown:
        biggest, stat = shown[0]
        section.facts = [
            count_fact("largest_type_lines", f"Lines of {biggest}", stat.lines),
        ]
    return section
