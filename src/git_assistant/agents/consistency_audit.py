"""Is this repository tidy, and does it agree with itself?

Two questions that get asked once a project has been running a while, and that
nothing here could answer before:

- **What can I delete?** Branches accumulate. Merged ones are litter; unmerged
  ones are somebody's abandoned work. They look identical in ``git branch``,
  which is why nobody prunes. See ``branches``.
- **What is it pinned to?** A submodule's version is the commit its parent
  records, and a working tree left somewhere else is invisible until somebody
  clones and gets something different. See ``submodules``.

**Everything here is about the selected repository, and nothing else.** The
Repositories list is not swept: a report headed with one repository's name that
silently measured fourteen others is a report whose numbers cannot be read, and
selecting a different repository has to change the answer.

Nothing here changes a repository. The delete commands are printed to be read
and run, for the reason the configuration audit gives about its own: that is a
decision, not a button.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from git_assistant.agents import branches as branches_mod
from git_assistant.agents import submodules as submodules_mod
from git_assistant.agents.base import (
    AgentContext,
    AgentInfo,
    Report,
    Section,
    Table,
)
from git_assistant.agents.branches import StaleRules
from git_assistant.agents.facts import count_fact, fact

DESCRIPTION = (
    "Finds branches nobody has touched for months and says which of them are "
    "safe to delete -- merged ones are litter, unmerged ones are somebody's "
    "abandoned work, and only the first kind is ever proposed. Then reads the "
    "selected repository's submodules: what each one is pinned to, and where a "
    "working tree has drifted off the commit that would be cloned."
)

AGENT_ID = "consistency-audit"

#: Said in the unmerged section, in place of commands there deliberately are not.
NO_COMMANDS = (
    "No delete commands are offered for these. Their commits are not on the "
    "default branch, so the only copy is the branch itself. A branch whose "
    "upstream is marked (gone) was probably squash-merged -- git cannot tell "
    "that from abandoned work, and neither can this."
)


class ConsistencyAuditAgent:
    info = AgentInfo(
        id=AGENT_ID,
        label="Repository consistency audit",
        description=DESCRIPTION,
        cost_hint=(
            "Seconds for the branches; a second or two per submodule of the "
            "selected repository. Reads only; changes nothing."
        ),
    )

    def collect(self, ctx: AgentContext) -> Report:
        now = datetime.now(timezone.utc)
        rules = _rules(ctx.settings)

        ctx.say("Reading branches...", 5)
        survey = branches_mod.survey(ctx.repo)
        ctx.check_cancel()

        ctx.say("Reading submodules...", 55)
        found = submodules_mod.across([ctx.repo], check_cancel=ctx.check_cancel)
        ctx.say("Writing the report...", 95)
        return _build(ctx, survey, rules, found, now)


def _rules(settings) -> StaleRules:
    """The configured rules, or the safe defaults. Never raises.

    Accepts a `StaleRules` directly as well, so a test -- or a caller that has
    already built one -- does not have to go through a dict to get here.
    """
    raw = getattr(settings, "stale_branch_rules", None)
    if isinstance(raw, StaleRules):
        return raw
    reader = getattr(settings, "stale_rules", None)
    return reader() if callable(reader) else StaleRules.from_dict(raw)


# ---- the report -------------------------------------------------------------------
def _build(ctx, survey, rules: StaleRules, found, now: datetime) -> Report:
    proposed = survey.proposed(rules, now)
    stale = survey.stale(rules, now)
    kept = survey.kept(rules, now)
    disagreements = found.disagreements()
    drifted = found.drifted()
    here = Path(ctx.repo).name or ctx.repo

    summary = Section(
        number="1",
        title="Executive summary",
        slot="consistency_summary",
        facts=[
            count_fact("branches_total", f"Local branches in {here}", len(survey.branches)),
            count_fact("stale_total", f"Untouched for over {rules.months} months", len(stale)),
            count_fact("stale_merged", "Stale and merged (safe to delete)", len(proposed)),
            count_fact("stale_unmerged", "Stale and unmerged (work would be lost)", len(kept)),
            count_fact("submodules_used", "Submodules declared", len(found.uses)),
            count_fact("submodule_disagreements", "Dependencies at more than one version", len(disagreements)),
            count_fact("submodule_drift", "Submodules off their pinned commit", len(drifted)),
        ],
    )

    branch_section = Section(
        number="2",
        title=f"Branches in {here}",
        facts=[
            fact("default_branch", "Default branch", survey.default_branch or "unknown"),
            fact("stale_after", "Counted as stale after", f"{rules.months} months"),
        ],
        sections=[
            _proposed_section(proposed, now),
            _kept_section(kept, now),
            _protected_section(survey, rules, now),
        ],
    )

    submodule_section = Section(
        number="3",
        title=f"Submodules in {here}",
        # No slot: this is a heading over 3.1-3.3, and 3.2 already narrates the
        # finding. A second paragraph here would restate it a line earlier.
        facts=[
            count_fact("submodules_used", "Submodules declared", len(found.uses)),
            count_fact("submodule_disagreements", "Pinned at more than one version", len(disagreements)),
            count_fact("submodule_drift", "Off their pinned commit", len(drifted)),
        ],
        sections=[
            _usage_section(found),
            _disagreement_section(disagreements),
            _drift_section(drifted),
        ],
    )

    report = Report(
        agent_id=AGENT_ID,
        title="Repository consistency audit",
        subtitle=f"{here} — stale branches, and what its submodules are pinned to",
        generated_at=datetime.now().strftime("%d %B %Y %H:%M"),
        repo_path=ctx.repo,
        sections=[summary, branch_section, submodule_section],
    )
    if survey.problem:
        report.warnings.append(f"Branches could not be read: {survey.problem}")
    report.warnings.extend(found.problems)
    return report


def _proposed_section(proposed, now) -> Section:
    section = Section(
        number="2.1",
        title="Stale and merged — safe to delete",
        facts=[count_fact("stale_merged", "Branches", len(proposed))],
    )
    if not proposed:
        section.facts.append(fact("proposed_none", "Nothing to delete", "none"))
        return section
    section.tables.append(
        Table(
            title="Merged into the default branch and untouched",
            columns=["Branch", "Last commit", "Upstream"],
            rows=[[b.name, b.age_label(now), b.upstream_label()] for b in proposed],
            note=(
                "Their commits are already on the default branch, so deleting "
                "the branch loses nothing."
            ),
        )
    )
    section.commands.append(
        (
            "Delete them — `-d` refuses anything that turns out to be unmerged:",
            branches_mod.delete_commands(proposed),
        )
    )
    return section


def _kept_section(kept, now) -> Section:
    section = Section(
        number="2.2",
        title="Stale and unmerged — nothing is proposed",
        facts=[count_fact("stale_unmerged", "Branches", len(kept))],
    )
    if not kept:
        return section
    section.tables.append(
        Table(
            title="Untouched, and not on the default branch",
            columns=["Branch", "Last commit", "Merged", "Upstream"],
            rows=[
                [b.name, b.age_label(now), "yes" if b.merged else "no", b.upstream_label()]
                for b in kept
            ],
            note=NO_COMMANDS,
        )
    )
    return section


def _protected_section(survey, rules: StaleRules, now) -> Section:
    """What the rules spared, so the rules can be seen working."""
    spared = [
        b
        for b in survey.branches
        if rules.is_protected(b.name, survey.default_branch)
    ]
    return Section(
        number="2.3",
        title="Protected by the rules",
        facts=[
            count_fact("protected", "Branches never proposed", len(spared)),
            fact("protect_patterns", "Patterns", ", ".join(rules.protect) or "none"),
        ],
        tables=[
            Table(
                title="Spared whatever their age",
                columns=["Branch", "Last commit"],
                rows=[[b.name, b.age_label(now)] for b in spared],
            )
        ]
        if spared
        else [],
    )


def _usage_section(found) -> Section:
    section = Section(
        number="3.1",
        title="What this repository vendors",
        facts=[count_fact("submodules_used", "Submodules declared", len(found.uses))],
    )
    if not found.uses:
        section.facts.append(fact("submodules_none", "No submodules declared", "none"))
        return section
    section.tables.append(
        Table(
            title="Submodules, and the commit each one is pinned to",
            columns=["Path", "Remote", "Version", "Pinned commit"],
            rows=[
                [u.path, u.key, u.version, u.short_pin()]
                for u in sorted(found.uses, key=lambda u: u.path)
            ],
            # Said here because the column would otherwise be read as "the
            # version you have", which is a different and less useful fact.
            note=(
                "The version is the commit this repository pins, read from its "
                "HEAD tree and described by tags -- not whatever the working "
                "tree happens to be checked out at."
            ),
        )
    )
    return section


def _disagreement_section(disagreements) -> Section:
    """One remote vendored at two paths, pinned to different commits.

    Rare, and worth a section anyway: nothing in ``git status`` says the two
    copies of one dependency are not the same copy, and a build that links both
    is the first thing that does. Paths are compared by remote URL, so the same
    dependency at ``vendor/lib`` and ``third_party/lib`` is one dependency.
    """
    section = Section(
        number="3.2",
        title="The same dependency at more than one version",
        slot="submodule_disagreements",
        facts=[count_fact("submodule_disagreements", "Dependencies", len(disagreements))],
    )
    if not disagreements:
        section.facts.append(fact("agreement", "Every submodule agrees", "yes"))
        return section
    rows = [
        [key, use.path, use.version, use.short_pin()]
        for key, uses in disagreements.items()
        for use in sorted(uses, key=lambda u: u.version)
    ]
    section.tables.append(
        Table(
            title="One remote, vendored twice, pinned differently",
            columns=["Remote", "Path", "Version", "Pinned commit"],
            rows=rows,
            note=(
                "Identified by remote URL, not by path, so one dependency "
                "vendored at two paths is one dependency."
            ),
        )
    )
    return section


def _drift_section(drifted) -> Section:
    section = Section(
        number="3.3",
        title="Working tree away from the pinned commit",
        facts=[count_fact("submodule_drift", "Submodules", len(drifted))],
    )
    if not drifted:
        return section
    section.tables.append(
        Table(
            title="What you have is not what you would clone",
            columns=["Submodule", "Pinned", "Checked out"],
            rows=[[u.path, u.short_pin(), u.checked_out[:8]] for u in drifted],
            note=(
                "Invisible until somebody else clones and gets the pinned "
                "commit instead. Commit the submodule pointer, or check the "
                "submodule back out at the commit its parent records."
            ),
        )
    )
    return section
