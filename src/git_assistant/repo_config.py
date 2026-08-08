"""Settings that belong to a repository rather than to the person using it.

Three tiers, nearest wins:

    built-in defaults                              this file
    <config dir>/default_repo_settings.json        the user's answer for every repo
    <repo>/.git_assistant/repo_settings.json       this project's answer

The middle tier does two jobs, and they are the same job. It is what a
repository added tomorrow is configured with before anyone configures it -- so
a repository with no file of its own is not unconfigured -- and it is the
known-good copy to go back to when a file has been edited into something that
does not work. Both are "the answer when the nearer one is no use", which is
why there is one file rather than two.

Being a fallback is what it is *for*, so nothing may depend on it being
readable: every value in it also exists as a constant here, and
``restore_defaults`` writes those back over it. There is no tier below that can
go missing.

The third tier is *in the repository*, so a convention that belongs to a
project -- where its branches are named, whether its history is worth fetching
whole -- travels with the project rather than living in one person's settings
file on one machine.

Merging is per key, not per file. A repository that sets only ``fetch.depth``
keeps the user's branch pattern; anything else would mean copying every setting
into every repository to change one of them.

Nothing here runs git or reads a widget. It answers "what is configured", and
the callers decide what to do about it -- which is why ``BranchRules.render``
is handed a user name rather than going and finding one.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant.config import APP_NAME

SCHEMA_VERSION = 1

#: Where a repository keeps its own answer. A directory rather than a bare
#: file: it is where anything else this application ever puts *in* a repository
#: belongs, and one dotted name in a project root is easier to explain than
#: three.
REPO_DIR = ".git_assistant"
REPO_FILE = "repo_settings.json"

#: The user's answer for repositories that do not have one.
DEFAULTS_FILE = "default_repo_settings.json"


# ---- branch names ------------------------------------------------------------------
#: Characters a branch name may keep. Git's rules are longer than this (see
#: git-check-ref-format) and are enforced by git itself when the branch is
#: created; this is only about not building a name that was never going to work.
_UNSAFE = re.compile(r"[^\w./-]+")


def slug(text: str) -> str:
    """``text`` as a piece of a branch name, or ``""`` if nothing survives.

    Case is kept. A ticket is ``JIRA-412`` and lower-casing it makes it
    something else, which matters more than tidiness.
    """
    cleaned = _UNSAFE.sub("-", (text or "").strip())
    cleaned = cleaned.replace("..", ".")  # git refuses a ref containing ".."
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return cleaned.strip("-./")


@dataclass
class BranchRules:
    """Where a new branch's name comes from.

    ``pattern`` carries ``{user}`` and ``{name}``. Anything else is left where
    it is rather than blanked, so a mistyped placeholder shows up in the name
    the window offers instead of quietly disappearing from it.
    """

    pattern: str = "{name}"
    #: Who ``{user}`` is. Blank means "ask git", which the caller does -- this
    #: file does not run git.
    user: str = ""
    push_sets_upstream: bool = True

    def render(self, name: str, *, user: str = "") -> str:
        """The full branch name for ``name``. Empty when nothing is left of it."""
        rendered = self.pattern.replace("{user}", slug(self.user or user)).replace(
            "{name}", slug(name)
        )
        # A pattern whose {user} resolved to nothing leaves "dev//thing", which
        # git refuses. Dropping the empty piece is what was meant.
        parts = [part for part in rendered.split("/") if part]
        return "/".join(parts)


@dataclass
class FetchRules:
    """How much history a fetch asks for."""

    shallow: bool = False
    depth: int = 1
    prune: bool = True
    tags: bool = True

    def effective_depth(self) -> int | None:
        """Commits to fetch per ref, or ``None`` for the whole history.

        The depth is kept when shallow is off, so turning it back on restores
        the number that was chosen rather than resetting it to one.
        """
        return max(1, self.depth) if self.shallow else None


@dataclass
class RepoSettings:
    """What is configured for one repository, and where that came from."""

    branch: BranchRules = field(default_factory=BranchRules)
    fetch: FetchRules = field(default_factory=FetchRules)
    #: The files that were read, furthest first. Empty means the built-in
    #: defaults and nothing else.
    sources: list[str] = field(default_factory=list)
    #: Why a file that exists was not used. Kept rather than raised: a settings
    #: file with a comma out of place must not stop the application, and must
    #: not silently behave as though it said something else either.
    problem: str = ""

    def to_dict(self) -> dict:
        return {
            "version": SCHEMA_VERSION,
            "branch": {
                "pattern": self.branch.pattern,
                "user": self.branch.user,
                "push_sets_upstream": self.branch.push_sets_upstream,
            },
            "fetch": {
                "shallow": self.fetch.shallow,
                "depth": self.fetch.depth,
                "prune": self.fetch.prune,
                "tags": self.fetch.tags,
            },
        }


# ---- where the files are -------------------------------------------------------------
def defaults_path() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False)) / DEFAULTS_FILE


def repo_dir(repo_path: str | Path) -> Path:
    return Path(repo_path) / REPO_DIR


def repo_config_path(repo_path: str | Path) -> Path:
    return repo_dir(repo_path) / REPO_FILE


def has_repo_config(repo_path: str | Path) -> bool:
    return bool(repo_path) and repo_config_path(repo_path).is_file()


# ---- reading -------------------------------------------------------------------------
def _read(path: Path) -> tuple[dict | None, str]:
    """``(data, problem)``. ``(None, "")`` when the file is simply not there."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ""
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{path.name} could not be read: {exc}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{path.name} is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name} does not hold a settings object."
    return data, ""


