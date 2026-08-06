"""Who uses which submodule, at which version, across every repository.

Three things decide whether this answers anything useful.

**A submodule is its URL, not its path.** One team vendors a dependency at
`vendor/lib` and another at `third_party/lib`. By path those are two things; by
URL they are one, and only the second answer makes "how many repositories use
this" a number worth reading. URLs are normalised before they are compared --
`git@host:owner/repo.git` and `https://host/owner/repo` are the same place.

**The version describes the commit the parent pins**, read out of the parent's
`HEAD` tree, not the commit that happens to be checked out. A working tree
someone left on a branch is a *finding* -- drift -- not a different version.
Using the checked-out commit would make one repository report different versions
on two laptops and turn the comparison into noise.

**A submodule that was never initialised still counts.** The parent records a
commit whether or not anything was cloned; that repository is using it. The
version is unknown, and the row says so rather than being left out.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from git_assistant.git_ops import _run

#: `.gitmodules` is INI-ish: one `[submodule "name"]` section per entry, each
#: with `path` and `url` in either order. Parsed rather than regex-swept for
#: paths alone, because a path without its URL cannot be compared to anything.
_SECTION = re.compile(r'^\s*\[submodule\s+"(?P<name>.*)"\]\s*$')
_SETTING = re.compile(r"^\s*(?P<key>path|url)\s*=\s*(?P<value>.+?)\s*$")

#: `git ls-tree` marks a submodule entry with this mode.
GITLINK = "160000"

#: Said in place of a version that could not be determined.
UNKNOWN = "unknown"
NOT_CHECKED_OUT = "not checked out"


@dataclass(frozen=True)
class Declared:
    """One entry in a `.gitmodules` file."""

    name: str
    path: str
    url: str


@dataclass(frozen=True)
class Use:
    """One repository's use of one submodule."""

    repo: str  # the parent repository's path
    path: str  # where it sits inside the parent
    url: str  # as written in .gitmodules
    key: str  # the normalised url; what identifies it across repositories
    pinned: str = ""  # the commit the parent's HEAD records
    version: str = UNKNOWN  # that commit, described by tags
    #: The working tree is at a different commit from the one recorded. Real,
    #: and invisible until someone else clones and gets something else.
    drifted: bool = False
    checked_out: str = ""  # the working tree's commit, when it differs

    @property
    def repo_name(self) -> str:
        return Path(self.repo).name or self.repo

    def short_pin(self) -> str:
        return self.pinned[:8] if self.pinned else "-"


@dataclass
class Fleet:
    """Every use of every submodule found, and what could not be read."""

    uses: list[Use] = field(default_factory=list)
    #: Repositories that were looked at, whether or not they had submodules --
    #: the denominator of "3 of 14 repositories use this".
    scanned: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def by_key(self) -> dict[str, list[Use]]:
        found: dict[str, list[Use]] = {}
        for use in self.uses:
            found.setdefault(use.key, []).append(use)
        return found

    def versions_of(self, key: str) -> list[str]:
        """The distinct versions in use, in the order first seen."""
        seen: list[str] = []
        for use in self.by_key().get(key, []):
            if use.version not in seen:
                seen.append(use.version)
        return seen

    def disagreements(self) -> dict[str, list[Use]]:
        """Submodules used at more than one version. The point of all this."""
        return {
            key: uses
            for key, uses in self.by_key().items()
            if len({u.version for u in uses}) > 1
        }

    def drifted(self) -> list[Use]:
        return [u for u in self.uses if u.drifted]


# ---- .gitmodules ------------------------------------------------------------------
def declared_in(repo: str) -> list[Declared]:
    """Submodules a repository declares, with their URLs. Never raises.

    Read from the file rather than through `git submodule`, for the reason
    `git_ops._gitmodules_paths` gives: it stays subprocess-free, and a
    repository git refuses to touch still reports what it declares.
    """
    try:
        text = (Path(repo) / ".gitmodules").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return []

    found: list[Declared] = []
    name, values = "", {}

    def flush() -> None:
        if name and values.get("path"):
            found.append(
                Declared(name=name, path=values["path"], url=values.get("url", ""))
            )

    for line in text.splitlines():
        header = _SECTION.match(line)
        if header:
            flush()
            name, values = header.group("name"), {}
            continue
        setting = _SETTING.match(line)
        if setting and name:
            values[setting.group("key")] = setting.group("value")
    flush()
    return found


