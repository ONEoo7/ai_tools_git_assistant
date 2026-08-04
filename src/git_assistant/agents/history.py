"""Keeping audit runs, so "did it improve?" has something to compare against.

Runs live beside the settings file rather than inside it, following
``committer_identities.json``: ``Settings.save()`` rewrites the whole file on
every debounced edit and drops any key it does not declare, which is the wrong
home for tens of kilobytes per run.

    <config dir>/agent_runs/<repo key>/
        index.json                                  small; rewritten per run
        20260804T131233Z-config-audit-a91f.json     write-once; never reopened

The split is what makes the list cheap: drawing twenty rows with their arrows
reads one small index, not twenty reports. The run files are the record of
truth and the index is a cache derived from them -- which is the whole recovery
story when the index is lost or torn.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant.agents import compare
from git_assistant.agents.base import (
    CheckResult,
    Fact,
    Report,
    Section,
    Status,
    Table,
)
from git_assistant.config import APP_NAME, norm_path

SCHEMA_VERSION = 1
RUNS_DIR = "agent_runs"
INDEX_FILE = "index.json"
#: Newest N kept per repository *and* agent, so a quick config audit run daily
#: cannot evict every size audit.
DEFAULT_LIMIT = 20

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


# ---- where things live --------------------------------------------------------
def runs_root() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False)) / RUNS_DIR


def repo_key(repo_path: str) -> str:
    """A filename for a repository whose path may contain anything.

    The hash is the identity -- taken from ``norm_path``, so ``D:\\Repo`` and
    ``d:\\repo\\`` are one history, the same answer the repository tree gives.
    The readable stem is for whoever opens the folder.
    """
    norm = norm_path(repo_path)
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    stem = _UNSAFE.sub("_", Path(norm).name)[:32].strip("._") or "repo"
    return f"{stem}-{digest}"


def runs_dir(repo_path: str) -> Path:
    return runs_root() / repo_key(repo_path)


# ---- records -------------------------------------------------------------------
@dataclass
class StoredRun:
    """One recorded run: its metadata always, its report once loaded."""

    run_id: str
    agent_id: str
    repo_path: str
    started_at: str  # ISO-8601 UTC, sortable -- unlike Report.generated_at
    head: str = ""
    branch: str = ""
    dirty: bool = False
    narrated: bool = True
    fast: bool = False
    warnings: int = 0
    pinned: bool = False
    headline: dict = field(default_factory=dict)
    report: Report | None = None  # filled by load_run

    def when(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.started_at.replace("Z", "+00:00"))
        except ValueError:
            return None

    def when_label(self) -> str:
        """Local time, short: ``4 Aug 13:12``, with the year when it is not this one."""
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

    def to_index(self) -> dict:
        data = asdict(self)
        data.pop("report", None)
        return data


def _from_index(data: dict) -> StoredRun | None:
    if not isinstance(data, dict) or not data.get("run_id") or not data.get("agent_id"):
        return None
    return StoredRun(
        run_id=str(data["run_id"]),
        agent_id=str(data["agent_id"]),
        repo_path=str(data.get("repo_path", "")),
        started_at=str(data.get("started_at", "")),
        head=str(data.get("head", "")),
        branch=str(data.get("branch", "")),
        dirty=bool(data.get("dirty", False)),
        narrated=bool(data.get("narrated", True)),
        fast=bool(data.get("fast", False)),
        warnings=int(data.get("warnings", 0) or 0),
        pinned=bool(data.get("pinned", False)),
        headline=data.get("headline") if isinstance(data.get("headline"), dict) else {},
    )


# ---- serializing a report --------------------------------------------------------
def report_to_dict(report: Report) -> dict:
    return asdict(report)


def report_from_dict(data: dict) -> Report:
    """Rebuild a report so it is indistinguishable from a freshly collected one."""
    return Report(
        agent_id=str(data.get("agent_id", "")),
        title=str(data.get("title", "")),
        subtitle=str(data.get("subtitle", "")),
        generated_at=str(data.get("generated_at", "")),
        repo_path=str(data.get("repo_path", "")),
        head=str(data.get("head", "")),
        branch=str(data.get("branch", "")),
        dirty=bool(data.get("dirty", False)),
        sections=[_section(s) for s in data.get("sections", []) if isinstance(s, dict)],
        warnings=[str(w) for w in data.get("warnings", [])],
        checks=[_check(c) for c in data.get("checks", []) if isinstance(c, dict)],
    )


def _section(data: dict) -> Section:
    return Section(
        number=str(data.get("number", "")),
        title=str(data.get("title", "")),
        slot=str(data.get("slot", "")),
        prose=str(data.get("prose", "")),
        prose_verified=bool(data.get("prose_verified", True)),
        draft=str(data.get("draft", "")),
        facts=[_fact(f) for f in data.get("facts", []) if isinstance(f, dict)],
        tables=[_table(t) for t in data.get("tables", []) if isinstance(t, dict)],
        # JSON has no tuples: rebuild them rather than leaving a rehydrated
        # report subtly different from a collected one.
        commands=[
            (str(c[0]), str(c[1]))
            for c in data.get("commands", [])
            if isinstance(c, (list, tuple)) and len(c) == 2
        ],
        sections=[_section(s) for s in data.get("sections", []) if isinstance(s, dict)],
    )


def _fact(data: dict) -> Fact:
    raw = data.get("raw")
    return Fact(
        key=str(data.get("key", "")),
        label=str(data.get("label", "")),
        value=str(data.get("value", "")),
        raw=raw if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None,
    )


def _table(data: dict) -> Table:
    return Table(
        title=str(data.get("title", "")),
        columns=[str(c) for c in data.get("columns", [])],
        rows=[[str(cell) for cell in row] for row in data.get("rows", []) if isinstance(row, list)],
        note=str(data.get("note", "")),
    )


def _check(data: dict) -> CheckResult:
    try:
        status = Status(str(data.get("status", "skip")))
    except ValueError:
        status = Status.SKIP
    return CheckResult(
        id=str(data.get("id", "")),
        title=str(data.get("title", "")),
        status=status,
        headline=str(data.get("headline", "")),
        evidence=[str(e) for e in data.get("evidence", [])],
        remediation=str(data.get("remediation", "")),
        weight=int(data.get("weight", 2) or 2),
    )


# ---- reading -----------------------------------------------------------------------
def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return None


def _load_index(directory: Path) -> list[StoredRun]:
    data = _read_json(directory / INDEX_FILE)
    runs = data.get("runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        return rebuild(directory)
    found = [r for r in (_from_index(entry) for entry in runs) if r is not None]
    return found or rebuild(directory)


def rebuild(directory: Path) -> list[StoredRun]:
    """Regenerate the index by reading the run files themselves.

    The index is a cache; the run files are the record. A lost or torn index
    costs a directory listing, not the history.
    """
    if not directory.is_dir():
        return []
    runs: list[StoredRun] = []
    for path in sorted(directory.glob("*.json")):
        if path.name == INDEX_FILE:
            continue
        data = _read_json(path)
        if not isinstance(data, dict):
            continue
        run = _from_index(data)
        if run is None:
            continue
        report = data.get("report")
        if isinstance(report, dict):
            run.headline = _headline(run.agent_id, report_from_dict(report))
        runs.append(run)
    runs.sort(key=lambda r: r.started_at, reverse=True)
    return runs


def list_runs(repo_path: str, agent_id: str = "") -> list[StoredRun]:
    """Recorded runs for a repository, newest first, optionally one agent's."""
    directory = runs_dir(repo_path)
    if not directory.is_dir():
        return []
    runs = sorted(_load_index(directory), key=lambda r: r.started_at, reverse=True)
    if agent_id:
        runs = [r for r in runs if r.agent_id == agent_id]
    return runs


