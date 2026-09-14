"""Thin wrapper around the `git` CLI via subprocess.

Kept dependency-free (no GitPython). All calls target an explicit repo path
with `git -C <path> ...` and suppress the console-window flash on Windows.
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from git_assistant import processes

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


#: Said whenever git itself cannot be started. A sentence rather than a
#: traceback: it is shown to the person who has to fix it, and what they have to
#: do is install git.
GIT_MISSING = "Git is not installed, or not on PATH."

#: What a shell reports for a command it cannot find. Used here so a caller
#: that only looks at `returncode` still sees a failure.
NOT_INSTALLED = 127


def _cannot_run(exc: OSError) -> GitResult:
    """A missing git as a failed result rather than as an exception.

    Every git call in this application went through the two runners below, and
    neither caught anything -- so on a machine with no git, the first one raised
    `FileNotFoundError` from inside a Qt slot, and PyQt turns an exception that
    escapes a slot into `qFatal()`. That is a process abort with no traceback:
    the winget validation crash of 0.3.16, reported as `Qt6Core.dll` and
    `c0000409`, and impossible to recognise as "git is not installed".

    `GitResult` already carries failure. Using it is what makes every caller's
    existing `if not res.ok` handle this too, without one of them being changed.
    """
    return GitResult(
        ok=False, stdout="", stderr=f"{GIT_MISSING} [{exc}]", returncode=NOT_INSTALLED
    )


def _run(repo: str | Path, args: list[str], *, stdin: str | None = None) -> GitResult:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return _cannot_run(exc)
    return GitResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        returncode=proc.returncode,
    )


def git_available() -> bool:
    """Is there a git to run at all?

    Asked once at start-up so the application can say so plainly, instead of
    every repository silently looking empty.
    """
    return _run_global(["--version"]).returncode != NOT_INSTALLED


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


# `path = <relative path>` inside a .gitmodules section.
_GITMODULES_PATH_RE = re.compile(r"^\s*path\s*=\s*(.+?)\s*$", re.MULTILINE)


def _gitmodules_paths(repo: str | Path) -> list[str]:
    """Submodule paths declared in ``repo``'s ``.gitmodules``, relative to it.

    Read from the file rather than via ``git submodule``: scanning stays
    subprocess-free (and therefore fast), and a repo git refuses to touch
    because of a dubious-ownership check still reports its submodules.
    """
    f = Path(repo) / ".gitmodules"
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [m.group(1).replace("/", os.sep) for m in _GITMODULES_PATH_RE.finditer(text)]


def find_submodules(repo: str | Path, max_depth: int = 4) -> list[str]:
    """Return the normalized paths of ``repo``'s submodules, parents first.

    Recurses into submodules that declare submodules of their own, up to
    ``max_depth`` levels. Only checked-out submodules are returned: one that was
    never initialised has no working tree to act on, so listing it would offer
    the user a repository they cannot commit in.

    Each level is sorted by name. ``.gitmodules`` lists submodules in whatever
    order they were added, which is arbitrary, and a repository with forty of
    them is then a list nobody can scan. Sorting per level rather than the
    finished list is what keeps the "parents first" promise above: a
    submodule's own submodules still follow it.
    """
    found: list[str] = []
    seen: set[str] = set()

    def walk(base: Path, depth: int) -> None:
        if depth > max_depth:
            return
        for rel in sorted(_gitmodules_paths(base), key=str.casefold):
            child = base / rel
            key = os.path.normcase(os.path.normpath(str(child)))
            if key in seen or not has_git_dir(child):
                continue
            seen.add(key)
            found.append(os.path.normpath(str(child)))
            walk(child, depth + 1)

    walk(Path(repo), 1)
    return found


def find_git_repos(
    root: str | Path, max_depth: int = 6, include_submodules: bool = True
) -> list[str]:
    """Scan ``root`` for git repositories and return their normalized paths.

    Walks up to ``max_depth`` levels deep, records any directory containing a
    ``.git`` entry, and does not descend into a repo once found. Noise
    directories are pruned for speed. Uses a lightweight ``.git`` presence check
    rather than spawning git per directory.

    Directories nested below a repo boundary are only reported when the repo
    declares them as submodules -- vendored checkouts and stray clones inside a
    working tree are not repositories the user manages.
    """
    root = Path(root)
    if not root.is_dir():
        return []

    def with_submodules(repo: str) -> list[str]:
        return [repo, *find_submodules(repo)] if include_submodules else [repo]

    if has_git_dir(root):
        return with_submodules(os.path.normpath(str(root)))

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
    # Sorted so a parent repo always precedes its submodules (its path is a
    # prefix of theirs), which is the order the repository tree expects.
    return [sub for repo in sorted(found) for sub in with_submodules(repo)]


def current_branch(repo: str | Path) -> str:
    res = _run(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    return res.stdout.strip() if res.ok else "(unknown)"


#: What `git rev-parse --abbrev-ref HEAD` reports when no branch is checked out.
DETACHED_HEAD = "HEAD"


#: How `.git` names the real git directory when it is a file rather than a
#: directory: a submodule's checkout, and a linked worktree.
_GITDIR_PREFIX = "gitdir:"
#: How HEAD names a branch. Anything else in there is a bare commit id, which
#: is a detached HEAD -- at a commit, not on a branch.
_HEAD_REF_PREFIX = "ref:"
_BRANCH_REF_PREFIX = "refs/heads/"


def _git_dir(repo: str | Path) -> Path:
    """Where ``repo`` keeps HEAD.

    Usually ``<repo>/.git``. For a submodule or a linked worktree that is a
    *file* holding ``gitdir: <path>``, pointing at the directory git actually
    keeps -- which is where that checkout's own HEAD lives, so a submodule
    answers for itself rather than for the repository containing it.

    Returns ``<repo>/.git`` when there is nothing readable to follow: a path
    with no HEAD under it and a bad one are the same answer to the caller.
    """
    dot_git = Path(repo) / ".git"
    try:
        if dot_git.is_dir():
            return dot_git
        pointer = dot_git.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return dot_git
    if not pointer.startswith(_GITDIR_PREFIX):
        return dot_git
    target = Path(pointer[len(_GITDIR_PREFIX) :].strip())
    return target if target.is_absolute() else Path(repo) / target


def head_branch(repo: str | Path) -> str:
    """The checked-out branch, read from ``.git`` rather than by running git.

    Subprocess-free for the reason `_gitmodules_paths` is, and by a wider
    margin than that reason suggests: this is asked once per repository every
    time the repository list is built, and spawning git is around 140 times
    the cost of the read it does -- measured at 55ms a call against 0.4ms
    here, so a list of 200 repositories is eleven seconds of starting
    processes or eighty milliseconds of reading files. Reading is all git does
    to answer this: HEAD is a text file naming a ref.

    Returns "" when there is no branch to name -- a detached HEAD, a `.git`
    that cannot be read, a path that is not a repository at all. The callers
    show nothing rather than an error: this is an annotation beside a
    repository, not the thing being asked about.
    """
    try:
        head = (_git_dir(repo) / "HEAD").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return ""
    ref = head.strip()
    if not ref.startswith(_HEAD_REF_PREFIX):
        return ""  # a commit id: detached
    name = ref[len(_HEAD_REF_PREFIX) :].strip()
    if name.startswith(_BRANCH_REF_PREFIX):
        name = name[len(_BRANCH_REF_PREFIX) :]
    return name


def list_branches(repo: str | Path) -> list[str]:
    """Local branches, most recently committed to first.

    Recency order rather than alphabetical: the branches someone is working on
    are the ones they want at the top of a picker, which is how the repository
    list is ordered too.
    """
    res = _run(
        repo,
        [
            "for-each-ref",
            "--format=%(refname:short)",
            "--sort=-committerdate",
            "refs/heads",
        ],
    )
    if not res.ok:
        return []
    return [line.strip() for line in res.stdout.splitlines() if line.strip()]


def has_uncommitted_changes(repo: str | Path) -> bool:
    """True when anything is staged, modified or untracked in the work tree."""
    res = _run(repo, ["status", "--porcelain"])
    return bool(res.ok and res.stdout.strip())


def switch_branch(repo: str | Path, name: str) -> GitResult:
    """Check out an existing local branch.

    ``git switch`` rather than ``git checkout``: it only ever means "change
    branch", so a branch name that also matches a path cannot be read as a
    request to discard that file's changes. Git refuses the switch by itself
    when carrying the local changes over would overwrite something, and that
    refusal is returned here rather than being worked around.
    """
    return _run(repo, ["switch", name])


# scp-style remote, e.g. git@github.com:ONEoo7/ai_tools.git
_SCP_RE = re.compile(r"^[^/@]+@([^/:]+):(.+)$")


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


def blocked_by_ownership(repo: str | Path) -> bool:
    """True when git refuses to work in ``repo`` because of who owns it.

    That is the dubious-ownership check: a repository owned by another account
    is off limits until the user adds a ``safe.directory`` exception to their
    global git config, and every command run in it fails until they do.

    Any command that opens the repository runs the check, so this asks the
    cheapest one -- the same question `is_git_repo` asks. It used to be answered
    as a side effect of looking up the remote, back when the remote's owner was
    wanted as well: a lookup of up to three git calls to learn a boolean the
    first of them had already settled.
    """
    return _is_dubious_ownership(_run(repo, ["rev-parse", "--is-inside-work-tree"]))


def _run_global(args: list[str]) -> GitResult:
    """Run a git command not tied to a specific repository (e.g. global config).

    Guarded like `_run`, and for the same reason: this is the one that actually
    fired on a clean machine, from the identity bootstrap the settings window
    runs before it draws anything.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return _cannot_run(exc)
    return GitResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
        returncode=proc.returncode,
    )


