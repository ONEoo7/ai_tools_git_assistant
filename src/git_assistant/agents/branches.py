"""Which branches have been left behind, and which of those are safe to delete.

The whole module turns on one distinction. A branch merged into the default
branch and untouched for a year is litter: its commits are also on `main`, and
deleting the ref loses nothing. A branch *not* merged and untouched for a year
is somebody's abandoned work, reachable only from that ref -- delete it and the
commits go at the next `gc`. In `git branch` the two look identical, which is
exactly why nobody prunes.

So staleness is measured once and reported twice, and only the merged half is
ever proposed for deletion.

Two git commands for the whole survey, however many branches there are: one
`for-each-ref` for the facts and one `branch --merged` for the verdict. A
per-branch subprocess would make a repository with two hundred branches take
minutes to answer a question that should take a moment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatch

from git_assistant.git_ops import _run

#: Branch names never proposed for deletion, whatever else the rules say.
#: Globs, so `release/*` means what anyone writing it expects.
DEFAULT_PROTECTED: tuple[str, ...] = (
    "main",
    "master",
    "develop",
    "trunk",
    "release/*",
    "hotfix/*",
)

#: Untouched for longer than this and it is stale. Six months is long enough
#: that a branch someone is still thinking about is not swept up.
DEFAULT_MONTHS = 6

#: Roughly. Months are not a unit git has, and "about seven months" is the
#: honest precision for "should this go".
DAYS_PER_MONTH = 30.44

#: How `for-each-ref` is asked, and what each field means. Tab-separated,
#: because a branch name may contain almost anything else.
_FIELDS = (
    "%(refname:short)%09"
    "%(committerdate:iso-strict)%09"
    "%(upstream:short)%09"
    "%(upstream:track)%09"
    "%(HEAD)"
)

_AHEAD = re.compile(r"ahead (\d+)")


@dataclass(frozen=True)
class BranchInfo:
    """One local branch, and everything needed to decide its fate."""

    name: str
    last_commit: datetime | None
    merged: bool = False
    upstream: str = ""
    #: Commits this branch has that its upstream does not. Unpushed work.
    ahead: int = 0
    #: The upstream branch was deleted on the remote. Usually means the pull
    #: request was merged -- but a *squash* merge leaves the branch looking
    #: unmerged here, which is why this is shown and not acted on.
    upstream_gone: bool = False
    is_head: bool = False

    def days_idle(self, now: datetime) -> float:
        if self.last_commit is None:
            return 0.0
        return max(0.0, (now - self.last_commit).total_seconds() / 86400)

    def months_idle(self, now: datetime) -> float:
        return self.days_idle(now) / DAYS_PER_MONTH

    def age_label(self, now: datetime) -> str:
        if self.last_commit is None:
            return "unknown"
        days = self.days_idle(now)
        if days < 45:
            return f"{days:.0f} days ago"
        return f"{days / DAYS_PER_MONTH:.0f} months ago"

    def upstream_label(self) -> str:
        if self.upstream_gone:
            return f"{self.upstream} (gone)" if self.upstream else "gone"
        if not self.upstream:
            return "none"
        return f"{self.upstream} (+{self.ahead} unpushed)" if self.ahead else self.upstream


@dataclass
class StaleRules:
    """When a branch counts as stale, and when deleting one may be proposed.

    Every field widens or narrows what is *proposed*; nothing here deletes
    anything. The defaults are a working configuration on their own, so someone
    who never opens the settings still gets a safe and useful report.
    """

    months: int = DEFAULT_MONTHS
    protect: list[str] = field(default_factory=lambda: list(DEFAULT_PROTECTED))
    #: Never propose a branch whose commits are not already on the default
    #: branch. Turning this off is how you delete work.
    merged_only: bool = True
    #: Never propose a branch holding commits its upstream has not got.
    keep_unpushed: bool = True

    def is_protected(self, name: str, default_branch: str = "") -> bool:
        """The default branch is protected whether or not the list says so.

        A rule file that permits deleting `main` is a rule file with a mistake
        in it, and this is not the place to find that out.
        """
        if default_branch and name == default_branch:
            return True
        return any(fnmatch(name, pattern) for pattern in self.protect)

    def is_stale(self, branch: BranchInfo, now: datetime) -> bool:
        if branch.last_commit is None:
            return False  # nothing to measure; never guessed at
        return branch.months_idle(now) > max(0, self.months)

    def proposes(
        self, branch: BranchInfo, now: datetime, default_branch: str = ""
    ) -> bool:
        """Whether a `git branch -d` line should be offered for this branch."""
        if not self.is_stale(branch, now) or branch.is_head:
            return False
        if self.is_protected(branch.name, default_branch):
            return False
        if self.merged_only and not branch.merged:
            return False
        return not (self.keep_unpushed and branch.ahead > 0)

    # ---- storage ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "months": self.months,
            "protect": list(self.protect),
            "merged_only": self.merged_only,
            "keep_unpushed": self.keep_unpushed,
        }

    @classmethod
    def from_dict(cls, data: object) -> "StaleRules":
        """Rebuild from settings, ignoring anything unusable. Never raises."""
        if not isinstance(data, dict):
            return cls()
        try:
            months = int(data.get("months", DEFAULT_MONTHS))
        except (TypeError, ValueError):
            months = DEFAULT_MONTHS
        raw = data.get("protect")
        protect = (
            [str(p).strip() for p in raw if str(p).strip()]
            if isinstance(raw, list)
            else list(DEFAULT_PROTECTED)
        )
        return cls(
            months=max(0, months),
            protect=protect,
            # Both default to the safe answer, so a hand-edited file that says
            # something unreadable gets the careful behaviour and not the other.
            merged_only=bool(data.get("merged_only", True)),
            keep_unpushed=bool(data.get("keep_unpushed", True)),
        )


@dataclass
class Survey:
    """Every local branch of one repository, and what could not be read."""

    default_branch: str = ""
    branches: list[BranchInfo] = field(default_factory=list)
    problem: str = ""

    def stale(self, rules: StaleRules, now: datetime) -> list[BranchInfo]:
        return [b for b in self.branches if rules.is_stale(b, now)]

    def proposed(self, rules: StaleRules, now: datetime) -> list[BranchInfo]:
        return [
            b
            for b in self.branches
            if rules.proposes(b, now, self.default_branch)
        ]

    def kept(self, rules: StaleRules, now: datetime) -> list[BranchInfo]:
        """Stale, but spared -- so the rules can be seen working."""
        proposed = {b.name for b in self.proposed(rules, now)}
        return [b for b in self.stale(rules, now) if b.name not in proposed]


# ---- reading it out of git ----------------------------------------------------
def default_branch(repo: str) -> str:
    """What this repository considers its trunk.

    What the remote says first, because that is the shared answer rather than
    this checkout's. Then the usual names, then whatever is checked out -- an
    answer is needed for "is this merged", and the current branch is a better
    guess than nothing.
    """
    remote = _run(repo, ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"])
    if remote.ok and remote.stdout.strip():
        return remote.stdout.strip().removeprefix("origin/")
    for name in ("main", "master", "develop", "trunk"):
        if _run(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"]).ok:
            return name
    head = _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    return head.stdout.strip() if head.ok else ""


def survey(repo: str) -> Survey:
    """Every local branch, with its age, its upstream and whether it is merged.

    Never raises. A repository git refuses -- dubious ownership, a bare repo, a
    path that has been moved -- comes back with `problem` set, which the report
    turns into a warning rather than into a count of zero.
    """
    trunk = default_branch(repo)
    listed = _run(repo, ["for-each-ref", f"--format={_FIELDS}", "refs/heads"])
    if not listed.ok:
        return Survey(default_branch=trunk, problem=_first_line(listed.stderr))

    merged = _merged_into(repo, trunk)
    branches = [
        parsed
        for line in listed.stdout.splitlines()
        if (parsed := _branch(line, merged)) is not None
    ]
    return Survey(default_branch=trunk, branches=branches)


def _merged_into(repo: str, trunk: str) -> set[str]:
    """Branches whose commits are all on ``trunk`` already.

    An empty set when there is no trunk to compare against: with no answer,
    nothing is *claimed* to be merged, and `merged_only` then proposes nothing.
    Erring the other way would propose deletions on the strength of a question
    that was never asked.
    """
    if not trunk:
        return set()
    found = _run(repo, ["branch", "--merged", trunk, "--format=%(refname:short)"])
    if not found.ok:
        return set()
    return {line.strip() for line in found.stdout.splitlines() if line.strip()}


def _branch(line: str, merged: set[str]) -> BranchInfo | None:
    parts = line.split("\t")
    if len(parts) < 5 or not parts[0].strip():
        return None
    name, when, upstream, track, head = (p.strip() for p in parts[:5])
    ahead = _AHEAD.search(track)
    return BranchInfo(
        name=name,
        last_commit=_when(when),
        merged=name in merged,
        upstream=upstream,
        ahead=int(ahead.group(1)) if ahead else 0,
        upstream_gone="gone" in track,
        is_head=head == "*",
    )


def _when(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Everything is compared against an aware "now"; a repository written by a
    # tool that omitted the offset would otherwise raise on subtraction.
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _first_line(text: str) -> str:
    lines = (text or "").strip().splitlines()
    return lines[0][:200] if lines else "git could not read this repository"


def delete_commands(branches: list[BranchInfo]) -> str:
    """The block a reader can run. ``-d``, never ``-D``.

    ``-d`` is git's own refusal to lose work, and it is the reason this can be
    offered at all: if something here turns out to be unmerged by the time it is
    run, git declines. That is the correct outcome, not a gap in the rules.
    """
    return "\n".join(f"git branch -d {b.name}" for b in branches)