def unreadable_count(repo_path: str) -> int:
    """Run files on disk that could not be read. Worth saying out loud."""
    directory = runs_dir(repo_path)
    if not directory.is_dir():
        return 0
    bad = 0
    for path in directory.glob("*.json"):
        if path.name != INDEX_FILE and _read_json(path) is None:
            bad += 1
    return bad


def load_run(run: StoredRun) -> StoredRun | None:
    """Fill in ``run.report`` from disk. ``None`` if the file is gone or broken."""
    data = _read_json(runs_dir(run.repo_path) / f"{run.run_id}.json")
    if not isinstance(data, dict) or not isinstance(data.get("report"), dict):
        return None
    run.report = report_from_dict(data["report"])
    return run


# ---- writing -------------------------------------------------------------------------
def _headline(agent_id: str, report: Report) -> dict:
    """The few numbers a list of runs needs, so drawing it opens no run file."""
    facts = report.facts_by_key()
    out = {}
    for key in compare.headline_keys(agent_id):
        fact = facts.get(key)
        if fact is not None:
            out[key] = {"value": fact.value, "raw": fact.raw}
    return out


def _write_index(directory: Path, runs: list[StoredRun]) -> None:
    """Rewritten on every run, so it is replaced atomically rather than truncated."""
    payload = {
        "version": SCHEMA_VERSION,
        "repo_path": runs[0].repo_path if runs else "",
        "runs": [r.to_index() for r in runs],
    }
    tmp = directory / f"{INDEX_FILE}.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, directory / INDEX_FILE)


