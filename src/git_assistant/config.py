"""What belongs to the person rather than to any repository.

Written to ``static_user_settings.json``. The two files beside it --
``user_settings.json`` and a repository's own ``repo_settings.json`` -- carry
the same keys as each other and none of the keys in this one; see
git_assistant.repo_config.

One question decides which file a new value goes in:

    Does it change what a run does?

Yes -- the diff mode, a branch pattern, when a branch counts as stale -- and it
is a *setting*. It goes in the shared schema, so a project can ship its answer
and a person can override it for themselves.

No -- which audits are ticked, which report is on screen, which repository is
active -- and it is a *selection*. It changes what is on screen and nothing
else, so it belongs here. Alongside it live the things that were never about a
repository at all: the account to call, the libraries to pick from, and which
repositories this person has.

Getting it wrong is not a crash, which is why tests/test_settings_split.py
enforces it. A setting kept here cannot be shared with a team; a selection kept
per repository forks the settings to Custom the first time a box is ticked.

JSON (not TOML) is used for on-disk storage so the editable multi-line prompt
template round-trips losslessly without a third-party TOML writer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant import jsonc
from git_assistant.providers import DEFAULT_PROVIDER, is_known
from git_assistant.prompts import DEFAULT_TEMPLATE

# Constants only, and no imports of its own, so this cannot become a cycle.
from git_assistant.review.prompts import JUDGE_TEMPLATE

APP_NAME = "git-assistant"

#: What every model is asked for until someone says otherwise. Low, because a
#: commit message is a description of a diff and not a creative act.
DEFAULT_TEMPERATURE = 0.2
#: What providers accept. Above 2.0 is an error from most of them, and a
#: rejected completion is a lost commit message.
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0


def _clamp_temperature(value: float) -> float:
    return max(MIN_TEMPERATURE, min(MAX_TEMPERATURE, value))
LEGACY_APP_NAME = "git-commit-assistant"  # pre-rename config location

# Files matching these globs are dropped from the diff before token counting.
# They are high-noise and low-signal for commit messages.
DEFAULT_IGNORE_GLOBS: list[str] = [
    "*.lock",
    "uv.lock",
    "poetry.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "composer.lock",
    "Gemfile.lock",
    "*.min.js",
    "*.min.css",
    "*.map",
    "*.svg",
    "*.png",
    "*.jpg",
    "*.jpeg",
    "*.gif",
    "*.ico",
    "*.pdf",
    "*.woff",
    "*.woff2",
    "*.ttf",
]


def config_path() -> Path:
    """The user's own settings: everything that is not about a repository.

    Named for what it holds. The two files beside it -- ``user_settings.json``
    and a repository's ``repo_settings.json`` -- carry the same keys as each
    other, and none of the keys in this one. See git_assistant.repo_config.
    """
    return Path(user_config_dir(APP_NAME, appauthor=False)) / "static_user_settings.json"


def old_config_path() -> Path:
    """The single file that held both halves, before they were split."""
    return Path(user_config_dir(APP_NAME, appauthor=False)) / "settings.json"


def _legacy_config_path() -> Path:
    """Path to the pre-rename settings, read once to migrate existing users."""
    return Path(user_config_dir(LEGACY_APP_NAME, appauthor=False)) / "settings.json"


# ---- what the file says about itself -------------------------------------------------
#: The first line of the file, answering the question somebody has when they
#: open it: why is this one not overridden like the others.
HEADER = "These settings are not overridden by repo_settings.json"

#: One sentence per key, written above it in the file. See
#: git_assistant.jsonc for how, and git_assistant.repo_config.FIELD_COMMENTS
#: for the other half of the schema.
FIELD_COMMENTS = {
    "provider": "Which backend generates text. See the Connection tab.",
    "provider_models": (
        "Model per provider, so switching provider does not carry a model "
        "name the new one has never heard of."
    ),
    "provider_temperatures": (
        "How adventurous each model may be, by provider and then by model. "
        "It is a property of the weights, not of the backend."
    ),
    "azure_api_version": "The api-version Azure pins its contract with.",
    "selected_model": (
        "LM Studio's model. Kept under its original name so settings files "
        "written by older versions still load."
    ),
    "repos": "The repositories this application manages.",
    "active_repo": "Path of the repository the window is working in.",
    "recent_repos": "Recently used repository paths, most recent first.",
    "scan_roots": "Folders scanned for repositories.",
    "watched_roots": "Folders watched, so a repository added there is noticed.",
    "mcp_allow_writes": (
        "Whether the command registered with a client offers the write tools. "
        "The flag lives in that command line; this only remembers what the "
        "tab last showed."
    ),
    "mcp_scope": "Which Claude Code scope the MCP server is registered in.",
    "theme": "How the window is painted: system, light, dark or pony.",
    "settings_tiers": (
        "Which settings each repository uses: user, repo or custom. The "
        "user's choice, so a repository cannot decide it is not being read."
    ),
    "audit_selected": (
        "Which audits are ticked, per repository. A selection: it changes "
        'what is on screen and nothing about what an audit does. "" is the '
        "answer for a repository nobody has ticked anything for yet."
    ),
    "audit_last": "Whose audit report is on screen, per repository.",
    "branch_pattern": (
        "Which branch naming convention is selected, per repository. Blank, "
        "and the name is what you type. The conventions themselves are a "
        "repository's setting, not this."
    ),
    "branch_user": (
        "A {user} typed in by hand, per repository. Blank asks git for the "
        "committer name."
    ),
    "default_template": (
        "The commit-message prompt that is always offered, whatever a "
        "repository ships.\nA project's own templates replace the named ones "
        "in user_settings.json and never this one, so there is always "
        "something to fall back to."
    ),
    "default_judge_prompt": (
        "What the code-review judge is asked. It is shown the exact prompt the "
        "reviewer was given and the exact answer it returned, and scores that "
        "answer out of ten.\n{prompt} and {reply} are filled in with the "
        "exchange being scored; anything else is left as typed."
    ),
    "repo_templates": (
        "Which template each repository uses, by repository key. A selection: "
        "it names one of the templates on offer and decides nothing about what "
        "any of them say."
    ),
}

DEFAULT_TEMPLATE_NAME = "Default"


@dataclass
class Template:
    """A named prompt template, so each project can have its own."""

    name: str
    text: str


@dataclass
class RepoEntry:
    """A git repository the user manages in the tray menu."""

    path: str
    label: str = ""
    owner: str = ""  # remote owner/org, e.g. "ONEoo7" (for disambiguation)
    template: str = ""  # named template to use; "" means the default one
    #: Named rule table this repository is code-reviewed against; "" means none
    #: has been chosen. The tables themselves live in code_review_rules.json --
    #: see git_assistant.review.rules -- because a rule set is far too large to
    #: rewrite on every debounced settings save.
    #:
    #: Superseded by `review_profile`, and kept: it is what a downgrade reads,
    #: and what the profile a repository is migrated to is built from.
    review_rules: str = ""
    #: Named review profile: which rules apply to which language at which
    #: version. See git_assistant.review.profiles.
    review_profile: str = ""

    def display(self) -> str:
        if self.label:
            return self.label
        name = Path(self.path).name or self.path
        return f"{self.owner}\\{name}" if self.owner else name


def norm_path(path: str) -> str:
    """Comparison form of a path: case- and separator-insensitive."""
    return os.path.normcase(os.path.normpath(path))


_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9._-]")


def repo_key(repo_path: str) -> str:
    """A filename for a repository whose path may contain anything.

    The hash is the identity -- taken from ``norm_path``, so ``D:\\Repo`` and
    ``d:\\repo\\`` are one history, the same answer the repository tree gives.
    The readable stem is for whoever opens the folder.

    It lives here rather than in one of the stores that needs it because both
    the agent runs and the code reviews are filed under it, and two
    implementations would be free to disagree about which paths are one
    repository.
    """
    norm = norm_path(repo_path)
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]
    stem = _UNSAFE_IN_FILENAME.sub("_", Path(norm).name)[:32].strip("._") or "repo"
    return f"{stem}-{digest}"


@dataclass
class RepoNode:
    """A repository plus the repositories nested inside it (its submodules)."""

    entry: "RepoEntry"
    children: list["RepoNode"] = field(default_factory=list)

    def walk(self) -> "Iterable[tuple[RepoEntry, int]]":
        """Yield ``(entry, depth)`` for this node and its descendants."""

        def rec(node: "RepoNode", depth: int):
            yield node.entry, depth
            for child in node.children:
                yield from rec(child, depth + 1)

        yield from rec(self, 0)


def build_repo_tree(entries: Iterable[RepoEntry]) -> list[RepoNode]:
    """Nest each repository under the deepest repository that contains it.

    Submodules live inside their parent's working tree, so containment is what
    identifies them -- no extra field to keep in step with the paths, and repos
    recorded before submodules were scanned nest correctly on the next load.
    Input order is preserved among siblings, and duplicates are dropped.
    """
    nodes: dict[str, RepoNode] = {}
    order: list[str] = []
    for entry in entries:
        key = norm_path(entry.path)
        if key in nodes:
            continue
        nodes[key] = RepoNode(entry)
        order.append(key)

    roots: list[RepoNode] = []
    for key in order:
        parent = ""
        for other in nodes:
            if key.startswith(other + os.sep) and len(other) > len(parent):
                parent = other
        (nodes[parent].children if parent else roots).append(nodes[key])
    return roots


def _profiles_from(raw: object) -> list:
    """Review profiles, rebuilt by the module that owns their shape.

    Imported here rather than at module scope: `review.profiles` reads the
    shipped rules, and loading settings must not pay for that.
    """
    from git_assistant.review.profiles import Profile

    found = [Profile.from_dict(entry) for entry in raw] if isinstance(raw, list) else []
    return [p for p in found if p is not None]


def _temperatures_from(raw: object) -> dict[str, dict[str, float]]:
    """Rebuild the nested temperature map, dropping anything unusable.

    Two levels of hand-editable JSON, so two levels of not trusting it. An
    entry that cannot be read as a number is left out entirely rather than
    coerced, so the default applies and the model is asked for something a
    provider will accept.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for provider, models in raw.items():
        if not isinstance(models, dict):
            continue
        kept = {}
        for model, value in models.items():
            try:
                kept[str(model)] = _clamp_temperature(float(value))
            except (TypeError, ValueError):
                continue
        if kept:
            out[str(provider)] = kept
    return out


