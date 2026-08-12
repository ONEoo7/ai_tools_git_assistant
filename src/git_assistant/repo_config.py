"""Settings that belong to a repository rather than to the person using it.

Three of them, and exactly one is in force:

    user     <config dir>/user_settings.json                  every repository
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

from git_assistant import jsonc
from git_assistant.config import APP_NAME, DEFAULT_IGNORE_GLOBS, repo_key
from git_assistant.prompts import (
    DEFAULT_TEMPLATE,
    SHORT_TEMPLATE,
    SHORT_TEMPLATE_NAME,
)

SCHEMA_VERSION = 1

#: Where a repository keeps its own answer. A directory rather than a bare
#: file: it is where anything else this application ever puts *in* a repository
#: belongs, and one dotted name in a project root is easier to explain than
#: three.
REPO_DIR = ".git_assistant"
REPO_FILE = "repo_settings.json"

#: The user's answer for repositories that do not have one.
DEFAULTS_FILE = "user_settings.json"
#: What it was called before the three files were named for what they are.
LEGACY_DEFAULTS_FILE = "default_repo_settings.json"

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


# ---- what the file says about itself -------------------------------------------------
#: The first line of each file: which of the three it is, and what that means
#: for it. One sentence, because it is answering the question somebody has when
#: they open a file they did not expect to find.
HEADERS = {
    Tier.USER: "These settings are overridden by repo_settings.json",
    Tier.REPO: (
        "These settings override user_settings.json for this repository.\n"
        "This file is in the repository, so it is shared with everyone who "
        "clones it."
    ),
    Tier.CUSTOM: (
        "These settings override user_settings.json and repo_settings.json for "
        "one repository.\nYours alone: this file is not in the repository and "
        "is not shared."
    ),
}

#: One sentence per key, above it in the file. Keyed by dotted path, so a
#: nested key is named the way it is spoken about.
#:
#: Here rather than beside each dataclass field, because this is the text a
#: person reads in the file and the docstrings are the text a person reads in
#: the code -- and the two want different words. A key without a line here is
#: written without a comment, which tests/test_settings_split.py refuses.
FIELD_COMMENTS = {
    "version": "Schema version of this file. Written by the application.",
    "branch": "Where a new branch's name comes from.",
    "branch.pattern": (
        "This repository's own naming convention. {user} and {name} are "
        "filled in; anything else is left as typed."
    ),
    "branch.patterns": (
        "The conventions offered in the Branches tab. This repository's own, "
        "above, is offered first."
    ),
    "branch.user": (
        "Who {user} is. Blank asks git for the committer name in this "
        "repository."
    ),
    "branch.push_sets_upstream": (
        "Set the upstream when a branch is pushed for the first time."
    ),
    "fetch": "How much a fetch brings back.",
    "fetch.shallow": "Fetch recent history only, instead of all of it.",
    "fetch.depth": (
        "Commits per ref a shallow fetch asks for. Kept when shallow is off, "
        "so turning it back on restores this number."
    ),
    "fetch.prune": "Delete local copies of branches that are gone from the remote.",
    "fetch.tags": "Bring tags along with the fetch.",
    "audit": "What the repository audits do, and how much of them is kept.",
    "audit.narrate": (
        "Let the model write the report's prose. Off, the report is the "
        "measurements alone and no request is made."
    ),
    "audit.fast": (
        "Skip the per-file history breakdown, which is the slow part on a "
        "large repository."
    ),
    "audit.large_file_mb": "A file at least this many megabytes is worth flagging.",
    "audit.history_limit": "Audit runs kept per repository. 0 keeps every one.",
    "audit.stale": "When a branch counts as stale, and when deleting one may be proposed.",
    "audit.stale.months": "A branch untouched for this many months counts as stale.",
    "audit.stale.protect": (
        "Branch name patterns that are never proposed for deletion. "
        "* matches anything."
    ),
    "audit.stale.merged_only": (
        "Only propose deleting a branch whose commits are already on another one."
    ),
    "audit.stale.keep_unpushed": "Never propose deleting a branch that was never pushed.",
    "commit": "What a generated commit message is made from, and how long it may be.",
    "commit.diff_mode": (
        'Which diff is described: "cached" for what is staged, "working" for '
        "everything uncommitted."
    ),
    "commit.subject_target": (
        "Soft target for the first line. Exceeding it is reported, not "
        "refused. 0 says nothing about it."
    ),
    "commit.subject_limit": (
        "Hard cap for the first line -- past it, tools that show a subject cut "
        "it without saying so. 0 for no cap."
    ),
    "commit.body_limit": "Total length of the body. 0 for no cap.",
    "commit.history_limit": (
        "Generated messages kept per repository. 0 keeps every one."
    ),
    "commit.ignore_globs": (
        "Files matching any of these never reach the model. Lock files and "
        "minified bundles are noise that costs tokens."
    ),
    "commit.include_lines": (
        "How much of an ignored file is sent once it has been un-ignored by "
        "hand, in the staged files list. 0 for no cap. Nothing is un-ignored "
        "on its own -- the globs above are always obeyed."
    ),
    "review": "Code review.",
    "review.history_limit": "Reviews kept per repository. 0 keeps every one.",
    "review.judge": (
        "The model that scores a review, which is not the one that wrote it. "
        "It is shown the exact prompt the reviewer was sent and the exact reply "
        "it gave, and scores that reply out of ten. The scores accumulate in "
        "code_review/leaderboard.json."
    ),
    "review.judge.enabled": (
        "Whether a review is scored. Off by default: a judge doubles the calls "
        "a review makes."
    ),
    "review.judge.provider": (
        "Which backend judges, by provider key. Empty means none is chosen, "
        "and nothing is scored however the tick box is set."
    ),
    "review.judge.model": (
        "The judging model. Its own, not the provider's active one: the judge "
        "is often the same provider as the reviewer with a stronger model."
    ),
    "review.judge.temperature": "How adventurous the judge is. 0 for repeatable scores.",
    "model": "How much of the model one run of this repository may use.",
    "model.context_window": (
        "Total tokens per request, input and output together. 0 asks the "
        "provider. Set it to match how the model is actually loaded."
    ),
    "model.safety_margin": (
        "Fraction of the window reserved for the model's answer. The rest is "
        "available for the diff."
    ),
    "model.parallel_calls": (
        "How many requests may be in flight at once. The provider's own limit "
        "still applies."
    ),
    "model.endpoints": (
        "Where each backend is, by provider key. Only the ones whose address "
        "is yours to give -- a hosted provider has one and it is not a "
        "setting.\nAPI keys are NOT here and must never be: they are in the "
        "Windows Credential Manager."
    ),
    "prompt": "What the model is asked for a commit message.",
    "prompt.templates": (
        'Named commit-message prompts, as [{"name": ..., "text": ...}].\n'
        "A repository that ships any of these replaces the ones in "
        "user_settings.json.\n"
        "The default template is never replaced: it is in "
        "static_user_settings.json and is always offered.\n"
        "Which one a repository uses is chosen in the window and kept with "
        "your own settings, not here."
    ),
    "review.profiles": (
        "Named review profiles: which rules apply to which language at which "
        "version. The rules themselves are files under code_review/."
    ),
    "tracing": (
        "Where a run's trace goes. Neither Langfuse key is here and neither "
        "may ever be -- both are in the Windows Credential Manager."
    ),
    "tracing.enabled": "Send traces at all.",
    "tracing.host": "The Langfuse instance to send them to.",
    "tracing.environment": "The environment name traces are filed under.",
    "tracing.release": (
        "The release traces are filed under. Blank means this build's version."
    ),
    "tracing.send_prompts": (
        "Whether the prompt and the reply travel with the trace. Off, a trace "
        "still carries the model, the timings, the tokens and any error."
    ),
}

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


def _component(part: str) -> str:
    """One slash-separated piece of a ref, as git will accept it.

    Two of git's rules are about the piece rather than the whole name, and
    ``slug`` cannot see them because it is handed the whole thing: a piece may
    not begin with a dot, and may not end with ``.lock``. Both come up in
    practice -- ``.hidden`` and ``index.lock`` are things people type -- and
    both are a refusal from ``git branch`` several seconds after the name was
    offered on screen as the one that would be created.

    A trailing dot goes too. Git only refuses one at the very end of the name,
    but ``release.`` is a typo wherever it appears.
    """
    part = part.lstrip(".")
    while part.endswith(".lock"):
        part = part[: -len(".lock")]
    return part.strip("-.")


#: Where LM Studio listens when nobody has said otherwise. Written out here
#: rather than imported from `providers` because this module depends on nothing
#: else in the application -- and asserted equal to the provider table's own
#: default by a test.
LMSTUDIO_ENDPOINT = "http://127.0.0.1:1234"

#: The pattern that adds nothing: the name is what was typed and no more.
PLAIN = "{name}"

#: Offered for a new branch when nobody has said otherwise. A convention, not a
#: rule -- the window still offers the plain name first, and this is the list
#: beside it.
DEFAULT_PATTERNS = ["dev/rem/{user}/{name}", "test/rem/{user}/{name}"]


@dataclass
class BranchRules:
    """Where a new branch's name comes from.

    A pattern carries ``{user}`` and ``{name}``. Anything else is left where it
    is rather than blanked, so a mistyped placeholder shows up in the name the
    window offers instead of quietly disappearing from it.

    ``patterns`` are the ones offered; ``pattern`` is this repository's own, and
    is offered first when it says anything. Two fields rather than one because
    they answer different questions: a project can add a convention to the list
    without deciding that the list is now only that.
    """

    pattern: str = PLAIN
    patterns: list[str] = field(default_factory=lambda: list(DEFAULT_PATTERNS))
    #: Who ``{user}`` is. Blank means "ask git", which the caller does -- this
    #: file does not run git.
    user: str = ""
    push_sets_upstream: bool = True

    def offered(self) -> list[str]:
        """The patterns to choose from, this repository's own first.

        The plain name is not one of them. It is not a pattern -- it is the
        absence of one, and the window offers it as its own thing.
        """
        out: list[str] = []
        for candidate in [self.pattern, *self.patterns]:
            if candidate and candidate != PLAIN and candidate not in out:
                out.append(candidate)
        return out

    def user_for(self, typed: str = "", git: str = "") -> str:
        """Who ``{user}`` is, in the order the answers beat each other.

        Typed into the window now, else configured for this repository, else
        whatever git says the committer is called here. Separate from
        ``render`` so that the precedence is one readable line rather than an
        argument order nobody can remember at the call site.
        """
        return typed or self.user or git

    def render(self, name: str, *, pattern: str = "", user: str = "") -> str:
        """The full branch name for ``name``. Empty when nothing is left of it.

        ``pattern`` is what the window has selected; blank falls back to this
        repository's own. ``user`` is already decided -- see ``user_for``.
        """
        chosen = pattern or self.pattern
        typed = slug(name)
        # A pattern is a prefix and a name. Nothing surviving of what was typed
        # -- "///", or a word made only of punctuation -- leaves the prefix,
        # and creating `dev/rem/<who>` as a branch is not what was asked for.
        if "{name}" in chosen and not typed:
            return ""
        rendered = chosen.replace("{user}", slug(user or self.user)).replace(
            "{name}", typed
        )
        # A pattern whose {user} resolved to nothing leaves "dev//thing", which
        # git refuses. Dropping the empty piece is what was meant.
        parts = [piece for piece in map(_component, rendered.split("/")) if piece]
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
    stale: StaleRules = field(default_factory=StaleRules)

    # Which audits are ticked, and whose report is on screen, are deliberately
    # not here. They alter nothing about what an audit does -- they are what is
    # selected in the window -- so they belong to the person looking at it and
    # not to the repository. See config.Settings.audits_selected. Keeping them
    # here also meant ticking a box forked the settings to Custom, which is a
    # file nobody meant to make.


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
    #: How much of an ignored file reaches the model once somebody has asked
    #: for it by hand. 0 for no cap. A document's opening pages say what it is,
    #: which is what a commit message needs; the other thousand lines are what
    #: got it ignored in the first place.
    include_lines: int = 200


@dataclass
class PromptRules:
    """The named commit-message prompts to pick from.

    Here rather than in the user's own settings because a template decides what
    is sent: a project whose commits follow a house style is a project that can
    ship the prompt producing it.

    The *default* template is deliberately not here. It lives in
    `config.Settings.default_template`, is always offered, and is the thing a
    project's own templates cannot take away -- see `offered_templates`. Which
    of the named ones a repository uses is a selection and stays with the user
    too; see `config.Settings.repo_templates`.
    """

    #: ``[{"name": ..., "text": ...}]``. Plain dicts rather than a dataclass:
    #: this module owns no shape it does not have to, and `config.Template` is
    #: what the window builds from these.
    #:
    #: Shipped with one entry, so the file arrives saying what an entry looks
    #: like rather than showing an empty list and leaving the shape to be
    #: guessed.
    templates: list = field(
        default_factory=lambda: [
            {"name": SHORT_TEMPLATE_NAME, "text": SHORT_TEMPLATE}
        ]
    )


@dataclass
class TracingRules:
    """Where a run's trace goes. Neither key is here and neither may ever be.

    Both are in the Windows Credential Manager; see git_assistant.tracing.
    """

    enabled: bool = False
    host: str = ""
    environment: str = "development"
    release: str = ""  # blank means this build's version
    send_prompts: bool = True


@dataclass
class JudgeRules:
    """The model that scores a review, which is not the one that wrote it.

    A second, stronger model is asked to mark the reviewer's homework: it is
    shown the exact prompt the reviewer was sent and the exact reply it gave,
    and scores that reply out of ten. Repeated across runs it answers the
    question a local model cannot answer about itself -- is it any good at
    this. See git_assistant.review.judge.

    Its own model and temperature, deliberately, rather than the provider's
    active ones: judge and reviewer are frequently the *same* provider with
    different models, and sharing the fields would mean choosing a judge
    silently changed what does the reviewing.
    """

    #: Off until asked for. A judge doubles the calls a review makes, so it is
    #: not something to discover on a bill.
    enabled: bool = False
    #: Provider key, from git_assistant.providers. "" means none chosen, which
    #: reads as "no judge configured" however the tick box is set.
    provider: str = ""
    model: str = ""
    temperature: float = 0.0


@dataclass
class ReviewRules:
    """What is kept from reviewing this repository."""

    history_limit: int = 20
    #: Named review profiles: which rules apply to which language at which
    #: version. The rules themselves are files under code_review/; see
    #: git_assistant.review.rule_files.
    profiles: list = field(default_factory=list)
    judge: JudgeRules = field(default_factory=JudgeRules)


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
    #: Where each backend is, by provider key. Only the ones whose address is
    #: yours to give -- the hosted providers have one and it is not a setting.
    #: API keys are NOT here and must never be: see git_assistant.credentials.
    endpoints: dict = field(default_factory=dict)


@dataclass
class RepoSettings:
    """What is configured for one repository, and where that came from."""

    branch: BranchRules = field(default_factory=BranchRules)
    fetch: FetchRules = field(default_factory=FetchRules)
    audit: AuditRules = field(default_factory=AuditRules)
    commit: CommitRules = field(default_factory=CommitRules)
    prompt: PromptRules = field(default_factory=PromptRules)
    review: ReviewRules = field(default_factory=ReviewRules)
    model: ModelRules = field(default_factory=ModelRules)
    tracing: TracingRules = field(default_factory=TracingRules)
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
                "patterns": list(self.branch.patterns),
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
                "include_lines": self.commit.include_lines,
            },
            "prompt": {
                "templates": [dict(one) for one in self.prompt.templates],
            },
            "review": {
                "history_limit": self.review.history_limit,
                "profiles": [dict(one) for one in self.review.profiles],
                "judge": {
                    "enabled": self.review.judge.enabled,
                    "provider": self.review.judge.provider,
                    "model": self.review.judge.model,
                    "temperature": self.review.judge.temperature,
                },
            },
            "model": {
                "context_window": self.model.context_window,
                "safety_margin": self.model.safety_margin,
                "parallel_calls": self.model.parallel_calls,
                "endpoints": dict(self.model.endpoints),
            },
            "tracing": {
                "enabled": self.tracing.enabled,
                "host": self.tracing.host,
                "environment": self.tracing.environment,
                "release": self.tracing.release,
                "send_prompts": self.tracing.send_prompts,
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
        data = jsonc.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"{path.name} is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return None, f"{path.name} does not hold a settings object."
    return data, ""


def _strings(data: dict, key: str, fallback: dict) -> dict:
    """A map of string to string. Anything else in it is left out."""
    raw = data.get(key)
    if not isinstance(raw, dict):
        return dict(fallback)
    return {
        str(name): str(value)
        for name, value in raw.items()
        if isinstance(value, str)
    }


def _dicts(data: dict, key: str, fallback: list) -> list:
    """A list of objects. Anything in it that is not one is left out."""
    raw = data.get(key)
    if not isinstance(raw, list):
        return [dict(one) for one in fallback]
    return [dict(one) for one in raw if isinstance(one, dict)]


def _named(data: dict, key: str, required: tuple, fallback: list) -> list:
    """A list of objects that each have every one of ``required``."""
    return [
        one
        for one in _dicts(data, key, fallback)
        if all(isinstance(one.get(name), str) for name in required)
    ]


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
    prompt = _section(data, "prompt")
    review = _section(data, "review")
    judge = _section(review, "judge")
    model = _section(data, "model")
    tracing = _section(data, "tracing")
    return RepoSettings(
        branch=BranchRules(
            pattern=_str(branch, "pattern", settings.branch.pattern),
            patterns=_list(branch, "patterns", settings.branch.patterns),
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
            include_lines=_int(
                commit, "include_lines", settings.commit.include_lines
            ),
        ),
        prompt=PromptRules(
            templates=_named(
                prompt, "templates", ("name", "text"), settings.prompt.templates
            ),
        ),
        review=ReviewRules(
            history_limit=_int(
                review, "history_limit", settings.review.history_limit
            ),
            profiles=_dicts(review, "profiles", settings.review.profiles),
            judge=JudgeRules(
                enabled=_bool(judge, "enabled", settings.review.judge.enabled),
                provider=_str(judge, "provider", settings.review.judge.provider),
                model=_str(judge, "model", settings.review.judge.model),
                temperature=_float(
                    judge, "temperature", settings.review.judge.temperature
                ),
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
            endpoints=_strings(model, "endpoints", settings.model.endpoints),
        ),
        tracing=TracingRules(
            enabled=_bool(tracing, "enabled", settings.tracing.enabled),
            host=_str(tracing, "host", settings.tracing.host),
            environment=_str(
                tracing, "environment", settings.tracing.environment
            ),
            release=_str(tracing, "release", settings.tracing.release),
            send_prompts=_bool(
                tracing, "send_prompts", settings.tracing.send_prompts
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
    "include_lines": ("commit", "include_lines"),
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
    "langfuse_enabled": ("tracing", "enabled"),
    "langfuse_host": ("tracing", "host"),
    "langfuse_environment": ("tracing", "environment"),
    "langfuse_release": ("tracing", "release"),
    "langfuse_send_prompts": ("tracing", "send_prompts"),
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
        """Repo-scoped names refuse; everything else goes to the user's settings.

        The refusal is the point: setting ``diff_mode`` here would change a copy
        and persist nothing, and the caller would have no way to tell. But
        ``active_repo`` is not one of those -- it is an ordinary user setting,
        it is the same object underneath, and refusing it would mean holding
        the unbound settings alongside the bound ones just to change which
        repository is active.
        """
        if name in _BOUND or name in ("repo", "tier"):
            raise AttributeError(
                f"{name} cannot be set on the settings bound to a repository. "
                "Use repo_config.change, which knows which file it belongs in."
            )
        setattr(self._settings, name, value)

    # ---- the ones that are not a plain lookup -------------------------------
    # Methods rather than entries in _BOUND because they take an argument or
    # build something. They were methods on `Settings` and every caller still
    # calls them by the same name, which is the whole point of this class.
    @property
    def templates(self) -> list:
        """The named templates on offer here, as the window's own objects.

        The repository's own if it ships any, else yours. The default is not
        among them; `template_names` puts it first.
        """
        from git_assistant.config import Template

        return [
            Template(name=one.get("name", ""), text=one.get("text", ""))
            for one in offered_templates(self.repo)
            if one.get("name")
        ]

    @property
    def base_url(self) -> str:
        """LM Studio's address. Other providers go through `provider_endpoint`."""
        return self.provider_endpoint("lmstudio") or LMSTUDIO_ENDPOINT

    def provider_endpoint(self, key: str) -> str:
        """Where this backend is, or ``""`` to use the provider's own default."""
        return (self._rules.model.endpoints.get(key) or "").strip()

    def template_names(self) -> list[str]:
        """Every selectable template, the default first."""
        from git_assistant.config import DEFAULT_TEMPLATE_NAME

        return [DEFAULT_TEMPLATE_NAME, *(one.name for one in self.templates)]

    def template_text(self, name: str) -> str:
        """Body of a named template, falling back to the default."""
        from git_assistant.config import DEFAULT_TEMPLATE_NAME
        from git_assistant.prompts import (
    DEFAULT_TEMPLATE,
    SHORT_TEMPLATE,
    SHORT_TEMPLATE_NAME,
)

        if name and name != DEFAULT_TEMPLATE_NAME:
            for one in self.templates:
                if one.name == name:
                    return one.text
        return getattr(self._settings, "default_template", "") or DEFAULT_TEMPLATE

    def template_for_repo(self, repo_path: str) -> str:
        """The template a repository should be described with."""
        return self.template_text(self._settings.repo_template(repo_path))

    def review_profiles_built(self) -> list:
        """The profiles as `review.profiles.Profile` objects, bad ones dropped."""
        from git_assistant.review import profiles

        built = [profiles.Profile.from_dict(one) for one in self._rules.review.profiles]
        return [one for one in built if one is not None]

    def review_profile(self, name: str):
        """A profile by name, or ``None``."""
        from git_assistant.review import profiles

        for one in self._rules.review.profiles:
            built = profiles.Profile.from_dict(one)
            if built is not None and built.name == name:
                return built
        return None

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
def _write(path: Path, text: str) -> None:
    """Replaced, never truncated: a torn write must not eat the settings."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_defaults(settings: RepoSettings) -> str:
    """Write the user's answer for every repository. Returns a problem, or ``""``."""
    try:
        _write(defaults_path(), text_from(settings.to_dict(), Tier.USER))
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
        data = jsonc.loads(text)
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
        # Written exactly as given. These files carry comments, and a save that
        # re-rendered the data would throw away every comment the user had
        # added to explain their own settings -- which is the one thing a
        # hand-editable file must not do to a hand edit.
        _write(path_for(tier, repo_path), text)
    except OSError as exc:
        return str(exc)
    return ""


