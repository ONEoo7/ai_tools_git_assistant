"""Did it get better?

Comparing two runs of one agent on one repository. Pure: no files, no Qt, no
provider -- two reports in, a set of deltas out.

Two rules decide what "better" means, and both are here rather than on the
measurements themselves:

*Direction belongs to the metric, not to the measurement.* A stored run that
carried its own opinion about which way is good would keep asserting it after
that opinion changed, and a comparison would have two answers to one question.
The registry below is consulted at comparison time, so it applies to every run
ever recorded, including the ones written before it said this.

*Growth is not a regression.* A repository that gained history because work
happened has not got worse, and a report that says otherwise stops being read.
Waste -- garbage, leftover packs, unreachable objects -- is always scored. Size
of everything is scored only when both runs sat on the same commit, where a
difference can only be housekeeping.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from enum import StrEnum

from git_assistant.agents.base import Fact, Report, Status
from git_assistant.agents.facts import human_bytes


class Polarity(StrEnum):
    LOWER = "lower"  # less of this is better
    HIGHER = "higher"  # more of this is better
    NEUTRAL = "neutral"  # it moved; that is not an improvement


class Direction(StrEnum):
    BETTER = "better"
    WORSE = "worse"
    SAME = "same"
    NEUTRAL = "neutral"

    def arrow(self) -> str:
        return {"better": "▲", "worse": "▼", "same": "=", "neutral": "·"}[self.value]


@dataclass(frozen=True)
class Metric:
    polarity: Polarity = Polarity.NEUTRAL
    #: Scored only when the two runs sat on the same commit. See the module note.
    needs_same_head: bool = False
    #: Cached into the history index so a list of runs can be drawn without
    #: opening any of them.
    headline: bool = False


_WASTE = Metric(Polarity.LOWER)
_SIZE = Metric(Polarity.LOWER, needs_same_head=True)

#: agent id -> fact key -> how to read a change in it. Keyed by agent first
#: because the same key means different things in different reports.
METRICS: dict[str, dict[str, Metric]] = {
    "size-audit": {
        # Always scored: none of this is work, all of it is waste.
        "garbage_objects": _WASTE,
        "garbage_size": Metric(Polarity.LOWER, headline=True),
        "tmp_packs": _WASTE,
        "tmp_pack_size": _WASTE,
        "unreachable_objects": _WASTE,
        "reclaimable_now": _WASTE,
        "reclaimable_total": Metric(Polarity.LOWER, headline=True),
        # Scored only at the same commit, where a change can only be repacking.
        "git_dir_total": Metric(Polarity.LOWER, needs_same_head=True, headline=True),
        "git_dir_bytes": _SIZE,
        "git_dir_files": _SIZE,
        "history_content": _SIZE,
        "history_packed": _SIZE,
        "history_objects": _SIZE,
        "history_blobs": _SIZE,
        "lfs_cache": _SIZE,
        "lfs_raw_total": _SIZE,
        "dominant_path_total": _SIZE,
    },
    "config-audit": {
        "checks_failed": Metric(Polarity.LOWER, headline=True),
        "checks_warned": Metric(Polarity.LOWER, headline=True),
        "checks_passed": Metric(Polarity.HIGHER, headline=True),
        "to_fix": Metric(Polarity.LOWER),
    },
    # None of these need `needs_same_head`. Unlike repository size, tidiness is
    # not confounded by the work having moved on: a branch merged and deleted
    # since the last run is an improvement whatever else happened.
    "consistency-audit": {
        "stale_merged": Metric(Polarity.LOWER, headline=True),
        "stale_unmerged": Metric(Polarity.LOWER, headline=True),
        "stale_total": Metric(Polarity.LOWER),
        "submodule_disagreements": Metric(Polarity.LOWER, headline=True),
        "submodule_drift": Metric(Polarity.LOWER),
        # Deliberately unscored: vendoring one more dependency is a fact about
        # the project, not a step in either direction.
        # (`Metric()` rather than `_NEUTRAL`, which is defined below this.)
        "submodules_used": Metric(),
    },
}

#: Everything else -- commits, branches, tags, tracked files, submodules -- is
#: reported when it changes and never scored.
_NEUTRAL = Metric()


def metric_for(agent_id: str, key: str) -> Metric:
    return METRICS.get(agent_id, {}).get(key, _NEUTRAL)


def headline_keys(agent_id: str) -> list[str]:
    return [k for k, m in METRICS.get(agent_id, {}).items() if m.headline]


def resolve_polarity(agent_id: str, key: str, *, same_head: bool) -> Polarity:
    metric = metric_for(agent_id, key)
    if metric.needs_same_head and not same_head:
        return Polarity.NEUTRAL
    return metric.polarity


# ---- deltas ------------------------------------------------------------------
@dataclass(frozen=True)
class MetricDelta:
    key: str
    label: str
    before: str  # formatted as that report showed it
    after: str
    before_raw: float | None
    after_raw: float | None
    delta: float | None
    percent: float | None
    polarity: Polarity
    direction: Direction

    def changed(self) -> bool:
        return self.direction is not Direction.SAME

    def change_text(self) -> str:
        """``down 412 MiB (-9.8%)`` -- how the movement should be read aloud."""
        if self.delta is None:
            return "—"
        if self.delta == 0:
            return "no change"
        word = "up" if self.delta > 0 else "down"
        size = _is_size(self.before, self.after)
        amount = human_bytes(abs(self.delta)) if size else f"{abs(self.delta):,.0f}"
        if self.percent is None:
            return f"{word} {amount}"
        return f"{word} {amount} ({self.percent:+.1f}%)"


@dataclass(frozen=True)
class CheckDelta:
    id: str
    title: str
    before: Status | None  # None: the check did not run in the earlier report
    after: Status | None  # None: it no longer runs
    direction: Direction
    headline: str

    def fixed(self) -> bool:
        return self.direction is Direction.BETTER

    def regressed(self) -> bool:
        return self.direction is Direction.WORSE


@dataclass
class RunDiff:
    agent_id: str
    repo_path: str
    before_label: str  # "4 Aug 13:12"
    after_label: str
    before_commit: str  # "main @ 9f2a1c8"
    after_commit: str
    same_head: bool
    dirty: bool  # either side had uncommitted changes
    metrics: list[MetricDelta] = field(default_factory=list)
    context: list[MetricDelta] = field(default_factory=list)
    checks: list[CheckDelta] = field(default_factory=list)

    def counts(self) -> tuple[int, int]:
        better = sum(1 for d in self.scored() if d.direction is Direction.BETTER)
        worse = sum(1 for d in self.scored() if d.direction is Direction.WORSE)
        return better, worse

    def scored(self) -> list:
        return [
            *(m for m in self.metrics if m.direction in (Direction.BETTER, Direction.WORSE)),
            *(c for c in self.checks if c.direction in (Direction.BETTER, Direction.WORSE)),
        ]

    def verdict(self) -> Direction:
        better, worse = self.counts()
        if better and worse:
            return Direction.WORSE if worse > better else Direction.BETTER
        if better:
            return Direction.BETTER
        if worse:
            return Direction.WORSE
        return Direction.SAME

    def summary(self) -> str:
        return summarize(self)


def _is_size(*values: str) -> bool:
    return any(v.endswith(("B", "iB")) for v in values)


def direction_of(
    before_raw: float | None, after_raw: float | None, polarity: Polarity
) -> Direction:
    if before_raw is None or after_raw is None:
        return Direction.NEUTRAL
    if math.isclose(before_raw, after_raw, rel_tol=1e-9, abs_tol=1e-9):
        return Direction.SAME
    if polarity is Polarity.NEUTRAL:
        return Direction.NEUTRAL
    rose = after_raw > before_raw
    better = rose if polarity is Polarity.HIGHER else not rose
    return Direction.BETTER if better else Direction.WORSE


def _ordered_facts(report: Report) -> list[Fact]:
    """Facts in the order the report shows them, first spelling of each key."""
    seen: set[str] = set()
    out: list[Fact] = []
    for section in report.walk():
        for fact in section.facts:
            if fact.key in seen:
                continue
            seen.add(fact.key)
            out.append(fact)
    return out


def diff_metrics(
    before: Report, after: Report, *, same_head: bool
) -> tuple[list[MetricDelta], list[MetricDelta]]:
    """``(measurements, context)`` -- numbers that can be scored, and the rest."""
    old = {f.key: f for f in _ordered_facts(before)}
    metrics: list[MetricDelta] = []
    context: list[MetricDelta] = []
    for fact in _ordered_facts(after):
        was = old.pop(fact.key, None)
        delta = _delta(after.agent_id, was, fact, same_head=same_head)
        if fact.raw is None or (was is not None and was.raw is None):
            # Not a quantity: a branch name, a version, a path. Worth showing
            # when it moved, never worth scoring.
            if delta.before != delta.after:
                context.append(delta)
        else:
            metrics.append(delta)
    for key, was in old.items():  # measured before, absent now
        metrics.append(_delta(after.agent_id, was, None, same_head=same_head))
    return metrics, context


def _delta(
    agent_id: str, was: Fact | None, now: Fact | None, *, same_head: bool
) -> MetricDelta:
    key = (now or was).key
    polarity = resolve_polarity(agent_id, key, same_head=same_head)
    before_raw = was.raw if was else None
    after_raw = now.raw if now else None
    change = (
        after_raw - before_raw
        if isinstance(before_raw, (int, float)) and isinstance(after_raw, (int, float))
        else None
    )
    percent = (
        100.0 * change / before_raw
        if change is not None and before_raw not in (None, 0)
        else None
    )
    return MetricDelta(
        key=key,
        label=(now or was).label,
        before=was.value if was else "—",
        after=now.value if now else "—",
        before_raw=before_raw,
        after_raw=after_raw,
        delta=change,
        percent=percent,
        polarity=polarity,
        direction=direction_of(before_raw, after_raw, polarity),
    )


# ---- configuration checks ------------------------------------------------------
_BY_LABEL = {
    "FAIL": Status.FAIL,
    "WARN": Status.WARN,
    "PASS": Status.PASS,
    "n/a": Status.SKIP,
}
_STATUS_SUFFIX = "_status"
_FINDING_SUFFIX = "_finding"


def check_statuses(report: Report) -> dict[str, tuple[Status, str, str]]:
    """``id -> (status, title, finding)``.

    Prefers the verdicts the report carries. Falls back to reading them back out
    of the finding sections, which is how a run recorded before reports carried
    them can still be compared.
    """
    if report.checks:
        return {c.id: (c.status, c.title, c.headline) for c in report.checks}

    out: dict[str, tuple[Status, str, str]] = {}
    for section in report.walk():
        facts = {f.key: f.value for f in section.facts}
        for key, value in facts.items():
            if not key.endswith(_STATUS_SUFFIX):
                continue
            check_id = key[: -len(_STATUS_SUFFIX)]
            status = _BY_LABEL.get(value)
            if status is None:
                continue
            title = section.title.split(" — ", 1)[-1]
            out[check_id] = (status, title, facts.get(check_id + _FINDING_SUFFIX, ""))
    return out


def _check_direction(before: Status | None, after: Status | None) -> Direction:
    if before is after:
        return Direction.SAME  # including skip -> skip, which is not a change
    # A check that stopped applying has not been fixed, and one that started
    # applying has not regressed -- either way the question changed, not the answer.
    if before is None or after is None or Status.SKIP in (before, after):
        return Direction.NEUTRAL
    rank = {Status.FAIL: 0, Status.WARN: 1, Status.PASS: 2}
    return Direction.BETTER if rank[after] > rank[before] else Direction.WORSE


def diff_checks(before: Report, after: Report) -> list[CheckDelta]:
    old = check_statuses(before)
    new = check_statuses(after)
    deltas: list[CheckDelta] = []
    for check_id in [*new, *(k for k in old if k not in new)]:
        was = old.get(check_id)
        now = new.get(check_id)
        direction = _check_direction(was[0] if was else None, now[0] if now else None)
        if direction is Direction.SAME:
            continue  # a wall of unchanged PASSes is not a comparison
        deltas.append(
            CheckDelta(
                id=check_id,
                title=(now or was)[1],
                before=was[0] if was else None,
                after=now[0] if now else None,
                direction=direction,
                headline=(now or was)[2],
            )
        )
    order = {Direction.WORSE: 0, Direction.BETTER: 1, Direction.NEUTRAL: 2}
    deltas.sort(key=lambda d: (order[d.direction], d.id))
    return deltas


# ---- the comparison -------------------------------------------------------------
def diff(before, after) -> RunDiff | None:
    """Compare two stored runs. ``None`` when they are not comparable.

    ``before`` and ``after`` are anything with ``.report``, ``.agent_id``,
    ``.repo_path``, ``.head``, ``.dirty`` and ``.when_label()`` -- in practice
    ``history.StoredRun``.
    """
    if before.agent_id != after.agent_id:
        return None
    if before.report is None or after.report is None:
        return None
    same_head = bool(before.head) and before.head == after.head
    metrics, context = diff_metrics(before.report, after.report, same_head=same_head)
    return RunDiff(
        agent_id=after.agent_id,
        repo_path=after.repo_path,
        before_label=before.when_label(),
        after_label=after.when_label(),
        before_commit=before.report.commit_line() or "unknown commit",
        after_commit=after.report.commit_line() or "unknown commit",
        same_head=same_head,
        dirty=bool(before.dirty or after.dirty),
        metrics=metrics,
        context=context,
        checks=diff_checks(before.report, after.report),
    )


def _biggest(deltas: list[MetricDelta]) -> MetricDelta | None:
    scored = [d for d in deltas if d.direction in (Direction.BETTER, Direction.WORSE)]
    if not scored:
        return None
    return max(scored, key=lambda d: (abs(d.percent or 0), abs(d.delta or 0)))


def summarize(diff_: RunDiff) -> str:
    """One line: did it improve, since when, and what moved most."""
    better, worse = diff_.counts()
    where = "at the same commit" if diff_.same_head else (
        f"{diff_.before_commit} → {diff_.after_commit}"
    )
    verdict = {
        Direction.BETTER: "Improved",
        Direction.WORSE: "Regressed",
        Direction.SAME: "No change",
    }[diff_.verdict()]
    if better and worse:
        verdict = "Mixed"

    parts: list[str] = []
    fixed = [c for c in diff_.checks if c.fixed()]
    broke = [c for c in diff_.checks if c.regressed()]
    if broke:
        parts.append(f"{', '.join(c.id for c in broke[:3])} regressed")
    if fixed:
        parts.append(f"{len(fixed)} check(s) fixed ({', '.join(c.id for c in fixed[:3])})")
    biggest = _biggest(diff_.metrics)
    if biggest is not None:
        parts.append(f"{biggest.label.lower()} {biggest.change_text()}")
    if not parts:
        parts.append("nothing measured moved")

    line = f"{verdict} since {diff_.before_label} ({where}): {'; '.join(parts)}."
    if diff_.dirty:
        # Otherwise the comparison quietly credits uncommitted edits to a commit.
        line += " One of the runs had uncommitted changes."
    return line


# ---- rendering ------------------------------------------------------------------
def to_markdown(diff_: RunDiff) -> str:
    out = ["# Comparison", "", summarize(diff_), ""]
    out += [
        f"- Before: {diff_.before_label} — {diff_.before_commit}",
        f"- After: {diff_.after_label} — {diff_.after_commit}",
        "",
    ]
    if diff_.checks:
        out += ["## Checks that changed", "", "| Check | Was | Now | Finding |", "| --- | --- | --- | --- |"]
        out += [
            f"| {c.id} {c.title} | {_status(c.before)} | {_status(c.after)} | {c.headline} |"
            for c in diff_.checks
        ]
        out.append("")
    changed = [m for m in diff_.metrics if m.changed()]
    if changed:
        out += ["## Measurements", "", "| Measurement | Before | After | Change | |", "| --- | --- | --- | --- | --- |"]
        out += [
            f"| {m.label} | {m.before} | {m.after} | {m.change_text()} | {m.direction.arrow()} |"
            for m in changed
        ]
        out.append("")
    unchanged = len(diff_.metrics) - len(changed)
    if unchanged:
        out += [f"*and {unchanged} measurement(s) unchanged*", ""]
    if diff_.context:
        out += ["## Other changes", "", "| Item | Before | After |", "| --- | --- | --- |"]
        out += [f"| {m.label} | {m.before} | {m.after} |" for m in diff_.context]
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _status(status: Status | None) -> str:
    return status.label() if status is not None else "—"


def to_html(diff_: RunDiff) -> str:
    from git_assistant.agents.report import _table_tag

    e = html.escape
    out = [
        "<html><body>",
        f"<h2>Comparison</h2><p><b>{e(summarize(diff_))}</b></p>",
        f"<p>{e(diff_.before_label)} — {e(diff_.before_commit)}<br>"
        f"{e(diff_.after_label)} — {e(diff_.after_commit)}</p>",
    ]
    if diff_.checks:
        rows = "".join(
            f"<tr><td>{e(c.id)} {e(c.title)}</td><td>{_status(c.before)}</td>"
            f"<td>{_status(c.after)}</td><td>{e(c.headline)}</td></tr>"
            for c in diff_.checks
        )
        out.append("<h3>Checks that changed</h3>")
        out.append(_table_tag(["Check", "Was", "Now", "Finding"], rows))
    changed = [m for m in diff_.metrics if m.changed()]
    if changed:
        rows = "".join(
            f"<tr><td>{e(m.label)}</td><td>{e(m.before)}</td><td>{e(m.after)}</td>"
            f"<td>{e(m.change_text())}</td><td>{m.direction.arrow()}</td></tr>"
            for m in changed
        )
        out.append("<h3>Measurements</h3>")
        out.append(_table_tag(["Measurement", "Before", "After", "Change", ""], rows))
    unchanged = len(diff_.metrics) - len(changed)
    if unchanged:
        out.append(f"<p><i>and {unchanged} measurement(s) unchanged</i></p>")
    if diff_.context:
        rows = "".join(
            f"<tr><td>{e(m.label)}</td><td>{e(m.before)}</td><td>{e(m.after)}</td></tr>"
            for m in diff_.context
        )
        out.append("<h3>Other changes</h3>")
        out.append(_table_tag(["Item", "Before", "After"], rows))
    out.append("</body></html>")
    return "\n".join(out)
