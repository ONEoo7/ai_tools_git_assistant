"""Keeping generated commit messages, so a better earlier one is not lost.

Regenerating is the normal thing to do: the second message is often worse than
the first, and until now the first was gone. Every generated message is recorded
per repository, with what produced it -- strategy, chunk count, the branch and
commit it described -- and whether it was the one that became a commit.

    <config dir>/commit_runs/<repo key>.json

One file per repository, unlike the audit and review stores. Those split the
index from the records because a report is tens of kilobytes and drawing a list
of twenty must not read all twenty. A commit message is a kilobyte; the whole
history of a repository is smaller than one audit, so the file *is* the index.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant.config import APP_NAME, repo_key

SCHEMA_VERSION = 1
RUNS_DIR = "commit_runs"
#: Newest N kept per repository (0 keeps everything).
DEFAULT_LIMIT = 20
#: A message longer than this is not a commit message; refuse to store the rest.
MAX_MESSAGE = 20_000


def runs_root() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False)) / RUNS_DIR


def runs_path(repo_path: str) -> Path:
    return runs_root() / f"{repo_key(repo_path)}.json"


@dataclass
class StoredMessage:
    """One generated message, and what produced it."""

    run_id: str
    repo_path: str
    started_at: str  # ISO-8601 UTC, sortable
    message: str = ""
    branch: str = ""
    head: str = ""
    dirty: bool = False
    strategy: str = ""  # "single-shot" | "map-reduce"
    num_chunks: int = 1
    input_tokens: int = 0
    context_window: int = 0
    model: str = ""
    provider: str = ""
    committed: bool = False  # this is the message that became a commit
    pinned: bool = False

    def when(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        except ValueError:
            return None

    def when_label(self) -> str:
        """Local time, short: ``5 Aug 13:12``, with the year when it is not this one."""
        moment = self.when()
        if moment is None:
            return self.started_at or "unknown"
        local = moment.astimezone()
        if local.year != datetime.now().astimezone().year:
            return local.strftime("%d %b %Y %H:%M")
        return local.strftime("%d %b %H:%M")

    def subject(self) -> str:
        """The first line -- what a list of messages is actually scanned by."""
        return next((line for line in self.message.splitlines() if line.strip()), "")

    def commit_label(self) -> str:
        where = f"{self.branch} @ {self.head[:7]}" if self.head else "unknown commit"
        return f"{where}*" if self.dirty else where

    def result_label(self) -> str:
        mark = "committed - " if self.committed else ""
        return f"{mark}{self.subject()}"

    def describe(self) -> str:
        bits = [f"{self.when_label()} - {self.commit_label()}"]
        if self.strategy:
            chunks = f", {self.num_chunks} chunk(s)" if self.num_chunks > 1 else ""
            bits.append(f"{self.strategy}{chunks}")
        if self.model:
            bits.append(f"model: {self.model} ({self.provider})")
        if self.committed:
            bits.append("this message was committed")
        if self.pinned:
            bits.append("pinned: kept regardless of the retention limit")
        return "\n".join(bits)


def _from_dict(data: dict) -> StoredMessage | None:
    if not isinstance(data, dict) or not data.get("run_id"):
        return None
    return StoredMessage(
        run_id=str(data["run_id"]),
        repo_path=str(data.get("repo_path", "")),
        started_at=str(data.get("started_at", "")),
        message=str(data.get("message", ""))[:MAX_MESSAGE],
        branch=str(data.get("branch", "")),
        head=str(data.get("head", "")),
        dirty=bool(data.get("dirty", False)),
        strategy=str(data.get("strategy", "")),
        num_chunks=int(data.get("num_chunks", 1) or 1),
        input_tokens=int(data.get("input_tokens", 0) or 0),
        context_window=int(data.get("context_window", 0) or 0),
        model=str(data.get("model", "")),
        provider=str(data.get("provider", "")),
        committed=bool(data.get("committed", False)),
        pinned=bool(data.get("pinned", False)),
    )


# ---- reading --------------------------------------------------------------------
def _read(path: Path) -> list[StoredMessage]:
    """Never raises: a broken file costs the history, not the generation."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return []
    runs = data.get("runs") if isinstance(data, dict) else data
    if not isinstance(runs, list):
        return []
    found = [r for r in (_from_dict(entry) for entry in runs) if r is not None]
    return sorted(found, key=lambda r: r.started_at, reverse=True)