def record(
    report: Report, *, narrated: bool = True, fast: bool = False, limit: int = DEFAULT_LIMIT
) -> tuple[StoredRun | None, str]:
    """Store a finished run. Returns ``(run, problem)``; it never raises.

    A five-minute audit must not be lost because the disk is full, so a failure
    here is reported back to be shown beside a report that is still on screen.
    """
    now = datetime.now(timezone.utc)
    run = StoredRun(
        run_id=f"{now.strftime('%Y%m%dT%H%M%SZ')}-{report.agent_id}-{uuid.uuid4().hex[:4]}",
        agent_id=report.agent_id,
        repo_path=report.repo_path,
        started_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        head=report.head,
        branch=report.branch,
        dirty=report.dirty,
        narrated=narrated,
        fast=fast,
        warnings=len(report.warnings),
        headline=_headline(report.agent_id, report),
        report=report,
    )
    directory = runs_dir(report.repo_path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        # Read the existing runs BEFORE writing this one: a missing index is
        # rebuilt by listing the directory, which would otherwise find the file
        # just written and list it twice.
        existing = _load_index(directory)
        payload = {**run.to_index(), "version": SCHEMA_VERSION, "report": report_to_dict(report)}
        # Compact: this file is read by the program, and the prose inside it is
        # already long enough without two spaces of indent per line.
        (directory / f"{run.run_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        runs = _dedupe([run, *existing])
        runs = _prune(directory, runs, limit)
        _write_index(directory, runs)
    except OSError as exc:
        return None, str(exc)
    return run, ""


def _dedupe(runs: list[StoredRun]) -> list[StoredRun]:
    seen: dict[str, StoredRun] = {}
    for run in runs:
        seen.setdefault(run.run_id, run)
    return list(seen.values())


def _prune(directory: Path, runs: list[StoredRun], limit: int) -> list[StoredRun]:
    """Drop the oldest beyond the cap, per agent. Pinned runs are never dropped."""
    if limit <= 0:
        return runs
    kept: list[StoredRun] = []
    seen: dict[str, int] = {}
    for run in sorted(runs, key=lambda r: r.started_at, reverse=True):
        if run.pinned:
            kept.append(run)
            continue
        count = seen.get(run.agent_id, 0)
        if count < limit:
            seen[run.agent_id] = count + 1
            kept.append(run)
            continue
        # Only forget the entry if the file really went: a file held open by a
        # scanner stays listed and is retried on the next run.
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


def delete_run(run: StoredRun) -> bool:
    directory = runs_dir(run.repo_path)
    if not _delete_file(directory, run.run_id):
        return False
    remaining = [r for r in _load_index(directory) if r.run_id != run.run_id]
    try:
        _write_index(directory, remaining)
    except OSError:
        return False
    return True


def set_pinned(run: StoredRun, pinned: bool) -> bool:
    """Pin a run so the retention cap never removes it (the baseline to beat)."""
    directory = runs_dir(run.repo_path)
    runs = _load_index(directory)
    for stored in runs:
        if stored.run_id == run.run_id:
            stored.pinned = pinned
            run.pinned = pinned
    try:
        _write_index(directory, runs)
    except OSError:
        return False
    return True


def clear_repo(repo_path: str) -> bool:
    """Forget one repository's history entirely."""
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
