"""Streaming git output, and measuring a directory tree.

``git_ops._run`` captures a command's whole output, which is right for every
call it makes and wrong for the one thing the audit agents need: the object list
of a large repository is millions of lines and must be consumed as it arrives.
These helpers follow ``git_ops``' conventions (``git -C <repo>``, no console
window on Windows, UTF-8 with replacement) and add the part it lacks -- a pipe
that is killed on the way out, so cancelling a run or closing the window cannot
leave ``git rev-list`` walking a 190 GB pack forever.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from git_assistant.git_ops import _NO_WINDOW

#: How often a consumer should look at the cancel flag. Cheap enough to be
#: responsive, rare enough not to show up in a profile.
CANCEL_EVERY = 20_000


def _kill(proc: subprocess.Popen | None) -> None:
    """End a child that may still be running, and wait for it to go."""
    if proc is None:
        return
    for stream in (proc.stdout, proc.stdin):
        try:
            if stream is not None and not stream.closed:
                stream.close()
        except OSError:
            pass
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _popen(repo: str | Path, args: list[str], **kwargs) -> subprocess.Popen:
    return subprocess.Popen(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
        **kwargs,
    )


def _lines(stream) -> Iterator[str]:
    for raw in stream:
        yield raw.decode("utf-8", "replace").rstrip("\r\n")


@contextmanager
def streamed(repo: str | Path, args: list[str]) -> Iterator[Iterator[str]]:
    """Run one git command, yielding its stdout line by line."""
    proc = None
    try:
        proc = _popen(repo, args)
        yield _lines(proc.stdout)
    finally:
        _kill(proc)


@contextmanager
def piped(
    repo: str | Path, first: list[str], second: list[str]
) -> Iterator[Iterator[str]]:
    """``git <first> | git <second>``, yielding the second command's lines.

    The pipe is what keeps the object scan streaming: the paths travel from
    ``rev-list`` to ``cat-file`` through the kernel rather than through a
    dictionary of every SHA in the repository.
    """
    left = right = None
    try:
        left = _popen(repo, first)
        right = _popen(repo, second, stdin=left.stdout)
        # Only the child needs the read end now; closing ours means the left
        # command is told to stop as soon as the right one goes away.
        left.stdout.close()
        yield _lines(right.stdout)
    finally:
        _kill(right)
        _kill(left)


@dataclass
class TreeSize:
    """Bytes and file count under a directory, bucketed by sub-tree."""

    total: int = 0
    files: int = 0
    buckets: dict[str, int] = None  # bucket name -> bytes

    def __post_init__(self) -> None:
        if self.buckets is None:
            self.buckets = {}


def measure_tree(
    root: str | Path,
    bucket_of,
    *,
    on_file=None,
    on_progress=None,
    every: int = 2000,
    should_stop=None,
) -> TreeSize:
    """Sum file sizes under ``root``, grouped by ``bucket_of(relative_parts)``.

    A filesystem walk rather than ``git count-objects``: that command reports
    the object store only, and cannot see ``.git/modules`` (submodules) or
    ``.git/lfs`` -- between them often most of the directory. ``os.scandir``
    carries size in the directory entry on Windows, so this costs one syscall
    per file and is bounded by file count, not by bytes.

    ``on_file(parts, size, mtime)`` sees every file, so a caller looking for
    particular names (leftover ``tmp_pack_*``, say) gets them from this pass
    instead of walking the same tree again.
    """
    root = Path(root)
    out = TreeSize()

    def walk(directory: Path, parts: tuple[str, ...]) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return  # unreadable directory: report what we can, not nothing
        for entry in entries:
            if should_stop is not None and should_stop():
                return
            here = (*parts, entry.name)
            try:
                if entry.is_dir(follow_symlinks=False):
                    walk(Path(entry.path), here)
                    continue
                stat = entry.stat(follow_symlinks=False)
                size = stat.st_size
            except OSError:
                continue
            out.total += size
            out.files += 1
            if on_file is not None:
                on_file(here, size, stat.st_mtime)
            bucket = bucket_of(here)
            out.buckets[bucket] = out.buckets.get(bucket, 0) + size
            if on_progress is not None and out.files % every == 0:
                on_progress(out)

    walk(root, ())
    return out
