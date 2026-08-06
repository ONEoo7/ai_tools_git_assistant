"""Is this repository tidy, and does the fleet agree with itself?

Two questions that get asked once a project has been running a while, and that
nothing here could answer before:

- **What can I delete?** Branches accumulate. Merged ones are litter; unmerged
  ones are somebody's abandoned work. They look identical in ``git branch``,
  which is why nobody prunes. See ``branches``.
- **Is the fleet consistent?** Several repositories vendoring one submodule
  drift apart, and nothing says so until a build breaks. See ``submodules``.

**This agent has two scopes, and every section says which one it is in.** The
branch sections are about the selected repository. The submodule sections sweep
every repository in the Repositories list -- the only way "how many repositories
use this" can be a number. A stored run must never be mistaken for a fleet-wide
statement about branches, so the section titles carry the scope rather than
leaving it to be inferred.

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
    "abandoned work, and only the first kind is ever proposed. Then sweeps "
    "every repository you have configured for submodules, and reports which "
    "ones are pinned to different versions of the same dependency."
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
            "Seconds for the branches; a few seconds per configured repository "
            "for the submodule sweep. Reads only; changes nothing."
        ),
    )

    def collect(self, ctx: AgentContext) -> Report:
        now = datetime.now(timezone.utc)
        rules = _rules(ctx.settings)

        ctx.say("Reading branches...", 5)
        survey = branches_mod.survey(ctx.repo)
        ctx.check_cancel()

        repos = _fleet_repos(ctx)
        fleet = _sweep(ctx, repos)
        return _build(ctx, survey, rules, fleet, now)


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


def _fleet_repos(ctx: AgentContext) -> list[str]:
    """Every repository to sweep, the selected one included.

    From the configured list rather than from a disk scan: it is the set the
    user has said they care about, and scanning for more would make the
    denominator of "3 of 14" depend on what else is on the drive.
    """
    configured = [r.path for r in getattr(ctx.settings, "repos", []) if r.path]
    if ctx.repo and ctx.repo not in configured:
        configured.insert(0, ctx.repo)
    return configured


def _sweep(ctx: AgentContext, repos: list[str]) -> submodules_mod.Fleet:
    total = max(1, len(repos))
    done = 0

    def on_repo(path: str) -> None:
        nonlocal done
        done += 1
        # 10..95: the branch survey is already behind us, and finishing the
        # sweep is not finishing the report.
        ctx.say(
            f"Submodules: {Path(path).name} ({done}/{total})...",
            10 + int(85 * done / total),
        )

    return submodules_mod.across(repos, on_repo=on_repo, check_cancel=ctx.check_cancel)


# ---- the report -------------------------------------------------------------------
def _build(ctx, survey, rules: StaleRules, fleet, now: datetime) -> Report:
    proposed = survey.proposed(rules, now)
    stale = survey.stale(rules, now)
    kept = survey.kept(rules, now)
    disagreements = fleet.disagreements()
    drifted = fleet.drifted()
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
            count_fact("repos_scanned", "Repositories swept for submodules", len(fleet.scanned)),
            count_fact("submodules_used", "Distinct submodules in use", len(fleet.by_key())),
            count_fact("submodule_disagreements", "Submodules at more than one version", len(disagreements)),
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

    fleet_section = Section(
        number="3",
        title=f"Submodules across {len(fleet.scanned)} configured repositories",
        # No slot: this is a heading over 3.1-3.3, and 3.2 already narrates the
        # finding. A second paragraph here would restate it a line earlier.
        facts=[
            count_fact("repos_scanned", "Repositories swept", len(fleet.scanned)),
            count_fact("submodules_used", "Distinct submodules", len(fleet.by_key())),
            count_fact("submodule_disagreements", "Used at more than one version", len(disagreements)),
        ],
        sections=[
            _usage_section(fleet),
            _disagreement_section(fleet, disagreements),
            _drift_section(drifted),
        ],
    )

    report = Report(
        agent_id=AGENT_ID,
        title="Repository consistency audit",
        subtitle=f"{here} — stale branches, and submodule versions across the fleet",
        generated_at=datetime.now().strftime("%d %B %Y %H:%M"),
        repo_path=ctx.repo,
        sections=[summary, branch_section, fleet_section],
    )
    if survey.problem:
        report.warnings.append(f"Branches could not be read: {survey.problem}")
    report.warnings.extend(fleet.problems)
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


def _usage_section(fleet) -> Section:
    by_key = fleet.by_key()
    rows = []
    for key, uses in sorted(by_key.items(), key=lambda kv: -len(kv[1])):
        versions = fleet.versions_of(key)
        rows.append(
            [
                key,
                str(len(uses)),
                ", ".join(versions),
                ", ".join(sorted({u.repo_name for u in uses})),
            ]
        )
    return Section(
        number="3.1",
        title="Which repositories use what",
        facts=[count_fact("submodules_used", "Distinct submodules", len(by_key))],
        tables=[
            Table(
                title="Submodules by remote",
                columns=["Submodule", "Used by", "Version(s)", "Repositories"],
                rows=rows,
                # Said here because the column would otherwise be read as "the
                # version you have", which is a different and less useful fact.
                note=(
                    "Identified by remote URL, not by path, so one dependency "
                    "vendored at two paths is one row. The version is the commit "
                    "the parent repository pins, described by tags."
                ),
            )
        ]
        if rows
        else [],
    )


def _disagreement_section(fleet, disagreements) -> Section:
    section = Section(
        number="3.2",
        title="Pinned at different versions",
        slot="submodule_disagreements",
        facts=[count_fact("submodule_disagreements", "Submodules", len(disagreements))],
    )
    if not disagreements:
        section.facts.append(fact("agreement", "Every submodule agrees", "yes"))
        return section
    rows = [
        [use.repo_name, key, use.version, use.short_pin()]
        for key, uses in disagreements.items()
        for use in sorted(uses, key=lambda u: u.version)
    ]
    section.tables.append(
        Table(
            title="One dependency, more than one version",
            columns=["Repository", "Submodule", "Version", "Pinned commit"],
            rows=rows,
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
            columns=["Repository", "Submodule", "Pinned", "Checked out"],
            rows=[
                [u.repo_name, u.path, u.short_pin(), u.checked_out[:8]] for u in drifted
            ],
            note=(
                "Invisible until somebody else clones and gets the pinned "
                "commit instead. Commit the submodule pointer, or check the "
                "submodule back out at the commit its parent records."
            ),
        )
    )
    return section
