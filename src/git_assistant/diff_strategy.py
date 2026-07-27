"""Diff preprocessing for the hybrid token-overflow strategy.

Pure, dependency-light helpers (no Qt, no network) so they can be unit tested:

- ``split_diff``       : parse a raw unified diff into per-file segments
- ``filter_files``     : drop noise files (lockfiles, binaries, user globs)
- ``split_into_hunks`` : break one file segment into header + individual hunks
- ``pack_units``       : greedily group text units into chunks under a token budget

The orchestration (single-shot vs map-reduce) lives in ``commit_generator``.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable
from dataclasses import dataclass

TokenFn = Callable[[str], int]


@dataclass
class FileDiff:
    """One file's section of a unified diff."""

    path: str
    text: str


def split_diff(raw: str) -> list[FileDiff]:
    """Split a raw unified diff into per-file :class:`FileDiff` segments."""
    if not raw.strip():
        return []
    lines = raw.splitlines(keepends=True)
    segments: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line.startswith("diff --git "):
            if current:
                segments.append(current)
            current = [line]
        elif current:
            current.append(line)
        else:
            # Content before the first "diff --git" (rare); start a segment.
            current = [line]
    if current:
        segments.append(current)

    result: list[FileDiff] = []
    for seg in segments:
        text = "".join(seg)
        result.append(FileDiff(path=_extract_path(seg), text=text))
    return result


def _extract_path(segment: list[str]) -> str:
    """Best-effort file path for a diff segment."""
    # Prefer the "+++ b/<path>" line (the new path).
    for line in segment:
        if line.startswith("+++ b/"):
            return line[6:].strip()
        if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            return line[4:].strip()
    # Fall back to the "diff --git a/<path> b/<path>" header.
    header = segment[0] if segment else ""
    if header.startswith("diff --git "):
        parts = header[len("diff --git ") :].split(" b/", 1)
        if len(parts) == 2:
            return parts[1].strip()
        return parts[0].removeprefix("a/").strip()
    return "?"


def filter_files(
    files: list[FileDiff], ignore_globs: list[str]
) -> tuple[list[FileDiff], list[str]]:
    """Split files into (kept, dropped-paths) based on ignore globs.

    A file matches if either its full path or its basename matches any glob.
    Binary-diff segments (git emits ``Binary files ... differ``) are always dropped.
    """
    kept: list[FileDiff] = []
    dropped: list[str] = []
    for f in files:
        basename = f.path.rsplit("/", 1)[-1]
        is_binary = "Binary files " in f.text and "differ" in f.text
        matched = any(
            fnmatch.fnmatch(f.path, g) or fnmatch.fnmatch(basename, g)
            for g in ignore_globs
        )
        if is_binary or matched:
            dropped.append(f.path)
        else:
            kept.append(f)
    return kept, dropped


def split_into_hunks(file_diff: FileDiff) -> list[str]:
    """Break a file segment into header + per-hunk texts.

    Each returned string is the file header (``diff --git`` .. up to the first
    ``@@``) followed by exactly one hunk, so every piece is self-describing.
    Files with no ``@@`` hunks (pure renames/mode changes) return a single piece.
    """
    lines = file_diff.text.splitlines(keepends=True)
    header: list[str] = []
    hunks: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("@@"):
            if current is not None:
                hunks.append(current)
            current = [line]
        elif current is None:
            header.append(line)
        else:
            current.append(line)
    if current is not None:
        hunks.append(current)

    if not hunks:
        return [file_diff.text]
    head = "".join(header)
    return [head + "".join(h) for h in hunks]


