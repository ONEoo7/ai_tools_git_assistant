"""Thin wrapper around the `git` CLI via subprocess.

Kept dependency-free (no GitPython). All calls target an explicit repo path
with `git -C <path> ...` and suppress the console-window flash on Windows.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Directories that never contain a project repo worth listing; pruned while
# scanning so large trees stay fast.
_SCAN_PRUNE = {
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    ".idea",
    ".vscode",
}

# Suppress the brief console window that pops up when a GUI app spawns a
# subprocess on Windows.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class GitError(RuntimeError):
    """Raised when a git command fails."""


@dataclass
class GitResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


def _run(repo: str | Path, args: list[str], *, stdin: str | None = None) -> GitResult:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    return GitResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        returncode=proc.returncode,
    )


def _diff_args(mode: str) -> list[str]:
    """Translate a diff mode into git diff arguments.

    "cached"  -> staged changes (git diff --cached)
    "working" -> all uncommitted changes vs HEAD (git diff HEAD)
    """
    if mode == "working":
        return ["diff", "HEAD"]
    return ["diff", "--cached"]


def _is_dubious_ownership(res: GitResult) -> bool:
    return "dubious ownership" in (res.stderr or "").lower()


def is_git_repo(path: str | Path) -> bool:
    p = Path(path)
    if not p.is_dir():
        return False
    res = _run(p, ["rev-parse", "--is-inside-work-tree"])
    if res.ok and res.stdout.strip() == "true":
        return True
    # A repo owned by another Windows account is still a real repo; git just
    # refuses to run in it until the user adds a safe.directory exception.
    return _is_dubious_ownership(res)


def has_git_dir(path: str | Path) -> bool:
    """Fast check: does the directory contain a `.git` entry (dir or file)?

    Used both for scanning and to detect repos that have vanished from disk
    (a repo whose ``.git`` is gone is treated as no longer present).
    """
    return (Path(path) / ".git").exists()


def find_git_repos(root: str | Path, max_depth: int = 6) -> list[str]:
    """Scan ``root`` for git repositories and return their normalized paths.

    Walks up to ``max_depth`` levels deep, records any directory containing a
    ``.git`` entry, and does not descend into a repo once found (nested repos
    below a repo boundary are ignored). Noise directories are pruned for speed.
    Uses a lightweight ``.git`` presence check rather than spawning git per dir.
    """
    root = Path(root)
    if not root.is_dir():
        return []
    if has_git_dir(root):
        return [os.path.normpath(str(root))]

    found: list[str] = []
    root_depth = len(root.parts)
    for dirpath, dirnames, _filenames in os.walk(root):
        p = Path(dirpath)
        if len(p.parts) - root_depth >= max_depth:
            dirnames[:] = []
            continue
        if has_git_dir(p):
            found.append(os.path.normpath(str(p)))
            dirnames[:] = []  # do not descend into a repo
            continue
        # Prune noise and hidden directories before descending.
        dirnames[:] = [
            d for d in dirnames if d not in _SCAN_PRUNE and not d.startswith(".")
        ]
    return sorted(found)


def current_branch(repo: str | Path) -> str:
    res = _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    return res.stdout.strip() if res.ok else "(unknown)"


# scp-style remote, e.g. git@github.com:ONEoo7/ai_tools.git
_SCP_RE = re.compile(r"^[^/@]+@([^/:]+):(.+)$")


def parse_owner_repo(url: str) -> tuple[str | None, str | None]:
    """Extract (owner, repo) from a git remote URL.

    Handles HTTPS (``https://github.com/ONEoo7/ai_tools.git``), scp-style
    (``git@github.com:ONEoo7/ai_tools.git``) and ssh:// URLs. The owner is the
    path segment just before the repository name (matches GitHub/GitLab).
    Returns (None, None) if nothing usable can be parsed.
    """
    if not url:
        return None, None
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]

    scp = _SCP_RE.match(url)
    if scp:
        path = scp.group(2)
    elif "://" in url:
        rest = url.split("://", 1)[1]  # strip scheme
        path = rest.split("/", 1)[1] if "/" in rest else ""  # drop host
    else:
        path = url

    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2:
        return parts[-2], parts[-1]
    if len(parts) == 1:
        return None, parts[0]
    return None, None


def get_remote_url(repo: str | Path) -> str | None:
    """Return the URL of ``origin`` (or the first remote), if any."""
    res = _run(repo, ["remote", "get-url", "origin"])
    if res.ok and res.stdout.strip():
        return res.stdout.strip()
    names = _run(repo, ["remote"])
    if names.ok and names.stdout.split():
        first = names.stdout.split()[0]
        alt = _run(repo, ["remote", "get-url", first])
        if alt.ok and alt.stdout.strip():
            return alt.stdout.strip()
    return None


def resolve_repo_meta(repo: str | Path) -> tuple[str, bool]:
    """Return ``(owner, blocked)`` for a repo.

    ``owner`` is the remote owner/org ("" if none), and ``blocked`` is True when
    git refused to read the repo due to a dubious-ownership (safe.directory)
    error, which the user must resolve in their global git config.
    """
    res = _run(repo, ["remote", "get-url", "origin"])
    if res.ok and res.stdout.strip():
        owner, _ = parse_owner_repo(res.stdout.strip())
        return owner or "", False
    if _is_dubious_ownership(res):
        return "", True
    names = _run(repo, ["remote"])
    if _is_dubious_ownership(names):
        return "", True
    if names.ok and names.stdout.split():
        alt = _run(repo, ["remote", "get-url", names.stdout.split()[0]])
        if alt.ok and alt.stdout.strip():
            owner, _ = parse_owner_repo(alt.stdout.strip())
            return owner or "", False
    return "", False


def repo_owner(repo: str | Path) -> str | None:
    """Return the owner/org of the repo's remote, or None if not determinable."""
    owner, _ = resolve_repo_meta(repo)
    return owner or None


def _run_global(args: list[str]) -> GitResult:
    """Run a git command not tied to a specific repository (e.g. global config)."""
    proc = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_NO_WINDOW,
    )
    return GitResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        returncode=proc.returncode,
    )


