"""What an agent is, and what it produces.

An agent inspects one repository and returns a :class:`Report`: numbered
sections holding facts, tables and remediation commands. Collection is
deterministic -- every figure is measured by git or the filesystem, never by a
model -- so a report is complete and renderable before any provider is
contacted. Prose is added afterwards by ``narrator``, which is only allowed to
use figures that are already in the report.

The shape mirrors ``CommitGenerator``: a long job takes ``progress`` and
``is_cancelled`` callables and raises :class:`CancelledError`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from git_assistant.config import Settings

ProgressFn = Callable[[str], None]
PctFn = Callable[[int], None]
CancelFn = Callable[[], bool]

#: Percentage value meaning "running, but the total is not known yet".
INDETERMINATE = -1


class CancelledError(RuntimeError):
    """Raised when an agent run is cancelled cooperatively.

    Distinct from ``commit_generator.CancelledError``: the two subsystems are
    cancelled independently, and a worker catches the one it can raise.
    """


def _noop(_: str) -> None:
    pass


def _noop_pct(_: int) -> None:
    pass


def _never() -> bool:
    return False


@dataclass(frozen=True)
class AgentInfo:
    """What the tab shows about an agent before it is run."""

    id: str  # "size-audit"
    label: str  # "Repository size audit"
    description: str  # one paragraph, shown under the agent list
    cost_hint: str  # "seconds" | "minutes on a large repository"


@dataclass
class Fact:
    """One measurement, pre-formatted.

    ``value`` is the string that may appear in the report and in the model's
    prose; the narrator checks the model's figures against exactly these, so
    formatting here is what makes that check possible.
    """

    key: str
    label: str
    value: str
    raw: int | float | None = None


@dataclass
class Table:
    title: str
    columns: list[str]
    rows: list[list[str]]  # every cell already formatted
    note: str = ""


@dataclass
class Section:
    number: str  # "1", "2.1"
    title: str
    slot: str = ""  # narration slot id; "" means no prose is written for it
    prose: str = ""
    #: False when the model wrote a figure that is not in this section's facts
    #: and the deterministic fallback was kept instead.
    prose_verified: bool = True
    draft: str = ""  # the rejected model draft, kept so it can be inspected
    facts: list[Fact] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    commands: list[tuple[str, str]] = field(default_factory=list)  # caption, block
    sections: list["Section"] = field(default_factory=list)

    def walk(self) -> Iterator["Section"]:
        yield self
        for child in self.sections:
            yield from child.walk()


@dataclass
class Report:
    agent_id: str
    title: str
    subtitle: str  # the repository this is about
    generated_at: str
    repo_path: str
    #: The point in the repository's history this describes. Sampled before the
    #: scan starts, because a scan can take minutes and the repository does not
    #: hold still for it. Without this a stored report says what was true, but
    #: not what it was true of.
    head: str = ""
    branch: str = ""
    dirty: bool = False  # the work tree had uncommitted changes
    sections: list[Section] = field(default_factory=list)
    #: Populated by the configuration audit. The report's own sections carry
    #: the same verdicts for a reader; these carry them for a comparison.
    checks: list["CheckResult"] = field(default_factory=list)
    #: Collection problems worth showing: a git command that refused, facts
    #: trimmed to fit the model's context, narration that could not run.
    warnings: list[str] = field(default_factory=list)

    def walk(self) -> Iterator[Section]:
        for section in self.sections:
            yield from section.walk()

    def find(self, number: str) -> Section | None:
        return next((s for s in self.walk() if s.number == number), None)

    def facts_by_key(self) -> dict[str, Fact]:
        return {f.key: f for s in self.walk() for f in s.facts}

    def commit_line(self) -> str:
        """``main @ 9f2a1c8 (uncommitted changes)`` -- what this describes."""
        if not self.head:
            return ""
        where = f"{self.branch} @ {self.head[:7]}" if self.branch else self.head[:7]
        return f"{where} (uncommitted changes)" if self.dirty else where


class Status(StrEnum):
    """Outcome of a single configuration check."""

    FAIL = "fail"
    WARN = "warn"
    PASS = "pass"
    SKIP = "skip"

    def label(self) -> str:
        return {"fail": "FAIL", "warn": "WARN", "pass": "PASS", "skip": "n/a"}[
            self.value
        ]


#: Sort key: what is broken first, then what is risky, then what is fine.
_STATUS_ORDER = {Status.FAIL: 0, Status.WARN: 1, Status.PASS: 2, Status.SKIP: 3}


@dataclass
class CheckResult:
    """One configuration check, its verdict and the evidence behind it."""

    id: str  # "EOL-02"
    title: str
    status: Status
    headline: str  # one deterministic sentence
    evidence: list[str] = field(default_factory=list)
    remediation: str = ""  # static text authored here, never generated
    weight: int = 2  # 3 correctness, 2 portability, 1 hygiene

    def sort_key(self) -> tuple[int, int, str]:
        return (_STATUS_ORDER[self.status], -self.weight, self.id)


@dataclass
class AgentContext:
    """Everything an agent needs to run, and the two ways it reports back."""

    repo: str
    settings: Settings
    progress: ProgressFn = _noop
    progress_pct: PctFn = _noop_pct
    is_cancelled: CancelFn = _never
    #: Skip the expensive per-path history scan (totals only).
    fast: bool = False

    def check_cancel(self) -> None:
        if self.is_cancelled():
            raise CancelledError("Cancelled.")

    def say(self, message: str, pct: int = INDETERMINATE) -> None:
        self.progress(message)
        self.progress_pct(pct)


class Agent(Protocol):
    """Collect facts about one repository. No model involved."""

    info: AgentInfo

    def collect(self, ctx: AgentContext) -> Report: ...