def list_runs(repo_path: str) -> list[StoredMessage]:
    """Messages generated for a repository, newest first."""
    return _read(runs_path(repo_path)) if repo_path else []


# ---- writing ---------------------------------------------------------------------
def _write(repo_path: str, runs: list[StoredMessage]) -> None:
    """Replaced, never truncated: an interrupted write must not eat the history."""
    path = runs_path(repo_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SCHEMA_VERSION,
        "repo_path": repo_path,
        "runs": [asdict(r) for r in runs],
    }
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _prune(runs: list[StoredMessage], limit: int) -> list[StoredMessage]:
    """Drop the oldest beyond the cap. Pinned messages are never dropped."""
    if limit <= 0:
        return runs
    kept: list[StoredMessage] = []
    count = 0
    for run in sorted(runs, key=lambda r: r.started_at, reverse=True):
        if run.pinned:
            kept.append(run)
        elif count < limit:
            count += 1
            kept.append(run)
    return sorted(kept, key=lambda r: r.started_at, reverse=True)


def record(
    repo_path: str,
    result,
    *,
    branch: str = "",
    head: str = "",
    dirty: bool = False,
    model: str = "",
    provider: str = "",
    limit: int = DEFAULT_LIMIT,
) -> tuple[StoredMessage | None, str]:
    """Store a generated message. Returns ``(stored, problem)``; never raises.

    ``result`` is a ``commit_generator.GenerationResult``. A run that produced
    nothing is not recorded: an empty row in the list is worse than no row.
    """
    message = (getattr(result, "message", "") or "").strip()
    if not repo_path or not message:
        return None, ""
    now = datetime.now(timezone.utc)
    stored = StoredMessage(
        run_id=f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:4]}",
        repo_path=repo_path,
        started_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        message=message[:MAX_MESSAGE],
        branch=branch,
        head=head,
        dirty=dirty,
        strategy=getattr(result, "strategy", ""),
        num_chunks=getattr(result, "num_chunks", 1),
        input_tokens=getattr(result, "input_tokens", 0),
        context_window=getattr(result, "context_window", 0),
        model=model,
        provider=provider,
    )
    try:
        _write(repo_path, _prune([stored, *_read(runs_path(repo_path))], limit))
    except OSError as exc:
        return None, str(exc)
    return stored, ""


def _update(stored: StoredMessage, **changes) -> bool:
    runs = _read(runs_path(stored.repo_path))
    for run in runs:
        if run.run_id == stored.run_id:
            for key, value in changes.items():
                setattr(run, key, value)
                setattr(stored, key, value)
    try:
        _write(stored.repo_path, runs)
    except OSError:
        return False
    return True


def mark_committed(stored: StoredMessage) -> bool:
    """Record that this message is the one that became a commit."""
    return _update(stored, committed=True)


def set_pinned(stored: StoredMessage, pinned: bool) -> bool:
    return _update(stored, pinned=pinned)


def delete_run(stored: StoredMessage) -> bool:
    runs = [r for r in _read(runs_path(stored.repo_path)) if r.run_id != stored.run_id]
    try:
        _write(stored.repo_path, runs)
    except OSError:
        return False
    return True


def clear_repo(repo_path: str) -> bool:
    """Forget one repository's generated messages entirely."""
    try:
        runs_path(repo_path).unlink(missing_ok=True)
    except OSError:
        return False
    return True