# ---- committer identity ----------------------------------------------------
def get_identity(repo: str | Path) -> tuple[str, str]:
    """Return the ``(name, email)`` git would stamp on a commit in ``repo``.

    This is the *effective* identity, so it accounts for every layer git
    consults -- repository config, ``includeIf`` conditional includes, and the
    global fallback. Either half is "" when unset. Reading the effective value
    rather than only the repo-local one matters: a repo with no local identity
    still commits as somebody, and showing nothing there would be a lie.
    """
    name = _run(repo, ["config", "--get", "user.name"])
    email = _run(repo, ["config", "--get", "user.email"])
    return (
        name.stdout.strip() if name.ok else "",
        email.stdout.strip() if email.ok else "",
    )


def get_local_identity(repo: str | Path) -> tuple[str, str]:
    """Return the identity set in ``repo``'s own config, ignoring wider scopes.

    Distinguishes "this repository pins an identity" from "it inherits one",
    which is what tells the user whether a previous selection is still in force.
    """
    name = _run(repo, ["config", "--local", "--get", "user.name"])
    email = _run(repo, ["config", "--local", "--get", "user.email"])
    return (
        name.stdout.strip() if name.ok else "",
        email.stdout.strip() if email.ok else "",
    )


def get_signingkey(repo: str | Path) -> str:
    """The key git would sign a commit in ``repo`` with ("" if none)."""
    res = _run(repo, ["config", "--get", "user.signingkey"])
    return res.stdout.strip() if res.ok else ""


def signing_enabled(repo: str | Path) -> bool:
    """True when ``commit.gpgsign`` asks for every commit here to be signed."""
    res = _run(repo, ["config", "--get", "--type=bool", "commit.gpgsign"])
    return res.ok and res.stdout.strip() == "true"


_OK = GitResult(ok=True, stdout="", stderr="", returncode=0)


def _unset_local(repo: str | Path, key: str) -> GitResult:
    """Remove a local config key. Already-absent is success, not failure.

    Git exits 5 for unsetting something that is not there, which describes the
    end state this asks for.
    """
    res = _run(repo, ["config", "--local", "--unset", key])
    return _OK if res.returncode == 5 else res


def set_identity(
    repo: str | Path, name: str, email: str, signingkey: str = ""
) -> GitResult:
    """Pin ``name``/``email`` as the committer identity for ``repo`` only.

    Written to the repository's own config (``--local``), so it outranks the
    global identity and any conditional include, and applies to commits made
    from any tool -- not just this one.

    ``user.signingkey`` is written when the identity carries one and *removed*
    when it does not. Leaving a previous identity's key in place is the bug
    this avoids: the commit would be authored by one person and signed by
    another's key, which forges report as unverified.
    """
    res = _run(repo, ["config", "--local", "user.name", name])
    if not res.ok:
        return res
    res = _run(repo, ["config", "--local", "user.email", email])
    if not res.ok:
        return res
    if signingkey:
        return _run(repo, ["config", "--local", "user.signingkey", signingkey])
    return _unset_local(repo, "user.signingkey")


def clear_local_identity(repo: str | Path) -> GitResult:
    """Drop ``repo``'s pinned identity so it inherits the global one again."""
    for key in ("user.name", "user.email", "user.signingkey"):
        res = _unset_local(repo, key)
        if not res.ok:
            return res
    return _OK


def get_global_identity() -> tuple[str, str]:
    """Return the ``(name, email)`` from the user's global git config."""
    name = _run_global(["config", "--global", "--get", "user.name"])
    email = _run_global(["config", "--global", "--get", "user.email"])
    return (
        name.stdout.strip() if name.ok else "",
        email.stdout.strip() if email.ok else "",
    )


def get_global_signingkey() -> str:
    res = _run_global(["config", "--global", "--get", "user.signingkey"])
    return res.stdout.strip() if res.ok else ""


# ---- push credentials -------------------------------------------------------
# Forges where the hostname in a remote URL is the real one, so it carries no
# information about *which* account will authenticate. A host that is not in
# this set is most likely an SSH config alias, which does.
_CANONICAL_HOSTS = {
    "github.com",
    "gitlab.com",
    "bitbucket.org",
    "codeberg.org",
    "git.sr.ht",
    "ssh.dev.azure.com",
    "vs-ssh.visualstudio.com",
}


@dataclass
class PushAuth:
    """What will authenticate a push -- as distinct from what signs a commit.

    Answers a question the identity picker cannot: ``user.email`` decides how a
    commit is *labelled*, never who git logs in as. The two are set in
    different places and can disagree without any error.

    Resolved from configuration only. Asking the credential helper (``git
    credential fill``) would give a firmer answer and can pop an
    authentication prompt, which is not acceptable while merely redrawing a
    combo box.
    """

    kind: str = ""  # "ssh" | "https" | "" when there is no remote
    host: str = ""
    account: str = ""  # username pinned in config; "" when not determinable
    shared: bool = False  # one credential serves every account on this host

    def summary(self) -> str:
        if not self.kind:
            return "no remote"
        if self.kind == "ssh":
            via = "default key" if self.shared else "key from SSH config"
            return f"push: SSH to {self.host} ({via})"
        if self.account:
            return f"push: {self.host} as {self.account}"
        return f"push: {self.host}"

    def destination(self) -> str:
        """`summary` for a readout that is already captioned "Push to:"."""
        if not self.kind:
            return "no remote"
        if self.kind == "ssh":
            via = "default key" if self.shared else "key from SSH config"
            return f"{self.host} over SSH ({via})"
        if self.account:
            return f"{self.host} as {self.account}"
        return self.host

    def warning(self) -> str:
        """Why the credential may not be the one this identity implies."""
        if not self.shared:
            return ""
        if self.kind == "ssh":
            return (
                f"Pushes to {self.host} use your default SSH key, whichever "
                "identity is selected. Committing as one account does not log "
                "you in as it.\n\nTo separate them, give each account a Host "
                "alias with its own IdentityFile in ~/.ssh/config and point "
                "the remote at the alias."
            )
        return (
            f"One credential is stored for all of {self.host}, so pushes use "
            "the same account whichever identity is selected. Committing as "
            "one account does not log you in as it.\n\nTo separate them: git "
            f"config --global credential.https://{self.host}.useHttpPath true"
        )