def text_from(data: dict, tier: Tier = Tier.USER) -> str:
    """``data`` as a settings file: the schema version, then the settings.

    Used by anything that builds settings rather than editing them -- a merge,
    for one -- so that what it writes is a file this module would have written,
    comments and all.
    """
    body = {key: value for key, value in data.items() if key != "version"}
    return jsonc.dumps(
        {"version": SCHEMA_VERSION, **body},
        FIELD_COMMENTS,
        HEADERS.get(tier, ""),
    )


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


# ---- the libraries, which the window edits ------------------------------------------
# Read from the user tier and written back to it. The window that edits these --
# the Template tab, the review profiles list -- is not about a repository, so it
# edits the answer every repository without one of its own gets. A repository
# that ships its own keeps it, which is the point of it being a setting.
def offered_templates(repo_path: str | Path = "") -> list:
    """The named templates on offer for ``repo_path``, as plain dicts.

    A repository that ships templates **replaces** yours; one that does not
    leaves them alone. The default is not in this list -- it is always offered
    and is added by the caller, which is what "a project cannot take it away"
    means in practice.

    Deliberately not the tier in force. A project that checked a prompt in did
    so to be used, and choosing "User settings" to change a fetch depth is not
    a decision about that. So this reads the repository's own file directly
    and falls back to whichever of yours applies.
    """
    theirs = _templates_in(source_dict(Tier.REPO, repo_path))
    return theirs if theirs else user_templates()


