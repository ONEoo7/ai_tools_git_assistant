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
    """
    found: list[str] = []
    seen: set[str] = set()

    def walk(base: Path, depth: int) -> None:
        if depth > max_depth:
            return
        for rel in _gitmodules_paths(base):
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