def _split_remote(url: str) -> tuple[str, str, str]:
    """Return ``(kind, host, user)`` for a remote URL."""
    url = (url or "").strip()
    scp = _SCP_RE.match(url)
    if scp:
        return "ssh", scp.group(1), url.split("@", 1)[0]
    if "://" not in url:
        return "", "", ""
    scheme, rest = url.split("://", 1)
    netloc = rest.split("/", 1)[0]
    user = ""
    if "@" in netloc:
        userinfo, netloc = netloc.rsplit("@", 1)
        user = userinfo.split(":", 1)[0]  # never carry a password around
    kind = "ssh" if scheme.startswith("ssh") else scheme.lower()
    if kind in ("http", "https"):
        kind = "https"
    return kind, netloc, user


def _config_first(repo: str | Path, keys: list[str]) -> str:
    """First of ``keys`` that is set, as git resolves it (repo, then global)."""
    for key in keys:
        res = _run(repo, ["config", "--get", key])
        if res.ok and res.stdout.strip():
            return res.stdout.strip()
    return ""


def describe_push_auth(repo: str | Path) -> PushAuth:
    """Work out what will authenticate a push from ``repo``."""
    kind, host, user = _split_remote(get_remote_url(repo) or "")
    if not kind or not host:
        return PushAuth()

    if kind == "ssh":
        # The "git@" in git@github.com is the protocol's user, not an account.
        # What actually picks a key is the host, so a non-canonical host means
        # an alias in ~/.ssh/config -- which is how keys get separated.
        return PushAuth(kind="ssh", host=host, shared=host in _CANONICAL_HOSTS)

    if kind != "https":
        return PushAuth(kind=kind, host=host)

    account = user or _config_first(
        repo,
        [f"credential.https://{host}.username", "credential.username"],
    )
    # Path-scoped credentials give each org its own entry, so one host can
    # serve several accounts without them colliding.
    per_path = _config_first(
        repo,
        [f"credential.https://{host}.useHttpPath", "credential.useHttpPath"],
    )
    scoped = per_path.strip().lower() in ("true", "yes", "on", "1")
    return PushAuth(
        kind="https", host=host, account=account, shared=not account and not scoped
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


def file_content(repo: str | Path, path: str, mode: str) -> str:
    """A file as it is *after* the change the given mode describes.

    The mode decides where to read from, and getting that wrong is not
    cosmetic: for staged changes the answer is the index, because a file staged
    and then edited again would otherwise be shown alongside a diff it no
    longer matches.

    Returns "" for a file that no longer exists (a deletion has no content
    after it) and for anything that cannot be decoded as text.
    """
    if mode == "working":
        try:
            return (Path(repo) / path).read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            return ""
    res = _run(repo, ["show", f":{path}"])
    return res.stdout if res.ok else ""


def list_tracked_files(repo: str | Path) -> list[str]:
    """Return repo-relative paths of all tracked files (respects .gitignore).

    Uses ``-z`` so filenames with spaces or newlines are handled correctly.
    Raises GitError if git refuses (e.g. dubious-ownership block).
    """
    res = _run(repo, ["ls-files", "-z"])
    if not res.ok:
        raise GitError(res.stderr.strip() or "git ls-files failed")
    return [p for p in res.stdout.split("\0") if p]


# ---- the working tree, a file at a time --------------------------------------
@dataclass
class GitBytes:
    """A `GitResult` whose output was kept as git wrote it. See `_run_bytes`."""

    ok: bool
    stdout: bytes
    stderr: str
    returncode: int

    def as_result(self) -> GitResult:
        return GitResult(
            ok=self.ok,
            stdout=self.stdout.decode("utf-8", errors="replace"),
            stderr=self.stderr,
            returncode=self.returncode,
        )


def _run_bytes(
    repo: str | Path, args: list[str], *, stdin: bytes | None = None
) -> GitBytes:
    """`_run`, without decoding what comes back.

    `_run` reads in text mode, which quietly turns every CRLF into LF on the way
    in. Harmless for a branch name; fatal for line endings. A diff read that way
    cannot show a change that is only line endings, and a patch built from it no
    longer matches the file it came from, so git refuses to apply it. Anything
    that shows or reproduces file content comes through here instead.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=stdin,
            capture_output=True,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        failed = _cannot_run(exc)
        return GitBytes(False, b"", failed.stderr, failed.returncode)
    return GitBytes(
        ok=proc.returncode == 0,
        stdout=proc.stdout or b"",
        stderr=(proc.stderr or b"").decode("utf-8", errors="replace"),
        returncode=proc.returncode,
    )


def _nul_joined(paths: list[str]) -> bytes:
    """Names for a ``-z`` stdin: no command-line length to overflow, and no
    character in a name that needs quoting."""
    return b"".join(path.encode("utf-8") + b"\0" for path in paths)


def _path(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class StatusEntry:
    """One changed path, as `git status` reports it.

    ``index`` and ``worktree`` are git's two status letters -- HEAD against the
    index, then the index against the file on disk -- with "." for unchanged.
    """

    path: str
    index: str = "."
    worktree: str = "."
    #: Where a staged rename or copy came from.
    orig_path: str = ""
    untracked: bool = False
    submodule: bool = False
    #: In conflict, mid-merge: git wants it resolved and then staged.
    unmerged: bool = False

    @property
    def staged(self) -> bool:
        """Some of this path's change is in the index."""
        return not (self.untracked or self.unmerged) and self.index != "."

    @property
    def unstaged(self) -> bool:
        """Some of this path's change is on disk and not in the index."""
        return self.untracked or self.unmerged or self.worktree != "."

    @property
    def paths(self) -> list[str]:
        """Every path an action on this entry has to name: both ends of a rename."""
        return [self.path, self.orig_path] if self.orig_path else [self.path]


def status_entries(repo: str | Path) -> list[StatusEntry]:
    """Every changed path: staged, unstaged, untracked and in conflict.

    Porcelain v2 with ``-z``: the one form that says which entries are
    submodules, and that never quotes a name. Untracked directories are listed a
    file at a time, because a file is what gets staged.

    Raises GitError when git refuses, e.g. over dubious ownership.
    """
    res = _run_bytes(repo, ["status", "--porcelain=v2", "-z", "--untracked-files=all"])
    if not res.ok:
        raise GitError(res.stderr.strip() or "git status failed")
    records = iter(res.stdout.split(b"\0"))
    entries: list[StatusEntry] = []
    for record in records:
        kind = record[:1]
        if kind == b"?":
            entries.append(StatusEntry(_path(record[2:]), untracked=True))
        elif kind == b"1":  # 1 XY sub mH mI mW hH hI path
            fields = record.split(b" ", 8)
            entries.append(_changed(fields[1], fields[2], fields[8]))
        elif kind == b"2":  # 2 XY sub mH mI mW hH hI Xscore path, then origPath
            fields = record.split(b" ", 9)
            orig = next(records, b"")
            entries.append(_changed(fields[1], fields[2], fields[9], orig=orig))
        elif kind == b"u":  # u XY sub m1 m2 m3 mW h1 h2 h3 path
            fields = record.split(b" ", 10)
            entries.append(_changed(fields[1], fields[2], fields[10], unmerged=True))
    return entries


def _changed(
    xy: bytes, sub: bytes, path: bytes, *, orig: bytes = b"", unmerged: bool = False
) -> StatusEntry:
    letters = xy.decode("ascii", errors="replace")
    return StatusEntry(
        path=_path(path),
        index=letters[:1] or ".",
        worktree=letters[1:2] or ".",
        orig_path=_path(orig),
        submodule=sub.startswith(b"S"),
        unmerged=unmerged,
    )


def unstaged_counts(entries: list[StatusEntry]) -> tuple[int, int]:
    """``(unstaged, changed)``: paths with work not yet staged, of all changed.

    A file staged in part is one changed path with some of it still unstaged,
    so it counts once in each.
    """
    return sum(1 for entry in entries if entry.unstaged), len(entries)


def has_head(repo: str | Path) -> bool:
    """Whether there is a commit yet. Before the first one there is not."""
    return _run(repo, ["rev-parse", "--verify", "--quiet", "HEAD"]).ok


#: Names given to add, restore and rm arrive on stdin, so no number of them
#: overflows a command line -- and, with --literal-pathspecs, literally, so a
#: file called ``*.txt`` is that file rather than a pattern.
_NAMES_ON_STDIN = ["--pathspec-from-file=-", "--pathspec-file-nul"]


def stage_paths(repo: str | Path, paths: list[str]) -> GitResult:
    """Stage everything about ``paths``: edits, new files and deletions alike."""
    if not paths:
        return GitResult(ok=True, stdout="", stderr="", returncode=0)
    return _run_bytes(
        repo,
        ["--literal-pathspecs", "add", "-A", *_NAMES_ON_STDIN],
        stdin=_nul_joined(paths),
    ).as_result()


def unstage_paths(repo: str | Path, paths: list[str]) -> GitResult:
    """Take ``paths`` back out of the index, leaving the files on disk alone.

    Before the first commit there is no HEAD to restore an entry from, so the
    entry is removed instead -- which is what unstaging a new file amounts to.
    """
    if not paths:
        return GitResult(ok=True, stdout="", stderr="", returncode=0)
    if has_head(repo):
        how = ["restore", "--staged"]
    else:
        how = ["rm", "--cached", "-r", "-q", "--ignore-unmatch"]
    return _run_bytes(
        repo,
        ["--literal-pathspecs", *how, *_NAMES_ON_STDIN],
        stdin=_nul_joined(paths),
    ).as_result()


#: Every diff here is read by `git apply` as well as by a person, so the config
#: that makes a diff nicer to read and impossible to apply is overridden:
#: colour, external diff tools, text conversion, and prefixes other than a/ b/.
_PATCH_ARGS = [
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--src-prefix=a/",
    "--dst-prefix=b/",
]


def file_diff(
    repo: str | Path, path: str, *, staged: bool = False, untracked: bool = False
) -> bytes:
    """One file's diff, exactly as git wrote it, carriage returns and all.

    ``staged`` compares the index with HEAD; otherwise the file on disk with the
    index. An untracked file has no index entry, so it is compared with nothing
    and comes back as a file of added lines.

    Raises GitError when git refuses.
    """
    # Names as they are rather than octal-escaped, and blank context lines as a
    # space rather than nothing -- a line with no prefix is not a diff line.
    readable = ["-c", "core.quotepath=false", "-c", "diff.suppressBlankEmpty=false"]
    if untracked:
        res = _run_bytes(
            repo,
            [*readable, "diff", "--no-index", *_PATCH_ARGS, "--", "/dev/null", path],
        )
        # --no-index exits 1 for "they differ", which a new file always does.
        if res.returncode not in (0, 1):
            raise GitError(res.stderr.strip() or "git diff failed")
        return res.stdout
    which = ["--cached"] if staged else []
    res = _run_bytes(
        repo,
        [*readable, "--literal-pathspecs", "diff", *which, *_PATCH_ARGS, "--", path],
    )
    if not res.ok:
        raise GitError(res.stderr.strip() or "git diff failed")
    return res.stdout


def apply_to_index(
    repo: str | Path, patch: bytes, *, reverse: bool = False
) -> GitResult:
    """Apply ``patch`` to the index alone; the file on disk is not touched.

    How part of a file is staged, or with ``reverse``, unstaged. ``--recount``
    has git count each hunk's lines itself rather than trust its header: a hunk
    with lines left out is a hunk whose header was written for other lines.
    """
    args = ["apply", "--cached", "--recount", "--whitespace=nowarn"]
    if reverse:
        args.append("--reverse")
    return _run_bytes(repo, [*args, "-"], stdin=patch).as_result()


#: One line of `git ls-files --eol`: "i/lf    w/crlf  attr/text eol=lf      ".
_EOL_INFO_RE = re.compile(r"^i/(\S*)\s+w/(\S*)\s+attr/(.*?)\s*$")


@dataclass(frozen=True)
class LineEndings:
    """How one file's lines end, in the index and on disk, and what is declared.

    ``index`` and ``worktree`` are git's own words: "lf", "crlf", "mixed",
    "none" for a file with no line break in it, "-text" for one git takes to be
    binary -- or "" where that copy does not exist: no index entry for a new
    file, no file on disk for a deleted one. ``attributes`` is what
    .gitattributes sets for the path, as git spells it ("text eol=lf"), or "".
    """

    index: str
    worktree: str
    attributes: str

    @property
    def declared(self) -> str:
        """The line ending .gitattributes names outright: "lf", "crlf" or ""."""
        for part in self.attributes.split():
            if part in ("eol=lf", "eol=crlf"):
                return part[len("eol=") :]
        return ""


def line_endings(repo: str | Path, path: str) -> LineEndings | None:
    """What ``path`` ends its lines with, as git sees it; None if git has no idea.

    Git's own reading rather than a count of bytes here, so a file git calls
    binary is binary here too, by the same test.
    """
    return line_endings_of(repo, [path]).get(path)


#: How much of a command line the names passed to one `git ls-files` may take.
#: Windows stops a command line at 32767 characters, and ls-files takes names
#: only as arguments.
_ARGUMENTS_BUDGET = 16_000


def line_endings_of(repo: str | Path, paths: list[str]) -> dict[str, LineEndings]:
    """`line_endings` for many paths at once, leaving out any git has no idea about."""
    found: dict[str, LineEndings] = {}
    wanted = set(paths)
    batch: list[str] = []
    size = 0
    for path in paths:
        if batch and size + len(path) + 1 > _ARGUMENTS_BUDGET:
            found.update(_ls_files_eol(repo, batch, wanted))
            batch, size = [], 0
        batch.append(path)
        size += len(path) + 1
    if batch:
        found.update(_ls_files_eol(repo, batch, wanted))
    return found


def _ls_files_eol(repo: str | Path, paths: list[str], wanted: set[str]) -> dict:
    res = _run_bytes(
        repo,
        [
            "--literal-pathspecs",
            "ls-files", "--eol", "-z", "--cached", "--others", "--", *paths,
        ],
    )
    if not res.ok:
        raise GitError(res.stderr.strip() or "git ls-files failed")
    found: dict[str, LineEndings] = {}
    for record in res.stdout.split(b"\0"):
        info, _tab, name = record.partition(b"\t")
        path = _path(name)
        match = _EOL_INFO_RE.match(info.decode("ascii", errors="replace"))
        if path in wanted and match:
            found[path] = LineEndings(*match.groups())
    return found


@dataclass
class Normalized:
    """What `normalize_line_endings` did with each path it was given."""

    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    #: No line-ending rule, a filter owns the file, or there is no file to rewrite.
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def line_ending_rules(repo: str | Path, paths: list[str]) -> set[str]:
    """The ``paths`` whose line endings .gitattributes decides.

    A path with a ``text`` or ``eol`` rule and no ``filter``. ``-text`` -- the
    ``binary`` macro included -- is a rule that says never convert, so it does
    not count; and a filter such as Git LFS owns the file's bytes outright, so
    rewriting them underneath it would break it.

    Asked of git rather than read out of the file, so every .gitattributes in
    the tree, the order they apply in, and macros are all accounted for.
    """
    if not paths:
        return set()
    res = _run_bytes(
        repo,
        ["check-attr", "-z", "--stdin", "text", "eol", "filter"],
        stdin=_nul_joined(paths),
    )
    if not res.ok:
        raise GitError(res.stderr.strip() or "git check-attr failed")
    fields = res.stdout.split(b"\0")
    found: dict[str, dict[str, str]] = {}
    for at in range(0, len(fields) - 2, 3):
        path, name, value = (_path(part) for part in fields[at : at + 3])
        found.setdefault(path, {})[name] = value
    ruled: set[str] = set()
    for path, attributes in found.items():
        text = attributes.get("text", "unspecified")
        eol = attributes.get("eol", "unspecified")
        filter_ = attributes.get("filter", "unspecified")
        filtered = filter_ not in ("unspecified", "unset")
        if filtered or text == "unset":
            continue
        if text in ("set", "auto") or eol in ("lf", "crlf"):
            ruled.add(path)
    return ruled


def normalize_line_endings(repo: str | Path, paths: list[str]) -> Normalized:
    """Rewrite ``paths`` on disk with the line endings .gitattributes gives them.

    The conversion is git's own rather than a copy of it: each file is cleaned
    into a blob the way ``git add`` would clean it, and that blob is written
    back out the way a checkout would write it. So everything git weighs --
    patterns, nested .gitattributes files, ``text=auto``'s binary detection, the
    platform's line ending for ``text`` with no ``eol`` -- is weighed here
    without being re-derived. The index is never touched.

    Only files with a line-ending rule are considered (see `line_ending_rules`),
    and a file is written only when that changes its bytes. None of it changes
    what ``git diff`` shows: git diffs the cleaned form, which is the same
    before and after.

    Raises GitError when git will not say which rules apply.
    """
    done = Normalized()
    root = Path(repo)
    on_disk: list[str] = []
    for path in paths:
        target = root / path
        if target.is_file() and not target.is_symlink():
            on_disk.append(path)
        else:
            done.skipped.append(path)  # deleted, a directory, a submodule, a link
    ruled = line_ending_rules(repo, on_disk)
    work: list[str] = []
    for path in on_disk:
        if path not in ruled:
            done.skipped.append(path)
        elif "\n" in path or "\r" in path:
            done.failed.append((path, "the file name contains a line break"))
        else:
            work.append(path)
    if not work:
        return done

    for path, form in _checked_out(repo, work).items():
        if isinstance(form, str):
            done.failed.append((path, form))
            continue
        target = root / path
        try:
            if target.read_bytes() == form:
                done.unchanged.append(path)
                continue
            _replace_bytes(target, form)
        except OSError as exc:
            done.failed.append((path, str(exc)))
            continue
        done.changed.append(path)
    return done


def _checked_out(repo: str | Path, paths: list[str]) -> dict[str, bytes | str]:
    """Each file as a checkout would write it back: its bytes, or why not.

    Git's own conversion, in two steps: cleaned into a blob the way ``git add``
    cleans, then smudged back out the way a checkout writes. Writes blobs into
    the object store -- as ``git add`` does -- and nothing else anywhere.
    """
    # Cleaned in one call. safecrlf is off because this conversion is the very
    # one it exists to warn about, and here it has been asked for.
    hashed = _run_bytes(
        repo,
        ["-c", "core.safecrlf=false", "hash-object", "-w", "--stdin-paths"],
        stdin=b"".join(path.encode("utf-8") + b"\n" for path in paths),
    )
    blobs = hashed.stdout.decode("ascii", errors="replace").split()
    if not hashed.ok or len(blobs) != len(paths):
        why = hashed.stderr.strip() or "git hash-object failed"
        return {path: why for path in paths}

    def checked_out(pair: tuple[str, str]) -> tuple[str, GitBytes]:
        path, blob = pair
        return path, _run_bytes(repo, ["cat-file", "--filters", f"--path={path}", blob])

    # Written back one call per file. `cat-file --batch --filters` would do it in
    # one, but it labels each object with its size *before* conversion, so its
    # output cannot be split where one file ends and the next begins. Run side
    # by side instead: the time goes on starting processes, not on work.
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(paths)))) as pool:
        outcomes = list(pool.map(checked_out, zip(paths, blobs)))
    return {
        path: (
            result.stdout
            if result.ok
            else (result.stderr.strip() or "git cat-file failed")
        )
        for path, result in outcomes
    }