def _templates_in(data: dict) -> list:
    """The `prompt.templates` of one file, with the unusable entries dropped."""
    section = data.get("prompt")
    section = section if isinstance(section, dict) else {}
    return _named(section, "templates", ("name", "text"), [])


def user_templates() -> list:
    """The named prompt templates in the user tier, as plain dicts.

    What the Template tab edits: yours, whatever any repository ships.
    """
    return [dict(one) for one in defaults().prompt.templates]


def save_user_templates(templates) -> None:
    """Replace them. ``templates`` may hold dicts or anything with name/text."""
    set_user_values(prompt={"templates": [_named_dict(one) for one in templates]})


def user_profiles() -> list:
    """The review profiles in the user tier, as plain dicts."""
    return [dict(one) for one in defaults().review.profiles]


def save_user_profiles(profiles) -> None:
    set_user_values(review={"profiles": [_profile_dict(one) for one in profiles]})


def _named_dict(one) -> dict:
    if isinstance(one, dict):
        return {"name": str(one.get("name", "")), "text": str(one.get("text", ""))}
    return {"name": str(getattr(one, "name", "")), "text": str(getattr(one, "text", ""))}


def _profile_dict(one) -> dict:
    return dict(one) if isinstance(one, dict) else one.to_dict()


def starter_text(tier: Tier = Tier.USER) -> str:
    """The user tier, written out as another tier's file would hold it.

    Every key, so the file can be read to find out what there is to set -- a
    file holding only the keys someone might want is one that answers nothing.

    Built from the user tier and deliberately not from what the repository
    currently resolves to. At creation there is no difference: a repository
    with no file of its own *is* the user tier. There is all the difference on
    a reset, where "what it resolves to" includes the file being reset, and
    faithfully reproduces whatever went wrong with it.
    """
    return text_from(defaults().to_dict(), tier)


