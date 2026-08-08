"""Settings that belong to a repository rather than to the person using it.

Three of them, and exactly one is in force:

    user     <config dir>/default_repo_settings.json          every repository
    repo     <repo>/.git_assistant/repo_settings.json         this project
    custom   <config dir>/custom/<repo key>/custom_repo_settings.json   this one, mine

One and not a blend. An earlier design merged them per key, so a repository
file setting only ``fetch.depth`` inherited the rest; that is convenient right
up to the moment somebody has to answer "where did this value come from", and
the answer is three files and a precedence rule. What is in force here is one
file, named on screen, and merging two of them is something a person does on
purpose and can see the result of.

The built-in constants in this module are the only thing underneath, and they
are not a tier: they fill in what the file in force does not say, so a
hand-trimmed file still works. Nothing else is consulted.

Which one is in force is the *user's* choice, kept in their settings -- a
repository cannot decide it is not being read. Nobody having chosen, a
repository with a file of its own uses it, which is what the merged design did
for anyone who never thought about it.

The repo tier is *in the repository*, so a convention that belongs to a project
-- where its branches are named, whether its history is worth fetching whole --
travels with the project. The custom tier is the opposite: one person's answer
for one repository, filed under `config.repo_key` so that two repositories both
called `api` are two directories and the folder is readable by whoever opens it.

The user tier doubles as the known-good copy to go back to, which is why
``restore_defaults`` exists and why every value in it is also a constant here.

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
from enum import StrEnum
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant.config import APP_NAME, DEFAULT_IGNORE_GLOBS, repo_key

SCHEMA_VERSION = 1

#: Where a repository keeps its own answer. A directory rather than a bare
#: file: it is where anything else this application ever puts *in* a repository
#: belongs, and one dotted name in a project root is easier to explain than
#: three.
REPO_DIR = ".git_assistant"
REPO_FILE = "repo_settings.json"

#: The user's answer for repositories that do not have one.
DEFAULTS_FILE = "default_repo_settings.json"

#: One person's answer for one repository. Under a directory named by
#: `config.repo_key` -- readable name, then the hash that makes it this
#: repository and not the other one with the same name.
CUSTOM_DIR = "custom"
CUSTOM_FILE = "custom_repo_settings.json"


class Tier(StrEnum):
    """Which settings are in force. Exactly one of them is."""

    USER = "user"
    REPO = "repo"
    CUSTOM = "custom"

    def label(self) -> str:
        return {"user": "User", "repo": "Repo", "custom": "Custom"}[self.value]


def tier_of(name: str) -> Tier | None:
    """``name`` as a tier, or ``None``. Never raises: this reads settings files."""
    try:
        return Tier(name)
    except ValueError:
        return None


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
class StaleRules:
    """When a branch counts as stale, and when deleting one may be proposed.

    Mirrors ``agents.branches.StaleRules``, which is the object the audit runs
    on. Two dataclasses rather than one import because this module answers
    "what is configured" for every part of the application and cannot depend on
    any of them; ``as_branch_rules`` is the one-line bridge.
    """

    months: int = 6
    #: Written out rather than imported, because this module depends on nothing
    #: else in the application -- and asserted equal to `agents.branches`'s own
    #: defaults by a test, because a mirror that drifts is worse than no mirror.
    protect: list[str] = field(
        default_factory=lambda: [
            "main",
            "master",
            "develop",
            "trunk",
            "release/*",
            "hotfix/*",
        ]
    )
    merged_only: bool = True
    keep_unpushed: bool = True

    def as_branch_rules(self):
        from git_assistant.agents.branches import StaleRules as Branches

        return Branches(
            months=self.months,
            protect=list(self.protect),
            merged_only=self.merged_only,
            keep_unpushed=self.keep_unpushed,
        )


@dataclass
class AuditRules:
    """What the audits do, and how much of them is kept.

    ``narrate`` and ``fast`` read as run options rather than settings, and they
    are both: what they are *set to* is remembered per repository, because the
    repository is what decides whether a history scan is worth its minutes.
    """

    narrate: bool = True
    fast: bool = False
    large_file_mb: int = 5
    history_limit: int = 20
    #: Which audits a run carries out, and which one's report is on screen.
    selected: list[str] = field(default_factory=list)
    last: str = ""
    stale: StaleRules = field(default_factory=StaleRules)


@dataclass
class CommitRules:
    """What a generated commit message is made from, and how long it may be."""

    diff_mode: str = "cached"  # "cached" | "working"
    subject_target: int = 50
    subject_limit: int = 72
    body_limit: int = 1000
    #: Generated messages kept for this repository (0 keeps everything).
    history_limit: int = 20
    #: Paths not worth sending to a model. Per repository because what counts
    #: as noise is: a lock file in one project is the point of another.
    ignore_globs: list[str] = field(
        default_factory=lambda: list(DEFAULT_IGNORE_GLOBS)
    )


@dataclass
class ReviewRules:
    """What is kept from reviewing this repository."""

    history_limit: int = 20


@dataclass
class ModelRules:
    """How much of the model one run of this repository may use.

    Per repository because the repository is what decides it: a monorepo whose
    diffs are always larger than the window needs a different answer from a
    project whose diffs fit in it whole.
    """

    context_window: int = 32768  # 0 asks the provider
    safety_margin: float = 0.10
    parallel_calls: int = 4


@dataclass
class RepoSettings:
    """What is configured for one repository, and where that came from."""

    branch: BranchRules = field(default_factory=BranchRules)
    fetch: FetchRules = field(default_factory=FetchRules)
    audit: AuditRules = field(default_factory=AuditRules)
    commit: CommitRules = field(default_factory=CommitRules)
    review: ReviewRules = field(default_factory=ReviewRules)
    model: ModelRules = field(default_factory=ModelRules)
    #: Which settings these are. The window names it, and a value nobody can
    #: trace to a file is a value nobody trusts.
    tier: "Tier" = None  # set by `resolve`; see `Tier`
    #: The file that was read. Empty means it was not there and these are the
    #: built-in constants.
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
            "audit": {
                "narrate": self.audit.narrate,
                "fast": self.audit.fast,
                "large_file_mb": self.audit.large_file_mb,
                "history_limit": self.audit.history_limit,
                "selected": list(self.audit.selected),
                "last": self.audit.last,
                "stale": {
                    "months": self.audit.stale.months,
                    "protect": list(self.audit.stale.protect),
                    "merged_only": self.audit.stale.merged_only,
                    "keep_unpushed": self.audit.stale.keep_unpushed,
                },
            },
            "commit": {
                "diff_mode": self.commit.diff_mode,
                "subject_target": self.commit.subject_target,
                "subject_limit": self.commit.subject_limit,
                "body_limit": self.commit.body_limit,
                "history_limit": self.commit.history_limit,
                "ignore_globs": list(self.commit.ignore_globs),
            },
            "review": {"history_limit": self.review.history_limit},
            "model": {
                "context_window": self.model.context_window,
                "safety_margin": self.model.safety_margin,
                "parallel_calls": self.model.parallel_calls,
            },
        }


# ---- where the files are -------------------------------------------------------------
def _config_root() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False))


def defaults_path() -> Path:
    return _config_root() / DEFAULTS_FILE


def repo_dir(repo_path: str | Path) -> Path:
    return Path(repo_path) / REPO_DIR


def repo_config_path(repo_path: str | Path) -> Path:
    return repo_dir(repo_path) / REPO_FILE


def custom_dir(repo_path: str | Path) -> Path:
    return _config_root() / CUSTOM_DIR / repo_key(str(repo_path))


def custom_config_path(repo_path: str | Path) -> Path:
    return custom_dir(repo_path) / CUSTOM_FILE


def path_for(tier: Tier, repo_path: str | Path = "") -> Path:
    """The file that ``tier`` is kept in for ``repo_path``."""
    if tier is Tier.USER:
        return defaults_path()
    if tier is Tier.REPO:
        return repo_config_path(repo_path)
    return custom_config_path(repo_path)


def exists(tier: Tier, repo_path: str | Path = "") -> bool:
    if tier is not Tier.USER and not repo_path:
        return False
    return path_for(tier, repo_path).is_file()


def has_repo_config(repo_path: str | Path) -> bool:
    return exists(Tier.REPO, repo_path)


def effective_tier(repo_path: str | Path, chosen: str = "") -> Tier:
    """Which settings are in force: what was chosen, or what is there.

    Nobody having chosen, a repository with a file of its own uses it. That is
    what the merged design did for everyone who never thought about it, and it
    means this arrived without changing what any existing repository does.
    """
    picked = tier_of(chosen)
    if picked is not None:
        return picked
    return Tier.REPO if has_repo_config(repo_path) else Tier.USER


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


def _float(data: dict, key: str, fallback: float) -> float:
    value = data.get(key, fallback)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return float(value)


def _list(data: dict, key: str, fallback: list) -> list:
    value = data.get(key, fallback)
    if not isinstance(value, list):
        return list(fallback)
    return [str(item) for item in value if isinstance(item, str)]


def _overlay(settings: RepoSettings, data: dict) -> RepoSettings:
    """Apply the keys ``data`` sets, and leave the rest of ``settings`` alone."""
    branch = _section(data, "branch")
    fetch = _section(data, "fetch")
    audit = _section(data, "audit")
    stale = _section(audit, "stale")
    commit = _section(data, "commit")
    review = _section(data, "review")
    model = _section(data, "model")
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
        audit=AuditRules(
            narrate=_bool(audit, "narrate", settings.audit.narrate),
            fast=_bool(audit, "fast", settings.audit.fast),
            large_file_mb=_int(
                audit, "large_file_mb", settings.audit.large_file_mb
            ),
            history_limit=_int(
                audit, "history_limit", settings.audit.history_limit
            ),
            selected=_list(audit, "selected", settings.audit.selected),
            last=_str(audit, "last", settings.audit.last),
            stale=StaleRules(
                months=_int(stale, "months", settings.audit.stale.months),
                protect=_list(stale, "protect", settings.audit.stale.protect),
                merged_only=_bool(
                    stale, "merged_only", settings.audit.stale.merged_only
                ),
                keep_unpushed=_bool(
                    stale, "keep_unpushed", settings.audit.stale.keep_unpushed
                ),
            ),
        ),
        commit=CommitRules(
            diff_mode=_str(commit, "diff_mode", settings.commit.diff_mode),
            subject_target=_int(
                commit, "subject_target", settings.commit.subject_target
            ),
            subject_limit=_int(
                commit, "subject_limit", settings.commit.subject_limit
            ),
            body_limit=_int(commit, "body_limit", settings.commit.body_limit),
            history_limit=_int(
                commit, "history_limit", settings.commit.history_limit
            ),
            ignore_globs=_list(
                commit, "ignore_globs", settings.commit.ignore_globs
            ),
        ),
        review=ReviewRules(
            history_limit=_int(
                review, "history_limit", settings.review.history_limit
            ),
        ),
        model=ModelRules(
            context_window=_int(
                model, "context_window", settings.model.context_window
            ),
            safety_margin=_float(
                model, "safety_margin", settings.model.safety_margin
            ),
            parallel_calls=_int(
                model, "parallel_calls", settings.model.parallel_calls
            ),
        ),
        tier=settings.tier,
        sources=list(settings.sources),
        problem=settings.problem,
    )


# ---- resolving -----------------------------------------------------------------------
def resolve(repo_path: str | Path = "", chosen: str = "") -> RepoSettings:
    """What is configured for ``repo_path``: the tier in force, over the built-ins.

    One file, not a blend of three. ``chosen`` is the user's answer -- ``""``
    meaning they have not given one, which `effective_tier` reads as "whatever
    is there".

    The built-ins fill in what that file does not say, so a file trimmed to the
    one key somebody cared about still works. Nothing else is consulted: a
    value that is not in the file in force and not a constant here does not
    exist.

    Read every time, and deliberately not cached against the file's timestamp.
    Two edits of the same length inside one tick of the filesystem's clock are
    indistinguishable by ``(mtime, size)``, and the stale answer that comes back
    is a setting that will not take -- for the saving of one small file read.
    A caller doing this per keystroke should resolve once and hold the object.
    """
    tier = effective_tier(repo_path, chosen)
    settings = RepoSettings(tier=tier)
    path = path_for(tier, repo_path)

    data, problem = _read(path)
    if problem:
        settings.problem = problem
        return settings
    if data is None:
        # Chosen, and not there. Said out loud rather than quietly falling back
        # to another tier: "the settings you picked are missing" and "the
        # settings you picked say nothing" are different sentences.
        if tier is not Tier.USER:
            settings.problem = f"{path.name} is not there, so the built-in defaults apply."
        return settings

    settings = _overlay(settings, data)
    settings.tier = tier
    settings.sources.append(str(path))
    return settings


def for_repo(settings, repo_path: str | Path = "") -> RepoSettings:
    """The settings in force for a repository, given the user's choice.

    The one call every consumer wants: it knows where the choice is kept, so
    nothing else has to.
    """
    chosen = settings.settings_tier(str(repo_path)) if repo_path else ""
    return resolve(repo_path, chosen)


#: Repo-scoped values under the names every consumer already reads them by.
#: The left side is what a caller asks for; the right is where it now lives.
_BOUND: dict[str, tuple[str, str]] = {
    "diff_mode": ("commit", "diff_mode"),
    "ignore_globs": ("commit", "ignore_globs"),
    "commit_subject_target": ("commit", "subject_target"),
    "commit_subject_limit": ("commit", "subject_limit"),
    "commit_body_limit": ("commit", "body_limit"),
    "commit_history_limit": ("commit", "history_limit"),
    "review_history_limit": ("review", "history_limit"),
    "context_window": ("model", "context_window"),
    "safety_margin": ("model", "safety_margin"),
    "parallel_calls": ("model", "parallel_calls"),
    "agents_narrate": ("audit", "narrate"),
    "agent_fast_mode": ("audit", "fast"),
    "agent_large_file_mb": ("audit", "large_file_mb"),
    "agent_history_limit": ("audit", "history_limit"),
}


class Bound:
    """The user's settings, with one repository's answers in front of them.

    A run reads a mixture: which provider to call (the user's, one account) and
    what to send it (the repository's). Threading two objects through
    ``CommitGenerator``, ``estimate``, ``parallel`` and ``ModelRuntime`` would
    mean editing every one of them to say which half each value came from --
    and every one of them already reads ``settings.diff_mode``. So this is that
    object: the repo-scoped names answered from the tier in force, everything
    else the user's settings unchanged.

    Read-only, and that is the point rather than an omission. Setting a value
    here would change a copy and persist nothing; changing a repository's
    settings goes through ``change``, which knows which file it belongs in.
    """

    def __init__(self, settings, repo_path: str | Path = "", **overrides) -> None:
        rules = for_repo(settings, repo_path)
        object.__setattr__(self, "_settings", settings)
        object.__setattr__(self, "_rules", rules)
        object.__setattr__(self, "_overrides", dict(overrides))
        object.__setattr__(self, "repo", str(repo_path or ""))
        object.__setattr__(self, "tier", rules.tier)

    def __getattr__(self, name: str):
        # Only reached for names not set on the instance, which is every name:
        # the repo-scoped ones are answered here, the rest are the user's.
        if name in self._overrides:
            return self._overrides[name]
        where = _BOUND.get(name)
        if where is None:
            return getattr(self._settings, name)
        section, key = where
        return getattr(getattr(self._rules, section), key)

    def __setattr__(self, name: str, value) -> None:
        raise AttributeError(
            f"{name} cannot be set on the settings bound to a repository. "
            "Use repo_config.change, which knows which file it belongs in."
        )

    def __repr__(self) -> str:
        return f"<Bound {self.repo or 'no repository'} tier={self.tier}>"


def bind(settings, repo_path: str | Path = "", **overrides) -> Bound:
    """``settings`` as one repository sees them. See :class:`Bound`.

    ``overrides`` answer for this run only and are written nowhere -- for a
    caller that was told what to use, such as an MCP client passing
    ``mode="working"`` for one message. Anything else stays as configured.
    """
    return Bound(settings, repo_path, **overrides)


def defaults() -> RepoSettings:
    """The user tier: what a repository with nothing of its own is configured with."""
    return resolve("", Tier.USER)


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


def read_text(tier: Tier, repo_path: str | Path = "") -> str:
    """One tier's file exactly as it is on disk, for showing and editing.

    Not the resolved settings: what is edited has to be what was written, down
    to the key that was left out on purpose.
    """
    try:
        return path_for(tier, repo_path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def check(text: str) -> str:
    """Why ``text`` is not a settings file, or ``""`` if it is one."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"Not valid JSON: {exc}"
    return "" if isinstance(data, dict) else "A settings file holds an object, not a list."


def write_text(tier: Tier, repo_path: str | Path, text: str) -> str:
    """Save one tier's file. Returns a problem, or ``""``.

    Refuses to write what cannot be read back. A settings file is edited here
    and read by every run afterwards, and the run that finds it broken is a
    long way from the keystroke that broke it.
    """
    problem = check(text)
    if problem:
        return problem
    try:
        _write(path_for(tier, repo_path), json.loads(text))
    except OSError as exc:
        return str(exc)
    return ""


def text_from(data: dict) -> str:
    """``data`` as a settings file: the schema version, then the settings.

    Used by anything that builds settings rather than editing them -- a merge,
    for one -- so that what it writes is a file this module would have written.
    """
    body = {key: value for key, value in data.items() if key != "version"}
    return json.dumps(
        {"version": SCHEMA_VERSION, **body}, indent=2, ensure_ascii=False
    ) + "\n"


def source_dict(tier: Tier | None, repo_path: str | Path = "") -> dict:
    """One tier's file as a plain dict, for comparing. ``None`` is the built-ins.

    Anything unreadable comes back empty. A comparison against a file nobody
    can parse is a comparison against nothing, which is the truth about it.
    """
    if tier is None:
        return RepoSettings().to_dict()
    data, _problem = _read(path_for(tier, repo_path))
    return data or {}


def set_user_values(**sections: dict) -> None:
    """Change some of the User tier, leaving the rest of the file alone.

    Section by section rather than whole-file: the caller knows the two keys it
    is setting, and a caller that had to hand back the whole file to change two
    keys would erase whatever it did not know about -- including keys added by
    a later version, and keys the user set by hand.

    No repository is involved, so nothing forks to Custom. Writing here changes
    the answer for every repository that has not given one of its own.
    """
    data = source_dict(Tier.USER)
    for name, values in sections.items():
        section = data.setdefault(name, {})
        if isinstance(section, dict):
            section.update(values)
        else:  # a hand-edited file with a string where a section belongs
            data[name] = dict(values)
    write_text(Tier.USER, "", text_from(data))


def starter_text() -> str:
    """The user tier, written out as another tier's file would hold it.

    Every key, so the file can be read to find out what there is to set -- a
    file holding only the keys someone might want is one that answers nothing.

    Built from the user tier and deliberately not from what the repository
    currently resolves to. At creation there is no difference: a repository
    with no file of its own *is* the user tier. There is all the difference on
    a reset, where "what it resolves to" includes the file being reset, and
    faithfully reproduces whatever went wrong with it.
    """
    return json.dumps(defaults().to_dict(), indent=2, ensure_ascii=False) + "\n"


def create(tier: Tier, repo_path: str | Path) -> str:
    """Write a starter file for ``tier``. Returns a problem, or ``""``.

    Refuses rather than replacing: this is offered where there is no file, and
    an offer that overwrites one is a way to lose a config nobody meant to lose.
    """
    if exists(tier, repo_path):
        return f"There is already a {tier.label()} settings file."
    return write_text(tier, repo_path, starter_text())


def reset(tier: Tier, repo_path: str | Path) -> str:
    """Put one tier's file back to the user tier. A problem, or ``""``.

    Replaces, where `create` refuses -- which is the point of it. This is what
    is reached for when a file has been edited into something that does not
    work, and the answer to that cannot be "you already have one".
    """
    return write_text(tier, repo_path, starter_text())


def remove(tier: Tier, repo_path: str | Path) -> str:
    """Delete one tier's file. A problem, or ``""``.

    The user tier is refused: it is what everything falls back to, and there is
    a `restore_defaults` for putting it right.
    """
    if tier is Tier.USER:
        return "The User settings cannot be removed; restore them instead."
    try:
        path_for(tier, repo_path).unlink(missing_ok=True)
    except OSError as exc:
        return str(exc)
    return ""


def change(settings, repo_path: str, mutate, *, may_replace_custom=None) -> str:
    """Change one repository's settings from anywhere in the window.

    Every control that sets a repo-scoped value comes through here, and the
    rule is the one the settings editor follows: what is written is the tier in
    force *plus this change*, written to Custom -- so a tick box on the Audit
    tab cannot quietly edit the file a whole team shares, or the defaults every
    other repository is using.

    ``mutate`` is handed the file's contents as a plain dict and changes what it
    means to change. The file, not the resolved settings: a file that says only
    what somebody cared about keeps saying only that.

    ``may_replace_custom`` is asked before a fork would overwrite an existing
    Custom file, and is the only thing standing between a tick box and somebody
    else's saved settings. No answer means no.
    """
    tier = effective_tier(repo_path, settings.settings_tier(repo_path))
    data = source_dict(tier, repo_path)
    mutate(data)

    if tier is Tier.CUSTOM:
        return write_text(tier, repo_path, text_from(data))

    if exists(Tier.CUSTOM, repo_path):
        allowed = may_replace_custom(
            source_dict(Tier.CUSTOM, repo_path), data
        ) if may_replace_custom else False
        if not allowed:
            return "Your Custom settings for this repository were not replaced."

    problem = write_text(Tier.CUSTOM, repo_path, text_from(data))
    if problem:
        return problem
    settings.set_settings_tier(repo_path, Tier.CUSTOM.value)
    settings.save()
    return ""


def restore_defaults() -> str:
    """Put the defaults back to the values this build ships with.

    The last thing standing when the file has been edited into nonsense: every
    tier below it is a constant in this module, so this cannot fail for want of
    something to restore *from*.
    """
    return save_defaults(RepoSettings())


#: Legacy `Settings` fields carried into the user tier the first time this
#: build runs, and the section they became. Kept here rather than in config.py
#: because this is the module that knows what they turned into.
_MIGRATED = {
    "agents_narrate": ("audit", "narrate"),
    "agent_fast_mode": ("audit", "fast"),
    "agent_large_file_mb": ("audit", "large_file_mb"),
    "agent_history_limit": ("audit", "history_limit"),
    "agent_selected_ids": ("audit", "selected"),
    "agent_last_id": ("audit", "last"),
    "diff_mode": ("commit", "diff_mode"),
    "commit_subject_target": ("commit", "subject_target"),
    "commit_subject_limit": ("commit", "subject_limit"),
    "commit_body_limit": ("commit", "body_limit"),
    "commit_history_limit": ("commit", "history_limit"),
    "ignore_globs": ("commit", "ignore_globs"),
    "review_history_limit": ("review", "history_limit"),
    "context_window": ("model", "context_window"),
    "safety_margin": ("model", "safety_margin"),
    "parallel_calls": ("model", "parallel_calls"),
}


def migrate_user_settings(settings) -> bool:
    """Carry the old per-repository settings into the user tier. Once.

    An upgrade must not silently reset what somebody configured. The values
    were in ``settings.json`` and are now the user tier, so they are copied
    across the first time this build sees a settings file that still has them
    -- and the old fields are then left alone, because a build that removes
    them is a build the previous one cannot read a downgrade from.

    Returns True when anything was carried over.
    """
    if getattr(settings, "settings_migrated", False):
        return False
    data = source_dict(Tier.USER)
    for field_name, (section, key) in _MIGRATED.items():
        if not hasattr(settings, field_name):
            continue
        data.setdefault(section, {})[key] = getattr(settings, field_name)
    stale = getattr(settings, "stale_branch_rules", None)
    if isinstance(stale, dict) and stale:
        data.setdefault("audit", {})["stale"] = dict(stale)

    if save_text(Tier.USER, "", text_from(data)):
        return False  # a disk that refused; try again next time
    settings.settings_migrated = True
    settings.save()
    return True


def save_text(tier: Tier, repo_path: str, text: str) -> str:
    """`write_text` under the name the migration reads better with."""
    return write_text(tier, repo_path, text)


def ensure_defaults() -> bool:
    """Put the built-in defaults on disk if nothing is there. True if written.

    So that the file exists to be found and read before anyone has changed
    anything in it: a settings file that appears only once it has been edited
    cannot be discovered by looking.
    """
    if defaults_path().exists():
        return False
    return save_defaults(RepoSettings()) == ""