def safe_directory_is_all() -> bool:
    """True if the global config already trusts all repos (safe.directory = *)."""
    res = _run_global(["config", "--global", "--get-all", "safe.directory"])
    return res.ok and any(line.strip() == "*" for line in res.stdout.splitlines())


def trust_all_repositories() -> GitResult:
    """Add ``safe.directory = *`` to the global git config (idempotent).

    Clears 'dubious ownership' errors for repos owned by another account.
    """
    if safe_directory_is_all():
        return GitResult(ok=True, stdout="already trusted", stderr="", returncode=0)
    return _run_global(["config", "--global", "--add", "safe.directory", "*"])


def get_diff(repo: str | Path, mode: str) -> str:
    """Return the raw unified diff for the given mode."""
    res = _run(repo, [*_diff_args(mode)])
    if not res.ok:
        raise GitError(res.stderr.strip() or "git diff failed")
    return res.stdout


def get_diffstat(repo: str | Path, mode: str) -> str:
    """Return the compact `--stat` summary for the given mode."""
    res = _run(repo, [*_diff_args(mode), "--stat"])
    if not res.ok:
        raise GitError(res.stderr.strip() or "git diff --stat failed")
    return res.stdout.strip()


def has_changes(repo: str | Path, mode: str) -> bool:
    return bool(get_diffstat(repo, mode).strip())


def list_tracked_files(repo: str | Path) -> list[str]:
    """Return repo-relative paths of all tracked files (respects .gitignore).

    Uses ``-z`` so filenames with spaces or newlines are handled correctly.
    Raises GitError if git refuses (e.g. dubious-ownership block).
    """
    res = _run(repo, ["ls-files", "-z"])
    if not res.ok:
        raise GitError(res.stderr.strip() or "git ls-files failed")
    return [p for p in res.stdout.split("\0") if p]


def commit(repo: str | Path, message: str) -> GitResult:
    """Create a commit with the given (multi-line) message.

    The message is passed via stdin (`-F -`) so newlines and special characters
    survive intact. Only staged changes are committed.
    """
    return _run(repo, ["commit", "-F", "-"], stdin=message)