def create(tier: Tier, repo_path: str | Path) -> str:
    """Write a starter file for ``tier``. Returns a problem, or ``""``.

    Refuses rather than replacing: this is offered where there is no file, and
    an offer that overwrites one is a way to lose a config nobody meant to lose.
    """
    if exists(tier, repo_path):
        return f"There is already a {tier.label()} settings file."
    return write_text(tier, repo_path, starter_text(tier))


def reset(tier: Tier, repo_path: str | Path) -> str:
    """Put one tier's file back to the user tier. A problem, or ``""``.

    Replaces, where `create` refuses -- which is the point of it. This is what
    is reached for when a file has been edited into something that does not
    work, and the answer to that cannot be "you already have one".
    """
    return write_text(tier, repo_path, starter_text(tier))


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
        return write_text(tier, repo_path, text_from(data, tier))

    if exists(Tier.CUSTOM, repo_path):
        allowed = may_replace_custom(
            source_dict(Tier.CUSTOM, repo_path), data
        ) if may_replace_custom else False
        if not allowed:
            return "Your Custom settings for this repository were not replaced."

    problem = write_text(Tier.CUSTOM, repo_path, text_from(data, Tier.CUSTOM))
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
    "review_profiles": ("review", "profiles"),
    "templates": ("prompt", "templates"),
    "langfuse_enabled": ("tracing", "enabled"),
    "langfuse_host": ("tracing", "host"),
    "langfuse_environment": ("tracing", "environment"),
    "langfuse_release": ("tracing", "release"),
    "langfuse_send_prompts": ("tracing", "send_prompts"),
}