def _section(data: dict, name: str) -> dict:
    section = data.get(name)
    return section if isinstance(section, dict) else {}


def _str(data: dict, key: str, fallback: str) -> str:
    value = data.get(key, fallback)
    return value if isinstance(value, str) else fallback


def _bool(data: dict, key: str, fallback: bool) -> bool:
    value = data.get(key, fallback)
    return value if isinstance(value, bool) else fallback


def _int(data: dict, key: str, fallback: int) -> int:
    value = data.get(key, fallback)
    # bool is an int in Python, and `"depth": true` means nothing here.
    return value if isinstance(value, int) and not isinstance(value, bool) else fallback


def _overlay(settings: RepoSettings, data: dict) -> RepoSettings:
    """Apply the keys ``data`` sets, and leave the rest of ``settings`` alone."""
    branch = _section(data, "branch")
    fetch = _section(data, "fetch")
    return RepoSettings(
        branch=BranchRules(
            pattern=_str(branch, "pattern", settings.branch.pattern),
            user=_str(branch, "user", settings.branch.user),
            push_sets_upstream=_bool(
                branch, "push_sets_upstream", settings.branch.push_sets_upstream
            ),
        ),
        fetch=FetchRules(
            shallow=_bool(fetch, "shallow", settings.fetch.shallow),
            depth=_int(fetch, "depth", settings.fetch.depth),
            prune=_bool(fetch, "prune", settings.fetch.prune),
            tags=_bool(fetch, "tags", settings.fetch.tags),
        ),
        sources=list(settings.sources),
        problem=settings.problem,
    )


# ---- resolving -----------------------------------------------------------------------
def resolve(repo_path: str | Path = "") -> RepoSettings:
    """What is configured for ``repo_path``, defaults included.

    Read every time, and deliberately not cached against the files'
    timestamps. Two edits of the same length inside one tick of the
    filesystem's clock are indistinguishable by ``(mtime, size)``, and the
    stale answer that comes back is a setting that will not take -- for a
    saving of two small file reads.

    A caller doing this per keystroke should resolve once and hold the object.
    That is a decision about one screen, and it belongs on that screen rather
    than in a cache here that every other caller has to reason about.
    """
    key = str(repo_path or "")
    settings = RepoSettings()
    problems: list[str] = []
    for path in (defaults_path(), repo_config_path(repo_path) if key else None):
        if path is None:
            continue
        data, problem = _read(path)
        if problem:
            problems.append(problem)
            continue
        if data is None:
            continue
        settings = _overlay(settings, data)
        settings.sources.append(str(path))
    settings.problem = " ".join(problems)
    return settings