@dataclass
class Settings:
    """All persisted user configuration."""

    #: Which backend generates the message. See git_assistant.providers.
    provider: str = DEFAULT_PROVIDER
    #: Per-provider model, so switching provider does not carry a model name
    #: the new one has never heard of into its request.
    provider_models: dict[str, str] = field(default_factory=dict)
    #: How adventurous each model is allowed to be, keyed by provider and then
    #: by model: ``{"lmstudio": {"qwen3.5-4b": 0.2}}``. Per model rather than
    #: per provider because it is a property of the weights -- what is careful
    #: for one is mute for another -- and nested rather than under a compound
    #: key because a model id contains slashes, colons and dots, and every
    #: separator that would survive them is one nobody can read in the file.
    provider_temperatures: dict[str, dict[str, float]] = field(default_factory=dict)
    #: Azure pins its contract with this; a mismatch is a 404 that says
    #: nothing about the version.
    azure_api_version: str = "2024-10-21"
    selected_model: str = ""
    repos: list[RepoEntry] = field(default_factory=list)
    active_repo: str = ""  # path of the active RepoEntry
    recent_repos: list[str] = field(default_factory=list)  # paths, most-recent first
    scan_roots: list[str] = field(default_factory=list)  # folders scanned for repos
    watched_roots: list[str] = field(default_factory=list)  # roots auto-watched for new repos
    # Committer identities live in committer_identities.json, not here -- see
    # git_assistant.identities. An older build wrote them into this file; that
    # key is migrated and removed on first run.
    # ---- Audit tab ---------------------------------------------------------
    # ---- Code Review tab ---------------------------------------------------
    # ---- MCP server --------------------------------------------------------
    #: Whether the command registered with a client offers the write tools.
    #: The flag lives in that command line, not here -- this only remembers
    #: what the tab last showed. See git_assistant.mcp.launch.
    mcp_allow_writes: bool = False
    mcp_scope: str = "user"  # which Claude Code scope to register in
    #: How the window is painted: follow the system, light, dark, or pink. See
    #: git_assistant.ui.theme, which owns the names -- config.py should not
    #: have to know what a palette is. An unknown value falls back rather than
    #: refusing to start.
    #: Which settings are in force for a repository: ``{repo key: "user" |
    #: "repo" | "custom"}``. The user's choice, so it is kept in the user's
    #: file -- a repository cannot decide it is not being read. Absent means
    #: nobody has chosen; see git_assistant.repo_config.effective_tier.
    settings_tiers: dict[str, str] = field(default_factory=dict)
    #: What is ticked and what is being read on the Audit tab, per repo key.
    #: Selections, so they are here and not in the settings a repository can
    #: carry -- see `audits_selected`.
    audit_selected: dict[str, list[str]] = field(default_factory=dict)
    audit_last: dict[str, str] = field(default_factory=dict)
    #: How a new branch is named on the Branches tab, per repo key: ``""`` for
    #: the plain name and a pattern string for one of the offered conventions.
    #: The patterns themselves are a repository's; which one is selected is not.
    branch_pattern: dict[str, str] = field(default_factory=dict)
    #: A `{user}` typed in by hand, per repo key. Blank -- the usual case -- is
    #: whatever git says the committer is called here.
    branch_user: dict[str, str] = field(default_factory=dict)
    #: The commit-message prompt that is always offered, whatever a repository
    #: ships. Here rather than in the settings a repository can carry, because
    #: that is the point of it: a project's own prompt replaces the *named*
    #: templates and never this one, so there is always something to fall back
    #: to without editing a file the whole team shares.
    default_template: str = DEFAULT_TEMPLATE
    #: Which template each repository uses, by repo key. A selection: it names
    #: one of the templates on offer and decides nothing about what they say.
    repo_templates: dict[str, str] = field(default_factory=dict)
    #: What the code-review judge is asked. Here for the same reason
    #: `default_template` is: it is the one prompt that is always there, so a
    #: repository's own settings can never leave a judge with nothing to say.
    #: `{prompt}` and `{reply}` are filled in with the exchange being scored;
    #: see git_assistant.review.judge.
    default_judge_prompt: str = JUDGE_TEMPLATE
    theme: str = "system"

    # ---- per-provider settings ---------------------------------------------
    def provider_model(self, key: str) -> str:
        """The model chosen for a provider.

        LM Studio keeps using `selected_model` so existing settings files load
        with their model intact; everything else is keyed by provider.
        """
        if key == DEFAULT_PROVIDER:
            return self.selected_model
        return (self.provider_models.get(key) or "").strip()

    def set_provider_model(self, key: str, value: str) -> None:
        value = (value or "").strip()
        if key == DEFAULT_PROVIDER:
            self.selected_model = value
            return
        if value:
            self.provider_models[key] = value
        else:
            self.provider_models.pop(key, None)

    def active_model(self) -> str:
        return self.provider_model(self.provider)

    # ---- temperature, per provider and per model ---------------------------
    def temperature_for(self, provider: str, model: str) -> float:
        """How adventurous this model may be, or the default nobody has changed.

        Never raises and never returns something a provider would reject: a
        hand-edited file with ``"warm"`` in it, or with 40, gets the default
        and the clamp respectively. A rejected completion is a lost commit
        message; a slightly wrong temperature is a slightly worse one.
        """
        try:
            value = float(self.provider_temperatures.get(provider, {})[model])
        except (KeyError, TypeError, ValueError):
            return DEFAULT_TEMPERATURE
        return _clamp_temperature(value)

    def set_temperature(self, provider: str, model: str, value: float | None) -> None:
        """Pin a temperature, or drop it back to the default with ``None``."""
        for_provider = self.provider_temperatures.setdefault(provider, {})
        if value is None:
            for_provider.pop(model, None)
            if not for_provider:
                self.provider_temperatures.pop(provider, None)
            return
        for_provider[model] = _clamp_temperature(float(value))

    def has_temperature(self, provider: str, model: str) -> bool:
        """Whether one was chosen, as opposed to the default being in effect."""
        return model in self.provider_temperatures.get(provider, {})

    def active_temperature(self) -> float:
        return self.temperature_for(self.provider, self.active_model())

    # ---- what is selected in the window ------------------------------------
    # Selections, not settings: they change what is on screen and nothing about
    # what a run does, so they are the user's and are not shared with anybody's
    # repository. Per repository all the same -- ticking three audits in one
    # project is not a statement about the next one.
    #
    # The entry under "" is the answer for a repository nobody has chosen for
    # yet. It is also where the single global list from before this split
    # lands, so an upgrade opens on the audits that were already ticked.
    def audits_selected(self, repo_path: str) -> list[str]:
        """Which audits are ticked here, or the last answer given anywhere."""
        chosen = self.audit_selected.get(repo_key(repo_path)) if repo_path else None
        return list(chosen if chosen is not None else self.audit_selected.get("", []))

    def set_audits_selected(self, repo_path: str, agent_ids) -> None:
        if not repo_path:
            return
        self.audit_selected[repo_key(repo_path)] = list(agent_ids)
        # Kept as the answer for the next repository too, so a habit does not
        # have to be re-entered once per project.
        self.audit_selected[""] = list(agent_ids)

    def audit_shown(self, repo_path: str) -> str:
        """Whose report is on screen here, or the last one read anywhere."""
        shown = self.audit_last.get(repo_key(repo_path)) if repo_path else ""
        return shown or self.audit_last.get("", "")

    def set_audit_shown(self, repo_path: str, agent_id: str) -> None:
        if not repo_path:
            return
        self.audit_last[repo_key(repo_path)] = agent_id
        self.audit_last[""] = agent_id

    def branch_pattern_for(self, repo_path: str) -> str:
        """The pattern selected here, or ``""`` for the plain name.

        Not carried between repositories, unlike the ticked audits: the plain
        name is the default and it is the answer for most repositories, so a
        convention chosen for one must not quietly become the answer for the
        next one cloned.
        """
        return self.branch_pattern.get(repo_key(repo_path), "") if repo_path else ""

    def set_branch_pattern(self, repo_path: str, pattern: str) -> None:
        if not repo_path:
            return
        key = repo_key(repo_path)
        if pattern:
            self.branch_pattern[key] = pattern
        else:
            self.branch_pattern.pop(key, None)  # back to the default; say nothing

    def branch_user_for(self, repo_path: str) -> str:
        """The ``{user}`` typed in here, or ``""`` to let git answer."""
        return self.branch_user.get(repo_key(repo_path), "") if repo_path else ""

    def set_branch_user(self, repo_path: str, user: str) -> None:
        if not repo_path:
            return
        key = repo_key(repo_path)
        if (user or "").strip():
            self.branch_user[key] = user.strip()
        else:
            self.branch_user.pop(key, None)

    # ---- which settings are in force ---------------------------------------
    def settings_tier(self, repo_path: str) -> str:
        """The tier chosen for ``repo_path``, or ``""`` when nobody has chosen."""
        return self.settings_tiers.get(repo_key(repo_path), "") if repo_path else ""

    def set_settings_tier(self, repo_path: str, tier: str) -> None:
        """Choose which settings a repository uses. ``""`` forgets the choice."""
        if not repo_path:
            return
        key = repo_key(repo_path)
        if tier:
            self.settings_tiers[key] = tier
        else:
            self.settings_tiers.pop(key, None)

    def active_repo_entry(self) -> RepoEntry | None:
        for r in self.repos:
            if r.path == self.active_repo:
                return r
        return self.repos[0] if self.repos else None

    # ---- templates ---------------------------------------------------------
    # Which template a repository uses is a selection and lives here. The
    # templates themselves decide what is sent, so they are a setting and live
    # in the shared schema -- see repo_config.PromptRules and the readers on
    # repo_config.Bound, which is what every consumer is handed.
    def repo_template(self, repo_path: str) -> str:
        """The name of the template chosen for a repository, or ``""``."""
        return self.repo_templates.get(repo_key(repo_path), "") if repo_path else ""

    def set_repo_template(self, repo_path: str, name: str) -> None:
        """Assign a template to a repository ("" or the default clears it)."""
        if not repo_path:
            return
        key = repo_key(repo_path)
        if name and name != DEFAULT_TEMPLATE_NAME:
            self.repo_templates[key] = name
        else:
            self.repo_templates.pop(key, None)  # back to the default; say nothing

    def repoint_template(self, old: str, new: str) -> None:
        """Follow a rename, or a removal when ``new`` is ``""``.

        Only the pointers. The templates themselves are in the shared settings,
        and the caller that renames one there calls this so the repositories
        that named it do not quietly fall back to the default.
        """
        for key, name in list(self.repo_templates.items()):
            if name != old:
                continue
            if new:
                self.repo_templates[key] = new
            else:
                self.repo_templates.pop(key, None)

    # ---- code-review rule tables -------------------------------------------
    # Only the *assignment* lives here. The tables are in their own file, so
    # these four methods keep the pointers honest when one is renamed or gone.
    def review_profile_for_repo(self, repo_path: str) -> str:
        """Name of the profile this repository is reviewed with."""
        for r in self.repos:
            if r.path == repo_path:
                return r.review_profile
        return ""

    def set_repo_review_profile(self, repo_path: str, name: str) -> None:
        for r in self.repos:
            if r.path == repo_path:
                r.review_profile = name or ""
                return

    def review_table_for_repo(self, repo_path: str) -> str:
        """Name of the rule table this repository is reviewed against."""
        for r in self.repos:
            if r.path == repo_path:
                return r.review_rules
        return ""

    def set_repo_review_table(self, repo_path: str, name: str) -> None:
        for r in self.repos:
            if r.path == repo_path:
                r.review_rules = name or ""
                return

    def rename_review_table(self, old: str, new: str) -> None:
        """Repoint every repository that used ``old``.

        Without this a rename leaves repositories pointing at a table that no
        longer exists, and their next review runs against no rules at all --
        which looks exactly like a clean review.
        """
        for r in self.repos:
            if r.review_rules == old:
                r.review_rules = new

    def remove_review_table(self, name: str) -> None:
        """Forget a deleted table; its repositories are left without one."""
        for r in self.repos:
            if r.review_rules == name:
                r.review_rules = ""

    # ---- recency ordering (for the tray menu) ------------------------------
    def ordered_repos(self) -> list[RepoEntry]:
        """Repos ordered active-first, then most-recently used, then the rest."""
        by_path = {r.path: r for r in self.repos}
        order: list[str] = []
        for p in [self.active_repo, *self.recent_repos, *by_path]:
            if p and p in by_path and p not in order:
                order.append(p)
        return [by_path[p] for p in order]

    def mark_recent(self, path: str) -> None:
        """Record ``path`` as the most-recently used repository."""
        valid = {r.path for r in self.repos}
        rec = [p for p in self.recent_repos if p != path and p in valid]
        if path in valid:
            rec.insert(0, path)
        self.recent_repos = rec

    # ---- (de)serialization -------------------------------------------------
    def to_dict(self) -> dict:
        data = asdict(self)
        data["repos"] = [asdict(r) for r in self.repos]
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        # A provider this build has never heard of -- hand-edited, or written
        # by a newer version -- falls back rather than stopping the app.
        if not is_known(str(clean.get("provider", ""))):
            clean["provider"] = DEFAULT_PROVIDER
        # Hand-edited files can put anything here; a non-dict would crash on
        # first use rather than at load, far from the cause.
        for name in ("provider_models",):
            value = clean.get(name)
            clean[name] = (
                {str(k): str(v) for k, v in value.items()}
                if isinstance(value, dict)
                else {}
            )
        clean["provider_temperatures"] = _temperatures_from(
            clean.get("provider_temperatures")
        )
        repos = clean.get("repos") or []
        clean["repos"] = [
            RepoEntry(
                path=r.get("path", ""),
                label=r.get("label", ""),
                owner=r.get("owner", ""),
                template=r.get("template", ""),
                # Built field by field, so a new one is dropped on load unless
                # it is named here -- silent, and only visible after a restart.
                review_rules=r.get("review_rules", ""),
                review_profile=r.get("review_profile", ""),
            )
            for r in repos
            if isinstance(r, dict) and r.get("path")
        ]
        # Where these used to live. `RepoEntry.template` held the pointer and
        # the old single settings file held the default; both are read here so
        # an upgrade does not silently reset a prompt somebody wrote.
        carried = dict(clean.get("repo_templates") or {})
        for entry in clean["repos"]:
            if entry.template and repo_key(entry.path) not in carried:
                carried[repo_key(entry.path)] = entry.template
        clean["repo_templates"] = {
            str(k): str(v) for k, v in carried.items() if isinstance(v, str) and v
        }
        if not clean.get("default_template"):
            legacy = data.get("prompt_template")
            clean["default_template"] = (
                str(legacy) if isinstance(legacy, str) and legacy else DEFAULT_TEMPLATE
            )
        return cls(**clean)

    # ---- load / save -------------------------------------------------------
    @classmethod
    def load(cls) -> "Settings":
        path = config_path()
        if not path.exists():
            # Migrate settings from the pre-rename location, if present.
            legacy = _legacy_config_path()
            if legacy.exists():
                path = legacy
            else:
                return cls()
        try:
            data = jsonc.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls.from_dict(data)

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            jsonc.dumps(self.to_dict(), FIELD_COMMENTS, HEADER),
            encoding="utf-8",
        )