def rename_legacy_user_file() -> bool:
    """Move ``default_repo_settings.json`` to ``user_settings.json``. True if moved.

    A rename and not a copy: two files, one of them stale and both looking
    authoritative, is the problem this whole split exists to end.
    """
    new, old = path_for(Tier.USER), _config_root() / LEGACY_DEFAULTS_FILE
    if new.exists() or not old.is_file():
        return False
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old, new)
    except OSError:
        return False  # a disk that refused; try again next run
    return True


def user_tier_from(data: dict) -> dict:
    """The per-repository half of an old ``settings.json``, as a user-tier file.

    Built from the raw dict rather than from a loaded ``Settings``, because the
    fields it reads no longer exist on that class -- which is the point of the
    split, and would otherwise make the migration unable to find what it is
    migrating.
    """
    out: dict = {}
    for field_name, (section, key) in _MIGRATED.items():
        if field_name in data:
            out.setdefault(section, {})[key] = data[field_name]
    stale = data.get("stale_branch_rules")
    if isinstance(stale, dict) and stale:
        out.setdefault("audit", {})["stale"] = dict(stale)

    # Where each backend is. Three shapes became one: a map of the providers
    # whose address the user supplied, plus LM Studio's own IP and port, which
    # asked the same question in its own way for no reason anybody could name.
    endpoints = data.get("provider_endpoints")
    endpoints = dict(endpoints) if isinstance(endpoints, dict) else {}
    ip = str(data.get("lmstudio_ip") or "").strip()
    port = data.get("lmstudio_port")
    if ip and isinstance(port, int):
        endpoints["lmstudio"] = f"http://{ip}:{port}"
    if endpoints:
        out.setdefault("model", {})["endpoints"] = endpoints
    return out


