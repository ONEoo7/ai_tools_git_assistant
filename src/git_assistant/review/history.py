"""Keeping code reviews, so "did this get better?" has something to compare.

The same shape as ``agents.history``, and for the same reasons:

    <config dir>/review_runs/<repo key>/
        index.json                              small; rewritten per review
        20260805T131233Z-a91f.json              write-once; never reopened

The index is a cache derived from the run files, which are the record. Losing
it costs a directory listing, not the history.

What is deliberately *not* stored is the calls. A review of forty files carries
forty prompts of a few thousand tokens each, and twenty of those runs would be
tens of megabytes of text nobody reads twice. The findings are the record; the
calls are for watching a run happen.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant.config import APP_NAME, repo_key
from git_assistant.review.parse import Finding
from git_assistant.review.reviewer import FileReview, ReviewRun

SCHEMA_VERSION = 1
RUNS_DIR = "review_runs"
INDEX_FILE = "index.json"
#: Newest N kept per repository (0 keeps everything).
DEFAULT_LIMIT = 20


# ---- where things live ----------------------------------------------------------
def runs_root() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False)) / RUNS_DIR


def runs_dir(repo_path: str) -> Path:
    return runs_root() / repo_key(repo_path)


# ---- records ---------------------------------------------------------------------
@dataclass
class StoredReview:
    """One recorded review: its metadata always, its findings once loaded."""

    run_id: str
    repo_path: str
    started_at: str  # ISO-8601 UTC, sortable
    table_name: str = ""
    table_fingerprint: str = ""
    model: str = ""
    provider: str = ""
    head: str = ""
    branch: str = ""
    dirty: bool = False
    pinned: bool = False
    headline: dict = field(default_factory=dict)
    run: ReviewRun | None = None  # filled by load_run

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

    def commit_label(self) -> str:
        where = f"{self.branch} @ {self.head[:7]}" if self.head else "unknown commit"
        return f"{where}*" if self.dirty else where

    def result_label(self) -> str:
        counts = self.headline
        findings = counts.get("findings", 0)
        files = counts.get("files", 0)
        text = f"{findings} finding(s), {files} file(s)"
        if counts.get("failed"):
            text += f", {counts['failed']} not reviewed"
        return text

    def to_index(self) -> dict:
        data = asdict(self)
        data.pop("run", None)
        return data


def _from_index(data: dict) -> StoredReview | None:
    if not isinstance(data, dict) or not data.get("run_id"):
        return None
    return StoredReview(
        run_id=str(data["run_id"]),
        repo_path=str(data.get("repo_path", "")),
        started_at=str(data.get("started_at", "")),
        table_name=str(data.get("table_name", "")),
        table_fingerprint=str(data.get("table_fingerprint", "")),
        model=str(data.get("model", "")),
        provider=str(data.get("provider", "")),
        head=str(data.get("head", "")),
        branch=str(data.get("branch", "")),
        dirty=bool(data.get("dirty", False)),
        pinned=bool(data.get("pinned", False)),
        headline=data.get("headline") if isinstance(data.get("headline"), dict) else {},
    )


# ---- serializing a run --------------------------------------------------------------
def run_to_dict(run: ReviewRun) -> dict:
    # Dropped before the copy, not after: ``asdict`` deep-copies what it walks,
    # and forty prompts is a lot of text to duplicate on the way to the bin.
    data = asdict(replace(run, calls=[]))
    data.pop("calls", None)  # see the module docstring
    return data


def run_from_dict(data: dict) -> ReviewRun:
    """Rebuild a run so it is indistinguishable from one just finished."""
    return ReviewRun(
        repo_path=str(data.get("repo_path", "")),
        table_name=str(data.get("table_name", "")),
        table_fingerprint=str(data.get("table_fingerprint", "")),
        rules_total=int(data.get("rules_total", 0) or 0),
        rules_sent=int(data.get("rules_sent", 0) or 0),
        provider=str(data.get("provider", "")),
        model=str(data.get("model", "")),
        diff_mode=str(data.get("diff_mode", "cached")),
        context_window=int(data.get("context_window", 0) or 0),
        started_at=str(data.get("started_at", "")),
        head=str(data.get("head", "")),
        branch=str(data.get("branch", "")),
        dirty=bool(data.get("dirty", False)),
        staged_total=int(data.get("staged_total", 0) or 0),
        files=[_file(f) for f in data.get("files", []) if isinstance(f, dict)],
    )


def _file(data: dict) -> FileReview:
    return FileReview(
        path=str(data.get("path", "")),
        findings=[_finding(f) for f in data.get("findings", []) if isinstance(f, dict)],
        raw_reply=str(data.get("raw_reply", "")),
        error=str(data.get("error", "")),
        diff_truncated=bool(data.get("diff_truncated", False)),
        content_truncated=bool(data.get("content_truncated", False)),
        content_sent=bool(data.get("content_sent", True)),
        rules_sent=int(data.get("rules_sent", 0) or 0),
        # Named here or dropped on load: this rebuild is field by field, and a
        # missing one is silent until a restart.
        rules_total=int(data.get("rules_total", 0) or 0),
        table_name=str(data.get("table_name", "")),
        language=str(data.get("language", "")),
        version=str(data.get("version", "")),
        retried=bool(data.get("retried", False)),
        seconds=float(data.get("seconds", 0.0) or 0.0),
    )


def _finding(data: dict) -> Finding:
    return Finding(
        rule_id=str(data.get("rule_id", "")),
        rule_details=str(data.get("rule_details", "")),
        path=str(data.get("path", "")),
        line=int(data.get("line", 0) or 0),
        message=str(data.get("message", "")),
        raw_line=str(data.get("raw_line", "")),
        parsed=bool(data.get("parsed", True)),
        rule_known=bool(data.get("rule_known", True)),
    )


# ---- reading --------------------------------------------------------------------------
def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return None


def _load_index(directory: Path) -> list[StoredReview]:
    data = _read_json(directory / INDEX_FILE)
    runs = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return rebuild(directory)
    found = [r for r in (_from_index(entry) for entry in runs) if r is not None]
    return found or rebuild(directory)


def rebuild(directory: Path) -> list[StoredReview]:
    """Regenerate the index by reading the run files themselves."""
    if not directory.is_dir():
        return []
    runs: list[StoredReview] = []
    for path in sorted(directory.glob("*.json")):
        if path.name == INDEX_FILE:
            continue
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        stored = _from_index(data)
        if stored is None:
            continue
        run = data.get("run")
        if isinstance(run, dict):
            stored.headline = run_from_dict(run).headline()
        runs.append(stored)
    runs.sort(key=lambda r: r.started_at, reverse=True)
    return runs


def list_runs(repo_path: str) -> list[StoredReview]:
    """Recorded reviews for a repository, newest first."""
    directory = runs_dir(repo_path)
    if not directory.is_dir():
        return []
    return sorted(_load_index(directory), key=lambda r: r.started_at, reverse=True)


def load_run(stored: StoredReview) -> StoredReview | None:
    """Fill in ``stored.run`` from disk. ``None`` if the file is gone or broken."""
    data = _read_json(runs_dir(stored.repo_path) / f"{stored.run_id}.json")
    if not isinstance(data, dict) or not isinstance(data.get("run"), dict):
        return None
    stored.run = run_from_dict(data["run"])
    return stored


# ---- writing ---------------------------------------------------------------------------
def _write_index(directory: Path, runs: list[StoredReview]) -> None:
    """Rewritten on every review, so it is replaced atomically, not truncated."""
    payload = {
        "version": SCHEMA_VERSION,
        "repo_path": runs[0].repo_path if runs else "",
        "runs": [r.to_index() for r in runs],
    }
    tmp = directory / f"{INDEX_FILE}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, directory / INDEX_FILE)


def record(run: ReviewRun, *, limit: int = DEFAULT_LIMIT) -> tuple[StoredReview | None, str]:
    """Store a finished review. Returns ``(stored, problem)``; it never raises.

    A review that took forty calls must not be lost because the disk is full,
    so a failure here is reported back to be shown beside findings that are
    still on screen.
    """
    now = datetime.now(timezone.utc)
    stored = StoredReview(
        run_id=f"{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:4]}",
        repo_path=run.repo_path,
        started_at=run.started_at or now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        table_name=run.table_name,
        table_fingerprint=run.table_fingerprint,
        model=run.model,
        provider=run.provider,
        head=run.head,
        branch=run.branch,
        dirty=run.dirty,
        headline=run.headline(),
        run=run,
    )
    directory = runs_dir(run.repo_path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Read the existing runs BEFORE writing this one: a missing index is
        # rebuilt by listing the directory, which would otherwise find the file
        # just written and list it twice.
        existing = _load_index(directory)
        payload = {
            **stored.to_index(),
            "version": SCHEMA_VERSION,
            "run": run_to_dict(run),
        }
        (directory / f"{stored.run_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        runs = _dedupe([stored, *existing])
        runs = _prune(directory, runs, limit)
        _write_index(directory, runs)
    except OSError as exc:
        return None, str(exc)
    return stored, ""


def _dedupe(runs: list[StoredReview]) -> list[StoredReview]:
    seen: dict[str, StoredReview] = {}
    for run in runs:
        seen.setdefault(run.run_id, run)
    return list(seen.values())


def _prune(directory: Path, runs: list[StoredReview], limit: int) -> list[StoredReview]:
    """Drop the oldest beyond the cap. Pinned reviews are never dropped."""
    if limit <= 0:
        return runs
    kept: list[StoredReview] = []
    count = 0
    for run in sorted(runs, key=lambda r: r.started_at, reverse=True):
        if run.pinned:
            kept.append(run)
            continue
        if count < limit:
            count += 1
            kept.append(run)
            continue
        # Only forget the entry if the file really went: one held open by a
        # scanner stays listed and is retried on the next review.
        if _delete_file(directory, run.run_id):
            continue
        kept.append(run)
    return sorted(kept, key=lambda r: r.started_at, reverse=True)


def _delete_file(directory: Path, run_id: str) -> bool:
    try:
        (directory / f"{run_id}.json").unlink(missing_ok=True)
    except OSError:
        return False
    return True


def delete_run(stored: StoredReview) -> bool:
    directory = runs_dir(stored.repo_path)
    if not _delete_file(directory, stored.run_id):
        return False
    remaining = [r for r in _load_index(directory) if r.run_id != stored.run_id]
    try:
        _write_index(directory, remaining)
    except OSError:
        return False
    return True


def set_pinned(stored: StoredReview, pinned: bool) -> bool:
    """Pin a review so the retention cap never removes it (the one to beat)."""
    directory = runs_dir(stored.repo_path)
    runs = _load_index(directory)
    for run in runs:
        if run.run_id == stored.run_id:
            run.pinned = pinned
            stored.pinned = pinned
    try:
        _write_index(directory, runs)
    except OSError:
        return False
    return True


def clear_repo(repo_path: str) -> bool:
    """Forget one repository's reviews entirely."""
    directory = runs_dir(repo_path)
    if not directory.is_dir():
        return True
    try:
        for path in directory.glob("*.json"):
            path.unlink(missing_ok=True)
        directory.rmdir()
    except OSError:
        return False
    return True
