"""User settings: dataclass model + JSON persistence in the platform config dir.

JSON (not TOML) is used for on-disk storage so the editable multi-line prompt
template round-trips losslessly without a third-party TOML writer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant.providers import DEFAULT_PROVIDER, is_known
from git_assistant.prompts import DEFAULT_TEMPLATE

APP_NAME = "git-assistant"
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

    def display(self) -> str:
        if self.label:
            return self.label
        name = Path(self.path).name or self.path
        return f"{self.owner}\\{name}" if self.owner else name


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
    # Committer identities live in committer_identities.json, not here -- see
    # git_assistant.identities. An older build wrote them into this file; that
    # key is migrated and removed on first run.
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
        repos = clean.get("repos") or []
        clean["repos"] = [
            RepoEntry(
                path=r.get("path", ""),
                label=r.get("label", ""),
                owner=r.get("owner", ""),
                template=r.get("template", ""),
            )
            for r in repos
            if isinstance(r, dict) and r.get("path")
        ]
        clean["templates"] = [
            Template(name=t.get("name", ""), text=t.get("text", ""))
            for t in (clean.get("templates") or [])
            if isinstance(t, dict) and t.get("name")
        ]
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