def migrate_user_settings(data: dict) -> bool:
    """Write the user tier from an old ``settings.json``. True if written.

    An upgrade must not silently reset what somebody configured, and the values
    it configured were in that file. Never over an existing user tier: the file
    being read here is the older answer by definition.
    """
    if exists(Tier.USER):
        return False
    carried = user_tier_from(data)
    if not carried:
        return False
    return not save_text(Tier.USER, "", text_from(carried))


#: The keys the old single file used for what is now a selection, and the maps
#: they become. The value they carried was one answer for every repository, so
#: it lands under "" -- the answer for a repository nobody has chosen for.
_LIFTED = {"agent_selected_ids": "audit_selected", "agent_last_id": "audit_last"}


def migrate_files() -> bool:
    """Turn whatever this machine has into the three files this build reads.

    Three shapes arrive here:

      * nothing -- a first run, and there is nothing to migrate;
      * ``settings.json`` alone, from a build before the settings were split by
        who they belong to. Its per-repository half becomes the user tier and
        the rest becomes ``static_user_settings.json``;
      * ``settings.json`` *and* ``default_repo_settings.json``, from a build
        that had split them but had not yet named them for what they hold.

    Renames rather than copies throughout. Two files, one of them stale and
    both looking authoritative, is the problem this split exists to end.

    Returns True when anything moved. Never raises: a disk that refuses costs
    the migration, not the launch, and the next run tries again.
    """
    from git_assistant import config as user_config

    moved = rename_legacy_user_file()
    if user_config.config_path().exists():
        return moved

    old = user_config.old_config_path()
    if not old.is_file():
        return moved
    try:
        data = jsonc.loads(old.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return moved
    if not isinstance(data, dict):
        return moved

    migrate_user_settings(data)
    settings = user_config.Settings.from_dict(_lift_selections(data))
    settings.save()
    try:
        old.unlink()
    except OSError:
        pass  # the new file is written; a leftover is untidy, not wrong
    return True


def _lift_selections(data: dict) -> dict:
    """One global selection becomes the fallback entry of a per-repository map."""
    out = dict(data)
    for old_key, new_key in _LIFTED.items():
        if old_key in data and data[old_key]:
            out[new_key] = {"": data[old_key]}
    return out


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