@dataclass(frozen=True)
class EndingChange:
    """How applying .gitattributes rewrites one file: git's words, before and after."""

    now: str
    becomes: str


def _ending_word(data: bytes) -> str:
    """"lf", "crlf", "mixed" or "none", by the same names `git ls-files --eol` uses."""
    crlf = data.count(b"\r\n")
    lone = data.count(b"\n") - crlf
    if crlf and lone:
        return "mixed"
    if crlf:
        return "crlf"
    return "lf" if lone else "none"


def line_ending_changes(repo: str | Path, paths: list[str]) -> dict[str, EndingChange]:
    """The ``paths`` whose line endings applying .gitattributes would rewrite.

    Exactly the files `normalize_line_endings` would change, by running the same
    conversion without writing the result -- so the two cannot disagree. Git's
    words for a file cannot settle it alone: a rule that names no ending
    (``text``, ``text=auto``) leaves the ending to this machine's config, and
    only the conversion itself says what that comes to.

    Converting is a git call per file, so only the files that could change are
    converted: a text file with a line-ending rule whose endings on disk are not
    already the ones the rule names. A file that already has them never changes.

    Raises GitError when git will not say which rules apply.
    """
    root = Path(repo)
    endings = line_endings_of(repo, paths)
    texty = [
        path
        for path in paths
        if (found := endings.get(path)) is not None
        and found.worktree in ("lf", "crlf", "mixed")
        and (root / path).is_file()
        and not (root / path).is_symlink()
        and "\n" not in path
        and "\r" not in path
    ]
    ruled = line_ending_rules(repo, texty)
    suspects = []
    for path in texty:
        declared = endings[path].declared
        # With no ending named, the rule leaves it to this machine's config,
        # which only the conversion itself can answer for.
        if path in ruled and (not declared or endings[path].worktree != declared):
            suspects.append(path)
    if not suspects:
        return {}
    changes: dict[str, EndingChange] = {}
    for path, form in _checked_out(repo, suspects).items():
        if isinstance(form, str):
            continue  # git would not convert it; Normalize would say why
        try:
            if (root / path).read_bytes() == form:
                continue
        except OSError:
            continue
        changes[path] = EndingChange(endings[path].worktree, _ending_word(form))
    return changes