def truncate_to_budget(text: str, budget: int, count_tokens: TokenFn) -> str:
    """Hard-truncate a single oversized text unit, keeping head and tail.

    Preserves the leading lines (file/hunk headers and early context) and the
    trailing lines, replacing the middle with a marker noting how much was cut.
    """
    if count_tokens(text) <= budget:
        return text
    lines = text.splitlines(keepends=True)
    if len(lines) <= 4:
        return text  # nothing meaningful to trim
    head_keep = max(1, len(lines) // 2)
    # Shrink the kept head until it fits, always leaving a tail line.
    while head_keep > 1:
        candidate_head = "".join(lines[:head_keep])
        if count_tokens(candidate_head) <= budget - 32:
            break
        head_keep = head_keep * 2 // 3
    cut = len(lines) - head_keep - 1
    if cut <= 0:
        return text
    marker = f"\n[... {cut} lines truncated to fit the model context ...]\n"
    return "".join(lines[:head_keep]) + marker + lines[-1]


def pack_units(units: list[str], budget: int, count_tokens: TokenFn) -> list[str]:
    """Greedily concatenate text units into chunks that stay under ``budget``.

    Units are assumed to already individually fit the budget (callers should run
    :func:`split_into_hunks` / :func:`truncate_to_budget` first). Each returned
    chunk is a newline-joined group of units.
    """
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for unit in units:
        t = count_tokens(unit)
        if current and current_tokens + t > budget:
            chunks.append("\n".join(current))
            current = []
            current_tokens = 0
        current.append(unit)
        current_tokens += t
    if current:
        chunks.append("\n".join(current))
    return chunks


def split_into_hunks_indexed(file_diff: FileDiff) -> list[tuple[str, list[int]]]:
    """Like :func:`split_into_hunks`, but also report each piece's line indices.

    Indices refer to positions in ``file_diff.text.splitlines(keepends=True)``,
    so callers can track exactly which original lines a piece carries.
    """
    lines = file_diff.text.splitlines(keepends=True)
    header_idx: list[int] = []
    hunks: list[list[int]] = []
    current: list[int] | None = None
    for i, line in enumerate(lines):
        if line.startswith("@@"):
            if current is not None:
                hunks.append(current)
            current = [i]
        elif current is None:
            header_idx.append(i)
        else:
            current.append(i)
    if current is not None:
        hunks.append(current)

    if not hunks:
        return [(file_diff.text, list(range(len(lines))))]
    out: list[tuple[str, list[int]]] = []
    for h in hunks:
        idxs = header_idx + h
        out.append(("".join(lines[i] for i in idxs), idxs))
    return out


def truncate_indexed(
    text: str, budget: int, count_tokens: TokenFn
) -> tuple[str, list[int]]:
    """Truncate ``text`` and report which of its lines were kept.

    Mirrors :func:`truncate_to_budget`; the returned indices are positions in
    ``text.splitlines(keepends=True)``.
    """
    lines = text.splitlines(keepends=True)
    everything = list(range(len(lines)))
    if count_tokens(text) <= budget or len(lines) <= 4:
        return text, everything
    head_keep = max(1, len(lines) // 2)
    while head_keep > 1:
        if count_tokens("".join(lines[:head_keep])) <= budget - 32:
            break
        head_keep = head_keep * 2 // 3
    cut = len(lines) - head_keep - 1
    if cut <= 0:
        return text, everything
    marker = f"\n[... {cut} lines truncated to fit the model context ...]\n"
    out = "".join(lines[:head_keep]) + marker + lines[-1]
    return out, [*range(head_keep), len(lines) - 1]


def build_units_with_coverage(
    files: list[FileDiff], budget: int, count_tokens: TokenFn
) -> tuple[list[str], dict[str, set[int]]]:
    """Build budget-sized units and report omitted lines per file.

    Returns ``(units, omitted)`` where ``omitted[path]`` holds the indices of
    lines that did **not** make it into any unit (and so never reach the model).
    """
    units: list[str] = []
    omitted_by_path: dict[str, set[int]] = {}
    for f in files:
        n_lines = len(f.text.splitlines(keepends=True))
        if count_tokens(f.text) <= budget:
            units.append(f.text)
            omitted_by_path[f.path] = set()
            continue
        omitted = set(range(n_lines))  # start pessimistic; clear what we send
        for piece_text, idxs in split_into_hunks_indexed(f):
            if count_tokens(piece_text) <= budget:
                units.append(piece_text)
                omitted -= set(idxs)
            else:
                trimmed, kept_rel = truncate_indexed(piece_text, budget, count_tokens)
                units.append(trimmed)
                omitted -= {idxs[i] for i in kept_rel if i < len(idxs)}
        omitted_by_path[f.path] = omitted
    return units, omitted_by_path


def build_units(
    files: list[FileDiff], budget: int, count_tokens: TokenFn
) -> list[str]:
    """Turn file diffs into budget-sized text units.

    Files within budget stay whole; oversized files split by hunk; oversized
    hunks are hard-truncated. The result feeds :func:`pack_units`.
    """
    units: list[str] = []
    for f in files:
        if count_tokens(f.text) <= budget:
            units.append(f.text)
            continue
        for piece in split_into_hunks(f):
            if count_tokens(piece) <= budget:
                units.append(piece)
            else:
                units.append(truncate_to_budget(piece, budget, count_tokens))
    return units
