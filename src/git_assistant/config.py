"""User settings: dataclass model + JSON persistence in the platform config dir.

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

from git_assistant.providers import DEFAULT_PROVIDER, is_known
from git_assistant.prompts import DEFAULT_TEMPLATE

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
    """Return the path to the settings JSON file (directory may not yet exist)."""
    return Path(user_config_dir(APP_NAME, appauthor=False)) / "settings.json"


def _legacy_config_path() -> Path:
    """Path to the pre-rename settings, read once to migrate existing users."""
    return Path(user_config_dir(LEGACY_APP_NAME, appauthor=False)) / "settings.json"


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
    #: Per-provider endpoint, keyed by provider key. Only for providers whose
    #: address the user supplies (Azure, a proxy); the rest have a fixed one.
    #: API keys are NOT here -- they live in the Windows Credential Manager,
    #: see git_assistant.credentials.
    provider_endpoints: dict[str, str] = field(default_factory=dict)
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
    lmstudio_ip: str = "127.0.0.1"
    lmstudio_port: int = 1234
    selected_model: str = ""
    parallel_calls: int = 4  # concurrent LLM requests during map-reduce
    repos: list[RepoEntry] = field(default_factory=list)
    active_repo: str = ""  # path of the active RepoEntry
    recent_repos: list[str] = field(default_factory=list)  # paths, most-recent first
    scan_roots: list[str] = field(default_factory=list)  # folders scanned for repos
    watched_roots: list[str] = field(default_factory=list)  # roots auto-watched for new repos
    diff_mode: str = "cached"  # "cached" (git diff --cached) | "working" (git diff HEAD)
    # The default template, used by any repo without one of its own. Kept under
    # its original name so existing settings files load unchanged.
    prompt_template: str = DEFAULT_TEMPLATE
    templates: list[Template] = field(default_factory=list)  # named extras
    # ---- how long a commit message may be ----------------------------------
    #: Asked of the model and checked afterwards; 0 turns a rule off entirely.
    #: See git_assistant.commit_style for what the numbers mean.
    commit_subject_target: int = 50
    commit_subject_limit: int = 72
    commit_body_limit: int = 1000
    # Committer identities live in committer_identities.json, not here -- see
    # git_assistant.identities. An older build wrote them into this file; that
    # key is migrated and removed on first run.
    # ---- Audit tab ---------------------------------------------------------
    agents_narrate: bool = True  # let the provider write the report's prose
    agent_last_id: str = ""  # the audit whose report and history are on screen
    #: The audits ticked to run. Separate from `agent_last_id`, which is the one
    #: being *read*: a run of three leaves three reports, and the tab can only
    #: show one of them at a time.
    agent_selected_ids: list[str] = field(default_factory=list)
    agent_fast_mode: bool = False  # skip the per-file history breakdown
    agent_large_file_mb: int = 5  # a binary this size is worth flagging
    #: When a branch counts as stale, and when the consistency audit may
    #: propose deleting one. A handful of values, so they live here rather than
    #: in a store of their own. See git_assistant.agents.branches.StaleRules --
    #: which owns the shape, because config.py should not have to know it.
    stale_branch_rules: dict = field(default_factory=dict)
    #: Recorded runs kept per repository and agent (0 keeps everything). The
    #: runs themselves live beside this file; see git_assistant.agents.history.
    agent_history_limit: int = 20
    #: Generated commit messages kept per repository (0 keeps everything).
    #: They live beside this file; see git_assistant.commit_history.
    commit_history_limit: int = 20
    # ---- Code Review tab ---------------------------------------------------
    #: Recorded reviews kept per repository (0 keeps everything). Like the agent
    #: runs, the reviews live beside this file; see git_assistant.review.history.
    review_history_limit: int = 20
    #: Review profiles, as prompt templates are: a few hundred bytes of
    #: pointers, so they belong in this file rather than in a store of their
    #: own. The rules they point at do not; see git_assistant.review.rules.
    review_profiles: list = field(default_factory=list)
    # ---- MCP server --------------------------------------------------------
    #: Whether the command registered with a client offers the write tools.
    #: The flag lives in that command line, not here -- this only remembers
    #: what the tab last showed. See git_assistant.mcp.launch.
    mcp_allow_writes: bool = False
    mcp_scope: str = "user"  # which Claude Code scope to register in
    # ---- Langfuse tracing --------------------------------------------------
    #: Off until the user turns it on and says where. Neither key is here and
    #: neither may ever be written here -- both are in the Windows Credential
    #: Manager, see git_assistant.tracing.settings.
    langfuse_enabled: bool = False
    langfuse_host: str = ""
    langfuse_environment: str = "development"
    langfuse_release: str = ""  # blank => this build's version
    #: Whether the prompt and the reply travel with the trace. Off, a trace
    #: still carries the model, the timings, the tokens and any error.
    langfuse_send_prompts: bool = True
    #: How the window is painted: follow the system, light, dark, or pink. See
    #: git_assistant.ui.theme, which owns the names -- config.py should not
    #: have to know what a palette is. An unknown value falls back rather than
    #: refusing to start.
    #: Which settings are in force for a repository: ``{repo key: "user" |
    #: "repo" | "custom"}``. The user's choice, so it is kept in the user's
    #: file -- a repository cannot decide it is not being read. Absent means
    #: nobody has chosen; see git_assistant.repo_config.effective_tier.
    settings_tiers: dict[str, str] = field(default_factory=dict)
    #: Whether the per-repository settings below have been carried into the
    #: user tier. See git_assistant.repo_config.migrate_user_settings; the old
    #: fields are kept so a downgrade still finds them.
    settings_migrated: bool = False
    theme: str = "system"
    context_window: int = 32768  # total tokens for input+output (0 => auto-detect)
    safety_margin: float = 0.10  # fraction of the window reserved for the model's output
    ignore_globs: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_GLOBS))

    # ---- URL helpers -------------------------------------------------------
    @property
    def base_url(self) -> str:
        """LM Studio's address. Other providers use `provider_endpoint`."""
        return f"http://{self.lmstudio_ip}:{self.lmstudio_port}"

    # ---- per-provider settings ---------------------------------------------
    def provider_endpoint(self, key: str) -> str:
        return (self.provider_endpoints.get(key) or "").strip()

    def set_provider_endpoint(self, key: str, value: str) -> None:
        value = (value or "").strip()
        if value:
            self.provider_endpoints[key] = value
        else:
            self.provider_endpoints.pop(key, None)

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

    # ---- stale branches ----------------------------------------------------
    def stale_rules(self):
        """The stale-branch rules, as an object rather than a dict.

        Rebuilt on each read rather than held: it is asked for once per audit,
        and a hand-edited settings file must not be able to put something
        unusable into a long-lived attribute.
        """
        from git_assistant.agents.branches import StaleRules

        return StaleRules.from_dict(self.stale_branch_rules)

    def set_stale_rules(self, rules) -> None:
        self.stale_branch_rules = rules.to_dict()

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
    def template_names(self) -> list[str]:
        """Every selectable template, the default first."""
        return [DEFAULT_TEMPLATE_NAME, *(t.name for t in self.templates)]

    def template_text(self, name: str) -> str:
        """Body of a named template, falling back to the default."""
        if name and name != DEFAULT_TEMPLATE_NAME:
            for t in self.templates:
                if t.name == name:
                    return t.text
        return self.prompt_template or DEFAULT_TEMPLATE

    def template_for_repo(self, repo_path: str) -> str:
        """The template a repository should be described with."""
        for r in self.repos:
            if r.path == repo_path:
                return self.template_text(r.template)
        return self.template_text("")

    def set_repo_template(self, repo_path: str, name: str) -> None:
        """Assign a template to a repository ("" or the default clears it)."""
        for r in self.repos:
            if r.path == repo_path:
                r.template = "" if name == DEFAULT_TEMPLATE_NAME else name
                return

    def rename_template(self, old: str, new: str) -> None:
        """Rename a template and repoint the repositories that referenced it."""
        for t in self.templates:
            if t.name == old:
                t.name = new
        for r in self.repos:
            if r.template == old:
                r.template = new

    def remove_template(self, name: str) -> None:
        """Delete a template; repositories using it fall back to the default."""
        self.templates = [t for t in self.templates if t.name != name]
        for r in self.repos:
            if r.template == name:
                r.template = ""

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

    def review_profile(self, name: str):
        """A profile by name, or ``None``."""
        return next((p for p in self.review_profiles if p.name == name), None)

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
        data["templates"] = [asdict(t) for t in self.templates]
        # Written by the profile itself: its shape is nested, and config.py
        # should not have to know it.
        data["review_profiles"] = [p.to_dict() for p in self.review_profiles]
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
        for name in ("provider_endpoints", "provider_models"):
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
        clean["templates"] = [
            Template(name=t.get("name", ""), text=t.get("text", ""))
            for t in (clean.get("templates") or [])
            if isinstance(t, dict) and t.get("name")
        ]
        clean["review_profiles"] = _profiles_from(clean.get("review_profiles"))
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
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls.from_dict(data)

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