def defaults() -> RepoSettings:
    """What every repository gets before its own file has its say."""
    return resolve("")


# ---- writing the defaults ------------------------------------------------------------
def _write(path: Path, data: dict) -> None:
    """Replaced, never truncated: a torn write must not eat the settings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def save_defaults(settings: RepoSettings) -> str:
    """Write the user's answer for every repository. Returns a problem, or ``""``."""
    try:
        _write(defaults_path(), settings.to_dict())
    except OSError as exc:
        return str(exc)
    return ""


def read_repo_text(repo_path: str | Path) -> str:
    """One repository's file exactly as it is on disk, for showing and editing.

    Not the resolved settings: what is edited has to be what was written, down
    to the key that was left out on purpose.
    """
    try:
        return repo_config_path(repo_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def check(text: str) -> str:
    """Why ``text`` is not a settings file, or ``""`` if it is one."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"Not valid JSON: {exc}"
    return "" if isinstance(data, dict) else "A settings file holds an object, not a list."


def write_repo_text(repo_path: str | Path, text: str) -> str:
    """Save one repository's file. Returns a problem, or ``""``.

    Refuses to write what cannot be read back. A settings file is edited here
    and read by every run afterwards, and the run that finds it broken is a
    long way from the keystroke that broke it.
    """
    problem = check(text)
    if problem:
        return problem
    try:
        _write(repo_config_path(repo_path), json.loads(text))
    except OSError as exc:
        return str(exc)
    return ""


def starter_text() -> str:
    """The defaults, written out as a repository's own file would hold them.

    Every key, so the file can be read to find out what there is to set -- a
    file holding only the keys someone might want is one that answers nothing.

    Built from the defaults and deliberately not from what the repository
    currently resolves to. At creation there is no difference: a repository
    with no file of its own *is* the defaults. There is all the difference on a
    reset, where "what it resolves to" includes the file being reset, and
    faithfully reproduces whatever went wrong with it.
    """
    return json.dumps(defaults().to_dict(), indent=2, ensure_ascii=False) + "\n"


def create_repo_config(repo_path: str | Path) -> str:
    """Write a starter file for ``repo_path``. Returns a problem, or ``""``.

    Refuses rather than replacing: this is offered where there is no file, and
    an offer that overwrites one is a way to lose a config nobody meant to lose.
    """
    if has_repo_config(repo_path):
        return "That repository already has a settings file."
    return write_repo_text(repo_path, starter_text())


def reset_repo_config(repo_path: str | Path) -> str:
    """Put one repository's file back to the defaults. A problem, or ``""``.

    Replaces, where `create_repo_config` refuses -- which is the point of it.
    This is what is reached for when a file has been edited into something that
    does not work, and the answer to that cannot be "you already have one".
    """
    return write_repo_text(repo_path, starter_text())


def remove_repo_config(repo_path: str | Path) -> str:
    """Delete one repository's file so it inherits again. A problem, or ``""``.

    Distinct from resetting it: a repository with no file follows the defaults
    as they change from now on, where a reset one holds a copy of what they
    said today.
    """
    try:
        repo_config_path(repo_path).unlink(missing_ok=True)
    except OSError as exc:
        return str(exc)
    return ""


def restore_defaults() -> str:
    """Put the defaults back to the values this build ships with.

    The last thing standing when the file has been edited into nonsense: every
    tier below it is a constant in this module, so this cannot fail for want of
    something to restore *from*.
    """
    return save_defaults(RepoSettings())


def ensure_defaults() -> bool:
    """Put the built-in defaults on disk if nothing is there. True if written.

    So that the file exists to be found and read before anyone has changed
    anything in it: a settings file that appears only once it has been edited
    cannot be discovered by looking.
    """
    if defaults_path().exists():
        return False
    return save_defaults(RepoSettings()) == ""
