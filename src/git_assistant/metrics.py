"""Line-count metrics for git repositories.

Counts lines in tracked files (via ``git ls-files``, so ``.gitignore`` and
untracked/build artifacts are excluded), grouped by file extension. Binary files
are skipped. Kept free of Qt so the logic is unit-testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from git_assistant import git_ops

# Read at most this many bytes to sniff for binary content.
_BINARY_SNIFF = 8192


@dataclass
class LangStat:
    files: int = 0
    lines: int = 0
    blank: int = 0

    @property
    def code(self) -> int:
        return self.lines - self.blank

    def add(self, files: int, lines: int, blank: int) -> None:
        self.files += files
        self.lines += lines
        self.blank += blank


@dataclass
class RepoMetrics:
    path: str
    ok: bool = True
    error: str = ""
    by_ext: dict[str, LangStat] = field(default_factory=dict)

    @property
    def totals(self) -> LangStat:
        t = LangStat()
        for s in self.by_ext.values():
            t.add(s.files, s.lines, s.blank)
        return t


def ext_of(rel_path: str) -> str:
    """Group key for a file: its lowercased extension, else its basename."""
    name = rel_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    suffix = Path(name).suffix.lower()
    if suffix:
        return suffix
    return name or "(no ext)"  # e.g. Makefile, Dockerfile, LICENSE


def count_text(text: str) -> tuple[int, int]:
    """Return (total_lines, blank_lines) for ``text``."""
    lines = text.splitlines()
    blank = sum(1 for ln in lines if not ln.strip())
    return len(lines), blank


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:_BINARY_SNIFF]


def analyze_repo(repo_path: str) -> RepoMetrics:
    """Count lines per extension for all tracked, non-binary files in a repo."""
    m = RepoMetrics(path=repo_path)
    try:
        files = git_ops.list_tracked_files(repo_path)
    except git_ops.GitError as exc:
        m.ok = False
        m.error = str(exc)
        return m

    for rel in files:
        full = os.path.join(repo_path, rel)
        try:
            with open(full, "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if is_binary(data):
            continue
        lines, blank = count_text(data.decode("utf-8", errors="replace"))
        stat = m.by_ext.setdefault(ext_of(rel), LangStat())
        stat.add(1, lines, blank)
    return m


def aggregate(metrics: list[RepoMetrics]) -> dict[str, LangStat]:
    """Sum per-extension stats across several repos."""
    total: dict[str, LangStat] = {}
    for m in metrics:
        for ext, s in m.by_ext.items():
            total.setdefault(ext, LangStat()).add(s.files, s.lines, s.blank)
    return total