def _replace_bytes(target: Path, data: bytes) -> None:
    """Write ``data`` over ``target`` in one step, keeping its permissions.

    By way of a temporary file beside it and a rename, so a failure part way
    through never leaves a file half rewritten.
    """
    handle, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        shutil.copymode(target, temporary)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def list_tags(repo: str | Path) -> list[str]:
    """All tags, newest version first (git's version-aware ordering)."""
    res = _run(repo, ["tag", "--list", "--sort=-v:refname"])
    if not res.ok:
        return []
    return [t.strip() for t in res.stdout.splitlines() if t.strip()]


def list_tags_with_dates(repo: str | Path) -> list[tuple[str, str]]:
    """Return ``(tag, creation date)`` pairs, newest version first.

    ``creatordate`` is the tag's own date for annotated tags and the commit date
    for lightweight ones, which is the date a user means by "when was it made".
    """
    res = _run(
        repo,
        [
            "for-each-ref",
            "--sort=-v:refname",
            "--format=%(refname:short)%09%(creatordate:format:%Y-%m-%d %H:%M)",
            "refs/tags",
        ],
    )
    if not res.ok:
        return []
    pairs: list[tuple[str, str]] = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        name, _, date = line.partition("\t")
        pairs.append((name.strip(), date.strip()))
    return pairs


