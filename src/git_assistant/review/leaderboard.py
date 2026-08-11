"""Which models are any good at reviewing code, kept across runs.

    <config dir>/code_review/leaderboard.json

Beside the per-language rule files, because it is the same subject: what a
review is checked against, and how well the checking went.

One row per **reviewed model and judge model together**, never per reviewed
model alone. A score is one model's opinion of another's work, so a 7 from Opus
and a 7 from a 4B local model are not the same measurement and averaging them
produces a number that means nothing. Changing judge starts a fresh comparison,
which is the honest thing for it to do.

`total` is kept beside `mean` so a finished run folds in with arithmetic rather
than a re-read of every review ever stored -- the history is pruned and the
leaderboard is not, so recomputing from it would lose the early runs.

Reading never raises and a damaged file reads as an empty board. A leaderboard
is a record of opinions about work already done; losing it must never be able to
fail the review that was about to be recorded in it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from git_assistant.review.history import replace_atomically
from git_assistant.review.rule_files import rules_dir

SCHEMA_VERSION = 1
LEADERBOARD_FILE = "leaderboard.json"


def path() -> Path:
    """Where the board lives.

    Through `rule_files.rules_dir()` rather than resolving the config directory
    again, so redirecting that one module in a test moves this file too --
    which is what `tests/conftest.py` already does for the rule files.
    """
    return rules_dir() / LEADERBOARD_FILE


@dataclass
class Row:
    """One reviewed model, as scored by one judge."""

    provider: str = ""
    model: str = ""
    judge_provider: str = ""
    judge_model: str = ""
    #: Runs folded in, files scored across them, and the sum of those files'
    #: scores. `mean` is derived and stored anyway, because it is what the table
    #: sorts on and recomputing it per sort is arithmetic nobody needs twice.
    runs: int = 0
    files: int = 0
    total: float = 0.0
    mean: float = 0.0
    #: How long the reviewer's calls took across those files, added up, and the
    #: mean per file. Per call rather than wall clock: files are reviewed
    #: several at a time, so elapsed time measures the worker count, not the
    #: model. Kept as a running total for the same reason `total` is.
    seconds: float = 0.0
    secs_per_file: float = 0.0
    last: str = ""  # ISO-8601 UTC

    def key(self) -> tuple[str, str, str, str]:
        return (self.provider, self.model, self.judge_provider, self.judge_model)

    def label(self) -> str:
        return f"{self.model or '?'} ({self.provider or '?'})"

    def judge_label(self) -> str:
        return f"{self.judge_model or '?'} ({self.judge_provider or '?'})"


@dataclass
class Board:
    """Every row there is, newest scoring last."""

    rows: list[Row] = field(default_factory=list)

    def find(self, key: tuple[str, str, str, str]) -> Row | None:
        return next((one for one in self.rows if one.key() == key), None)

    def ranked(self) -> list[Row]:
        """Best first. Ties broken by the more-measured row, then by name."""
        return sorted(self.rows, key=lambda r: (-r.mean, -r.files, r.model.lower()))


def _row(data: object) -> Row | None:
    if not isinstance(data, dict) or not str(data.get("model", "")).strip():
        return None
    try:
        return Row(
            provider=str(data.get("provider", "")),
            model=str(data.get("model", "")),
            judge_provider=str(data.get("judge_provider", "")),
            judge_model=str(data.get("judge_model", "")),
            runs=int(data.get("runs", 0) or 0),
            files=int(data.get("files", 0) or 0),
            total=float(data.get("total", 0.0) or 0.0),
            mean=float(data.get("mean", 0.0) or 0.0),
            seconds=float(data.get("seconds", 0.0) or 0.0),
            secs_per_file=float(data.get("secs_per_file", 0.0) or 0.0),
            last=str(data.get("last", "")),
        )
    except (TypeError, ValueError):
        return None  # a row somebody hand-edited into nonsense, not a crash


def load() -> Board:
    """The board on disk, or an empty one. Never raises."""
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return Board()
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return Board()
    return Board(rows=[one for one in (_row(r) for r in rows) if one is not None])


def save(board: Board) -> str:
    """Write the board. Returns a problem to report, or `""`.

    Reported rather than raised: this is called as a review finishes, and a
    review that succeeded must not be reported as failed because a scoreboard
    could not be written.
    """
    payload = {"version": SCHEMA_VERSION, "rows": [asdict(one) for one in board.rows]}
    destination = path()
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp = destination.with_name(f"{destination.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        replace_atomically(tmp, destination)
    except OSError as exc:
        return f"the leaderboard could not be written ({exc})"
    return ""


def record(
    *,
    provider: str,
    model: str,
    judge_provider: str,
    judge_model: str,
    scores: list[float],
    seconds: float = 0.0,
    when: str = "",
) -> str:
    """Fold one run's scores into the board. Returns a problem, or `""`.

    `scores` is one number per file that was actually scored -- a file whose
    judge failed contributes nothing rather than a zero, which is decided
    before this is called; see `review.judge`. `seconds` is how long the
    reviewer took over those same files, so the two columns describe the same
    set and a mean time cannot be about files that were never scored.

    A run that scored nothing is not recorded at all. Counting it as a run with
    no files would move the "runs" column without moving the mean, which reads
    as a model being measured when it was not.
    """
    if not scores or not model:
        return ""

    board = load()
    key = (provider, model, judge_provider, judge_model)
    row = board.find(key)
    if row is None:
        row = Row(
            provider=provider,
            model=model,
            judge_provider=judge_provider,
            judge_model=judge_model,
        )
        board.rows.append(row)

    row.runs += 1
    row.files += len(scores)
    row.total = round(row.total + sum(scores), 4)
    row.mean = round(row.total / row.files, 4) if row.files else 0.0
    row.seconds = round(row.seconds + max(0.0, seconds), 3)
    row.secs_per_file = round(row.seconds / row.files, 3) if row.files else 0.0
    row.last = when or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return save(board)


def clear() -> str:
    """Throw the board away. Returns a problem, or `""`."""
    try:
        path().unlink(missing_ok=True)
    except OSError as exc:
        return f"the leaderboard could not be removed ({exc})"
    return ""