def normalize_url(url: str) -> str:
    """One key for one remote, however it was written.

    ``git@github.com:ONEoo7/thing.git``, ``https://github.com/ONEoo7/thing`` and
    ``ssh://git@github.com/ONEoo7/thing/`` are the same place, and a fleet report
    that treated them as three would answer "how many repositories use this"
    with a number that is wrong in the direction that hides the problem.
    """
    text = (url or "").strip()
    if not text:
        return ""
    text = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", text)
    text = re.sub(r"^[^/@]+@", "", text)  # user@ in front of the host
    text = text.replace(":", "/", 1) if "/" not in text.split(":", 1)[0] else text
    text = text.rstrip("/")
    if text.lower().endswith(".git"):
        text = text[:-4]
    return text.lower()


# ---- what a parent pins -------------------------------------------------------------
def pinned_commit(repo: str, path: str) -> str:
    """The commit the parent's HEAD records for a submodule, or "".

    This is what a fresh clone checks out, which is why it and not the working
    tree is the version everyone is compared on.
    """
    found = _run(repo, ["ls-tree", "HEAD", "--", path.replace(os.sep, "/")])
    if not found.ok:
        return ""
    for line in found.stdout.splitlines():
        # "160000 commit <sha>\t<path>"
        parts = line.split()
        if len(parts) >= 3 and parts[0] == GITLINK:
            return parts[2]
    return ""


def describe(repo: str, commit: str) -> str:
    """``commit`` as a version, using the tags in ``repo``.

    ``--tags`` so lightweight tags count, and ``--always`` is deliberately *not*
    used: a bare abbreviated sha is not a version, and returning one would put
    something that looks like an answer in a column headed "Version".
    """
    if not commit:
        return UNKNOWN
    found = _run(repo, ["describe", "--tags", commit])
    if found.ok and found.stdout.strip():
        return found.stdout.strip()
    return UNKNOWN


def head_commit(repo: str) -> str:
    found = _run(repo, ["rev-parse", "HEAD"])
    return found.stdout.strip() if found.ok else ""


def uses_in(repo: str) -> list[Use]:
    """Every submodule this repository declares, resolved to a version."""
    out: list[Use] = []
    for declared in declared_in(repo):
        where = Path(repo) / declared.path
        pinned = pinned_commit(repo, declared.path)
        present = (where / ".git").exists()
        checked_out = head_commit(str(where)) if present else ""
        out.append(
            Use(
                repo=repo,
                path=declared.path,
                url=declared.url,
                key=normalize_url(declared.url) or declared.path.lower(),
                pinned=pinned,
                # Described inside the submodule, where its tags are. Without a
                # checkout there is no object to describe and no tags to do it
                # with, so the row says that rather than inventing a version.
                version=describe(str(where), pinned) if present else NOT_CHECKED_OUT,
                drifted=bool(present and pinned and checked_out and checked_out != pinned),
                checked_out=checked_out,
            )
        )
    return out


def across(repos: list[str], *, on_repo=None, check_cancel=None) -> Fleet:
    """Every repository's submodules, gathered into one picture. Never raises.

    ``on_repo`` is called with each path before it is read, so a sweep of thirty
    repositories can be watched rather than waited out, and ``check_cancel`` is
    checked between them -- between, not after, so Cancel means something on the
    tenth of thirty.
    """
    fleet = Fleet()
    for repo in repos:
        if check_cancel is not None:
            check_cancel()
        if on_repo is not None:
            on_repo(repo)
        fleet.scanned.append(repo)
        try:
            fleet.uses.extend(uses_in(repo))
        except OSError as exc:
            # Named, never skipped in silence: "no submodules" and "could not
            # read this repository" must not look the same.
            fleet.problems.append(f"{Path(repo).name}: {exc}")
    return fleet