def tag_exists(repo: str | Path, name: str) -> bool:
    res = _run(repo, ["rev-parse", "--verify", "--quiet", f"refs/tags/{name}"])
    return res.ok and bool(res.stdout.strip())


def create_tag(repo: str | Path, name: str, message: str = "") -> GitResult:
    """Create a tag at HEAD - annotated when a message is given, else lightweight."""
    if message.strip():
        return _run(repo, ["tag", "-a", name, "-F", "-"], stdin=message)
    return _run(repo, ["tag", name])


def delete_tag(repo: str | Path, name: str) -> GitResult:
    """Delete a local tag (does not touch the remote)."""
    return _run(repo, ["tag", "-d", name])


def remote_tag_exists(
    repo: str | Path, name: str, remote: str = "origin"
) -> bool | None:
    """Has ``name`` been pushed? None when the remote could not be reached.

    None is distinct from False on purpose: "not published" and "cannot tell"
    call for different answers before deleting a tag.
    """
    names = _run(repo, ["remote"])
    if not names.ok or not names.stdout.split():
        return False  # no remote at all, so nothing was ever pushed
    res = _run(repo, ["ls-remote", "--tags", remote, f"refs/tags/{name}"])
    if not res.ok:
        return None
    return bool(res.stdout.strip())


def push_tag(repo: str | Path, name: str, remote: str = "origin") -> GitResult:
    """Publish a single tag to ``remote``."""
    return _run(repo, ["push", remote, f"refs/tags/{name}"])


