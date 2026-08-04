"""Prose for a report that is already true.

Every section arrives with its measurements attached. The model is handed those
measurements and asked to write the paragraph above them -- and then its answer
is read back: any figure it wrote that was not in the measurements is rejected,
once with a correction, and after that the section keeps the plain rendering of
its own facts. That is what makes "the model did not invent numbers" a property
of the code rather than a hope about the prompt.
"""

from __future__ import annotations

from git_assistant.agents import prompts
from git_assistant.agents.base import AgentContext, Report, Section
from git_assistant.agents.facts import (
    allowed_figures,
    facts_block,
    unsupported_figures,
)
from git_assistant.agents.model_runtime import ModelRuntime
from git_assistant.tokenizer import estimate_tokens

#: Rows dropped from the end of a table when the facts do not fit. Tables are
#: shortened rather than cut mid-row: half a table reads as the whole one.
_TRIM_STEP = 5


def fill_deterministic(report: Report) -> None:
    """Give every narrated section prose made from its own facts.

    Run before narration, so a report is readable even if the provider never
    answers, and left in place for any section the model could not write.
    """
    for section in report.walk():
        if section.slot and not section.prose:
            section.prose = _plain(section)


def _plain(section: Section) -> str:
    """The measurements as a sentence. Dull, and correct by construction."""
    if not section.facts:
        return ""
    parts = [f"{f.label.lower()}: {f.value}" for f in section.facts]
    return f"Measured — {'; '.join(parts)}."


def narrate(
    report: Report,
    runtime: ModelRuntime,
    ctx: AgentContext,
) -> None:
    """Write the prose sections of ``report`` in place."""
    outlines = prompts.OUTLINES.get(report.agent_id, {})
    todo = [s for s in report.walk() if s.slot in outlines]
    for index, section in enumerate(todo, start=1):
        ctx.check_cancel()
        ctx.say(f"Writing section {index} of {len(todo)}: {section.title}")
        try:
            _narrate_section(report, section, outlines[section.slot], runtime)
        except Exception as exc:  # a provider failure must not lose the report
            report.warnings.append(
                f"Section {section.number} was written from the measurements: {exc}"
            )
            section.prose = _plain(section)
            break  # the next call would fail the same way


def _narrate_section(
    report: Report, section: Section, outline: prompts.Outline, runtime: ModelRuntime
) -> None:
    tables = _fit(report, section, outline, runtime)
    facts = facts_block(section.facts, tables)
    allowed = allowed_figures(section.facts, tables)

    user = outline.render(facts)
    draft = runtime.chat(
        system=prompts.AGENT_SYSTEM, user=user, max_tokens=outline.max_tokens()
    ).strip()
    bad = unsupported_figures(draft, allowed)
    if not bad:
        section.prose = draft
        return

    retry = user + prompts.RETRY_SUFFIX.replace("{bad}", ", ".join(bad[:6]))
    second = runtime.chat(
        system=prompts.AGENT_SYSTEM, user=retry, max_tokens=outline.max_tokens()
    ).strip()
    if not unsupported_figures(second, allowed):
        section.prose = second
        return

    # Two tries, still quoting figures nobody measured. Keep the draft so it can
    # be inspected, but do not let it into the report.
    section.prose = _plain(section)
    section.prose_verified = False
    section.draft = second
    report.warnings.append(
        f"Section {section.number}: the model quoted figures that were not "
        f"measured ({', '.join(bad[:3])}), so the measurements are shown instead."
    )


def _fit(
    report: Report, section: Section, outline: prompts.Outline, runtime: ModelRuntime
) -> list:
    """Shorten the section's tables until the request fits the model's window."""
    scaffold = prompts.AGENT_SYSTEM + outline.render("")
    budget = runtime.budget(estimate_tokens(scaffold))
    tables = [t for t in section.tables]
    while estimate_tokens(facts_block(section.facts, tables)) > budget:
        longest = max(tables, key=lambda t: len(t.rows), default=None)
        if longest is None or not longest.rows:
            report.warnings.append(
                f"Section {section.number}: the measurements do not fit the "
                "model's context window, so it was written from the facts alone."
            )
            return []
        keep = max(0, len(longest.rows) - _TRIM_STEP)
        # Replace rather than mutate: the report keeps the full table.
        trimmed = [t for t in tables]
        trimmed[tables.index(longest)] = type(longest)(
            title=longest.title,
            columns=longest.columns,
            rows=longest.rows[:keep],
            note=longest.note,
        )
        tables = trimmed
    return tables