def get_upstream(repo: str | Path) -> str | None:
    """Return the upstream ref for the current branch (e.g. ``origin/main``)."""
    res = _run(repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    return res.stdout.strip() if res.ok and res.stdout.strip() else None


def unpushed_count(repo: str | Path) -> int | None:
    """Commits on the current branch not yet on its upstream (None if no upstream)."""
    if get_upstream(repo) is None:
        return None
    res = _run(repo, ["rev-list", "--count", "@{u}..HEAD"])
    if not res.ok:
        return None
    try:
        return int(res.stdout.strip())
    except ValueError:
        return None


def push(repo: str | Path, remote: str = "origin") -> GitResult:
    """Push the current branch, setting upstream on first push.

    Never force-pushes; a rejected non-fast-forward is reported to the caller.
    """
    if get_upstream(repo) is not None:
        return _run(repo, ["push"])
    branch = current_branch(repo)
    return _run(repo, ["push", "--set-upstream", remote, branch])


# ---- branches --------------------------------------------------------------------
@dataclass
class BranchInfo:
    """One local branch, and how it stands against what it tracks."""

    name: str
    current: bool = False
    upstream: str = ""  # "origin/main"; "" when it tracks nothing
    ahead: int = 0  # commits it has that its upstream has not
    behind: int = 0
    subject: str = ""  # the tip commit's summary line

    def tracking_label(self) -> str:
        """What a list shows beside the name. Empty when there is nothing to say."""
        if not self.upstream:
            return "no upstream"
        parts = []
        if self.ahead:
            parts.append(f"{self.ahead} ahead")
        if self.behind:
            parts.append(f"{self.behind} behind")
        return ", ".join(parts) or "up to date"


def branch_exists(repo: str | Path, name: str) -> bool:
    res = _run(repo, ["rev-parse", "--verify", "--quiet", f"refs/heads/{name}"])
    return res.ok and bool(res.stdout.strip())


def blocking_branch(existing, name: str) -> str:
    """The branch that makes ``name`` impossible to create, or ``""``.

    Git keeps refs as paths, so ``dev`` is a file and ``dev/rem/x`` needs
    ``dev`` to be a directory. The two cannot both exist, in either order::

        fatal: cannot lock ref 'refs/heads/dev/rem/x': 'refs/heads/dev' exists
        fatal: cannot lock ref 'refs/heads/dev': 'refs/heads/dev/rem/x' exists

    Which is a perfectly clear message arriving at the worst moment: after the
    window has offered the name as the one that will be created, and after the
    button has been pressed. Asked here from a list already on screen, so it
    costs no git call and can be answered while the name is being typed.

    Takes the names rather than a repository for the same reason: this is
    called on every keystroke.
    """
    if not name:
        return ""
    names = set(existing)
    # Something above it is a branch, so there is no directory to put it in.
    parts = name.split("/")
    for depth in range(1, len(parts)):
        prefix = "/".join(parts[:depth])
        if prefix in names:
            return prefix
    # Or something below it is, so the name is already a directory. Sorted, so
    # a repository with several of them names the same one twice running.
    below = f"{name}/"
    return next((one for one in sorted(names) if one.startswith(below)), "")


def list_branch_info(repo: str | Path) -> list[BranchInfo]:
    """Every local branch with its upstream and how far it has drifted.

    One `for-each-ref` rather than a `rev-list` per branch: a repository with
    forty branches would otherwise be forty processes to draw one list, and on
    Windows the processes are the cost.

    Recency order, as `list_branches` is, and for the same reason: the branch
    someone is working on is the one they are looking for.
    """
    # %(upstream:track) is "[ahead 2, behind 1]", which is the same two numbers
    # git would give for two more commands.
    fields = ("refname:short", "upstream:short", "upstream:track", "HEAD", "subject")
    separator = "\x1f"
    res = _run(
        repo,
        [
            "for-each-ref",
            f"--format={separator.join('%(' + f + ')' for f in fields)}",
            "--sort=-committerdate",
            "refs/heads",
        ],
    )
    if not res.ok:
        return []

    branches: list[BranchInfo] = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        # Split only as many times as there are fields, so a separator inside
        # the last one stays in it. The subject is last precisely because it is
        # the field a commit message gets to choose, and a branch must not
        # disappear from this list because of what was written about it.
        parts = line.split(separator, len(fields) - 1)
        if len(parts) != len(fields):
            continue
        name, upstream, track, head, subject = parts
        ahead = re.search(r"ahead (\d+)", track)
        behind = re.search(r"behind (\d+)", track)
        branches.append(
            BranchInfo(
                name=name.strip(),
                current=head.strip() == "*",
                upstream=upstream.strip(),
                ahead=int(ahead.group(1)) if ahead else 0,
                behind=int(behind.group(1)) if behind else 0,
                subject=subject.strip(),
            )
        )
    return branches


def create_branch(
    repo: str | Path,
    name: str,
    *,
    start_point: str = "",
    switch: bool = True,
) -> GitResult:
    """Create ``name``, from ``start_point`` or from HEAD, and check it out.

    ``git switch -c`` rather than ``branch`` then ``switch``: one command that
    either does both or does neither, so a failure to check out cannot leave a
    branch nobody asked for lying around.

    Never ``-C``/``--force``: creating a branch over one that exists is how the
    branch that was there stops existing, and this is offered from a text field.
    """
    if not name:
        return GitResult(ok=False, stdout="", stderr="No branch name given.", returncode=1)
    if switch:
        args = ["switch", "--create", name]
    else:
        args = ["branch", name]
    if start_point:
        args.append(start_point)
    return _run(repo, args)


def delete_branch(repo: str | Path, name: str, *, force: bool = False) -> GitResult:
    """Delete a local branch. Does not touch the remote.

    ``-d`` unless ``force``: git refuses to delete a branch whose commits are
    on no other branch, and that refusal is the last thing standing between a
    button and somebody's afternoon. The caller that wants it gone anyway has
    to say so, and should have asked first.
    """
    return _run(repo, ["branch", "-D" if force else "-d", name])


def delete_remote_branch(
    repo: str | Path, name: str, remote: str = "origin"
) -> GitResult:
    """Delete ``name`` on ``remote``.

    Spelled `--delete <name>` rather than the `:refs/heads/<name>` refspec: the
    two do the same thing, and only one of them can be read by someone who is
    about to approve it.
    """
    return _run(repo, ["push", remote, "--delete", name])


def push_branch(
    repo: str | Path,
    name: str,
    *,
    remote: str = "origin",
    set_upstream: bool = True,
) -> GitResult:
    """Publish one branch. Never force-pushes.

    ``set_upstream`` is asked of the configuration rather than assumed, but a
    branch that already tracks something is left tracking it: re-pointing an
    upstream is a different act from pushing, and not one anybody asked for.
    """
    args = ["push"]
    if set_upstream and not branch_upstream(repo, name):
        args.append("--set-upstream")
    args += [remote, name]
    return _run(repo, args)


def branch_upstream(repo: str | Path, name: str) -> str:
    """What ``name`` tracks, or ``""``. Unlike `get_upstream`, not only HEAD's."""
    res = _run(
        repo,
        ["for-each-ref", "--format=%(upstream:short)", f"refs/heads/{name}"],
    )
    return res.stdout.strip() if res.ok else ""


# ---- fetching --------------------------------------------------------------------
def fetch(
    repo: str | Path,
    *,
    remote: str = "",
    depth: int | None = None,
    prune: bool = True,
    tags: bool = True,
) -> GitResult:
    """Bring refs up to date without touching the working tree.

    ``depth`` asks for a shallow fetch: that many commits per ref and no more.
    On a repository that already has its whole history this *deepens nothing*
    and truncates what it fetches -- see `is_shallow` and `unshallow`, which are
    how it is undone. It is offered because cloning a large history to read one
    branch is a wait nobody needs to have.

    ``--no-write-fetch-head``: fetching is something this application does on
    the user's behalf, and overwriting FETCH_HEAD would quietly change what
    their next `git merge FETCH_HEAD` means.
    """
    args = ["fetch", "--no-write-fetch-head"]
    if prune:
        args.append("--prune")
    args.append("--tags" if tags else "--no-tags")
    if depth is not None:
        args.append(f"--depth={max(1, depth)}")
    if remote:
        args.append(remote)
    return _run(repo, args)


def is_shallow(repo: str | Path) -> bool:
    """Whether this repository holds a truncated history."""
    res = _run(repo, ["rev-parse", "--is-shallow-repository"])
    return res.ok and res.stdout.strip() == "true"


def unshallow(repo: str | Path, remote: str = "origin") -> GitResult:
    """Fetch the rest of a shallow repository's history.

    Refuses on a repository that is not shallow rather than passing
    ``--unshallow`` to git, which fails with "--unshallow on a complete
    repository does not make sense" -- true, and not an answer to give someone
    who pressed a button offering to do it.
    """
    if not is_shallow(repo):
        return GitResult(
            ok=False,
            stdout="",
            stderr="This repository already has its whole history.",
            returncode=1,
        )
    return _run(repo, ["fetch", "--unshallow", remote])


def commit(repo: str | Path, message: str) -> GitResult:
    """Create a commit with the given (multi-line) message.

    The message is passed via stdin (`-F -`) so newlines and special characters
    survive intact. Only staged changes are committed.
    """
    return _run(repo, ["commit", "-F", "-"], stdin=message)


# ---- cloning and creating ------------------------------------------------------------
#: Variables that point git at a repository other than the one a command names.
#: Inherited from whatever started this application (a hook, an IDE), they would
#: make a clone or an init act on that repository instead.
_REDIRECTING_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")

#: Seconds a cancelled clone's folder is retried for while something still holds
#: a file in it open: git's own children as they die, a virus scanner.
_REMOVE_FOR = 3.0

#: Seconds of silence waited for once git has exited, for output still in the
#: pipe -- bounded, because a child that outlives git can hold the pipe open.
_LAST_WORDS = 2.0

_PERCENT_RE = re.compile(r"(\d{1,3})%")


@dataclass
class CloneResult(GitResult):
    """How a clone ended, and what it left on disk."""

    destination: str = ""
    #: Stopped because it was asked to be. What it had written is removed.
    cancelled: bool = False
    #: Everything was fetched but the files could not all be written out -- a
    #: path too long for Windows is the usual cause. The repository is kept: it
    #: is complete, and deleting a finished download to report an error would
    #: help nobody.
    checkout_failed: bool = False
    #: A path the clean-up after a cancel could not remove, or "".
    leftover: str = ""


def repo_name_from_url(url: str) -> str:
    """The folder name `git clone` would choose for ``url``.

    The last part of the path without a trailing ``.git`` or ``/.git`` -- alike
    for https and ssh URLs, ``git@host:org/repo.git`` and local folders.
    """
    text = url.strip().rstrip("/\\")
    if text.lower().endswith(("/.git", "\\.git")):
        text = text[:-5].rstrip("/\\")
    if text.lower().endswith(".git"):
        text = text[:-4]
    return re.split(r"[/\\:]", text)[-1].strip()


def _local_folder(url: str) -> Path | None:
    """``url`` as a folder on this machine, or None when it is a URL.

    ``host:path`` is git's ssh shorthand and ``C:\\work`` is a drive; asking the
    file system settles which, where reading the text would have to guess.
    """
    if "://" in url:
        return None
    try:
        folder = Path(url).expanduser()
        return folder if folder.is_dir() else None
    except (OSError, ValueError):
        return None


def clone(
    url: str,
    destination: str | Path,
    *,
    depth: int | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_percent: Callable[[int], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> CloneResult:
    """Clone ``url`` into ``destination``, passing on git's progress as it comes.

    ``depth`` makes the clone shallow: that many commits from the tip of *every*
    branch. Git's own shallow clone takes only the branch HEAD names and narrows
    the remote's fetch refspec to it, which keeps every other branch out of reach
    for good -- the Branches & Tags tab included. A local folder is cloned over
    ``file://`` then, because git ignores a depth for a plain path and says so
    only as a warning.

    Refused before git starts: no URL, one that starts with ``-``, and a
    destination that exists and is not an empty folder.

    Cancelling kills git and everything it started, then removes what the clone
    had written; a destination folder that already existed is left there, empty.
    Git removes its own leftovers when a clone fails on its own.
    """
    dest = Path(destination)
    source = url.strip()

    def result(ok: bool, *, stdout="", stderr="", returncode=1, **extra) -> CloneResult:
        return CloneResult(
            ok=ok,
            stdout=stdout,
            stderr=stderr,
            returncode=returncode,
            destination=str(dest),
            **extra,
        )

    if not source:
        return result(False, stderr="There is no URL to clone.")
    if source.startswith("-"):
        return result(False, stderr=f"'{source}' is not something git can clone.")
    existed = dest.exists()
    if existed and (not dest.is_dir() or any(dest.iterdir())):
        return result(False, stderr=f"{dest} exists and is not an empty folder.")

    local = _local_folder(source)
    if depth is not None and local is not None:
        source = local.resolve().as_uri()
    args = ["clone", "--progress"]
    if depth is not None:
        args += [f"--depth={max(1, depth)}", "--no-single-branch"]
    args += ["--", source, str(dest)]

    stopped = is_cancelled or (lambda: False)

    def seen(line: str) -> None:
        if on_progress is not None:
            on_progress(line)
        if on_percent is not None and (match := _PERCENT_RE.search(line)):
            on_percent(min(100, int(match.group(1))))

    try:
        code, tail = _stream(
            ["git", *args], env=_clone_env(), on_line=seen, is_cancelled=stopped
        )
    except OSError as exc:
        missing = _cannot_run(exc)
        return result(False, stderr=missing.stderr, returncode=missing.returncode)

    # Asked once more after git is done: a Cancel pressed as the last line
    # arrived still means the person did not want this repository.
    if code is None or stopped():
        leftover = _remove_partial(dest, keep_folder=existed)
        return result(False, stderr="Cancelled.", cancelled=True, leftover=leftover)
    if code == 0:
        return result(True, stdout="\n".join(tail), returncode=0)
    return result(
        False,
        stderr=_complaints(tail) or "git clone failed.",
        returncode=code,
        checkout_failed=(dest / ".git").is_dir(),
    )


def _complaints(lines: list[str]) -> str:
    """What git said went wrong, out of everything it wrote; else its last words."""
    said = [line for line in lines if line.startswith(("fatal:", "error:", "warning:"))]
    return "\n".join(said or lines[-3:])


def _clone_env() -> dict[str, str]:
    """This process's environment, minus what would redirect git, prompts off.

    There is no terminal to type a password into, so git is told not to ask for
    one: it reports that it could not authenticate instead of waiting for an
    answer nobody can give. A credential helper with a window of its own -- Git
    Credential Manager -- still shows it.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in _REDIRECTING_ENV
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _stream(
    argv: list[str],
    *,
    env: dict[str, str] | None,
    on_line: Callable[[str], None],
    is_cancelled: Callable[[], bool],
    poll: float = 0.1,
) -> tuple[int | None, list[str]]:
    """Run ``argv``, handing ``on_line`` each line it writes while it runs.

    Returns the exit code -- None when it was cancelled -- and the last lines.
    Raises OSError when the program cannot be started at all.

    Git redraws a progress line in place with a carriage return, so a line ends
    at ``\\r`` as well as ``\\n``. The pipe is read on a thread of its own: a
    stalled network means no output for as long as the stall lasts, and Cancel
    has to work through exactly that.
    """
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        **processes.killable(),
    )
    lines: queue.SimpleQueue[str | None] = queue.SimpleQueue()

    def read() -> None:
        pending = b""
        try:
            while chunk := proc.stdout.read1(65536):
                *complete, pending = re.split(rb"[\r\n]", pending + chunk)
                for raw in complete:
                    if text := raw.decode("utf-8", "replace").strip():
                        lines.put(text)
            if text := pending.decode("utf-8", "replace").strip():
                lines.put(text)
        except (OSError, ValueError):
            pass  # the pipe went away under a kill
        finally:
            lines.put(None)

    reader = threading.Thread(target=read, name="git-output", daemon=True)
    reader.start()
    tail: deque[str] = deque(maxlen=50)
    quiet = 0.0
    try:
        while True:
            if is_cancelled():
                # The whole tree: the git on PATH is a launcher, and killing it
                # alone leaves the clone running. See git_assistant.processes.
                processes.kill_tree(proc)
                return None, list(tail)
            try:
                line = lines.get(timeout=poll)
            except queue.Empty:
                if proc.poll() is not None:
                    quiet += poll
                    if quiet >= _LAST_WORDS:
                        break
                continue
            if line is None:
                break
            quiet = 0.0
            tail.append(line)
            on_line(line)
        return proc.wait(), list(tail)
    finally:
        # A git that finished is left alone; one still running here is one whose
        # output handler raised.
        processes.kill_tree(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        reader.join(timeout=2)
        # Closing the pipe while the reader is still inside a read would wait
        # for that read to end; a finished reader has nothing left to wait on.
        if not reader.is_alive():
            proc.stdout.close()


def _remove_partial(dest: Path, *, keep_folder: bool) -> str:
    """Delete what a cancelled clone wrote into ``dest``; "" once all of it is gone.

    Git makes the objects it writes read-only, which Windows refuses to delete, so
    each is made writable first. Something can still hold a file open for a
    moment afterwards, so removal is retried for a few seconds before the path
    is handed back as left behind. ``keep_folder`` empties ``dest`` rather than
    removing it: it existed before the clone did.
    """

    def writable(function, path, _exc) -> None:
        os.chmod(path, stat.S_IWRITE)
        function(path)

    deadline = time.monotonic() + _REMOVE_FOR
    while True:
        try:
            if not dest.exists():
                return ""
            if not keep_folder:
                shutil.rmtree(dest, onexc=writable)
                return ""
            for child in list(dest.iterdir()):
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, onexc=writable)
                else:
                    os.chmod(child, stat.S_IWRITE)
                    child.unlink()
            return ""
        except OSError:
            if time.monotonic() >= deadline:
                return str(dest)
            time.sleep(0.2)


def init(path: str | Path, *, initial_branch: str = "") -> GitResult:
    """Make ``path`` a new repository, creating the folder (and parents) if needed.

    Refuses a folder that already is one. Git would "reinitialize" it and report
    success, which reads as though a repository had just been created.
    """
    target = Path(path)
    if has_git_dir(target):
        return GitResult(
            ok=False,
            stdout="",
            stderr=f"{target} is already a git repository.",
            returncode=1,
        )
    args = ["init"]
    if initial_branch:
        args.append(f"--initial-branch={initial_branch}")
    return _run_global([*args, "--", str(target)])


def default_branch_name() -> str:
    """The branch a new repository starts on, as configured, or "" when unset.

    ``init.defaultBranch`` is read from the global config and then the system
    one -- Git for Windows' installer writes it into the latter -- and never from
    whatever repository this process happens to be running in.
    """
    for scope in ("--global", "--system"):
        res = _run_global(["config", scope, "--get", "init.defaultBranch"])
        if res.ok and res.stdout.strip():
            return res.stdout.strip()
    return ""


def valid_branch_name(name: str) -> bool:
    """Whether git accepts ``name`` for a branch, asked of git itself."""
    return bool(name) and _run_global(["check-ref-format", "--branch", name]).ok
