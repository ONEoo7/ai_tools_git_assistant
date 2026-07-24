"""User settings: dataclass model + JSON persistence in the platform config dir.

JSON (not TOML) is used for on-disk storage so the editable multi-line prompt
template round-trips losslessly without a third-party TOML writer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from platformdirs import user_config_dir

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


@dataclass
class RepoEntry:
    """A git repository the user manages in the tray menu."""

    path: str
    label: str = ""
    owner: str = ""  # remote owner/org, e.g. "ONEoo7" (for disambiguation)

    def display(self) -> str:
        if self.label:
            return self.label
        name = Path(self.path).name or self.path
        return f"{self.owner}\\{name}" if self.owner else name


@dataclass
class Settings:
    """All persisted user configuration."""

    lmstudio_ip: str = "127.0.0.1"
    lmstudio_port: int = 1234
    selected_model: str = ""
    repos: list[RepoEntry] = field(default_factory=list)
    active_repo: str = ""  # path of the active RepoEntry
    recent_repos: list[str] = field(default_factory=list)  # paths, most-recent first
    scan_roots: list[str] = field(default_factory=list)  # folders scanned for repos
    watched_roots: list[str] = field(default_factory=list)  # roots auto-watched for new repos
    diff_mode: str = "cached"  # "cached" (git diff --cached) | "working" (git diff HEAD)
    prompt_template: str = DEFAULT_TEMPLATE
    context_window: int = 32768  # total tokens for input+output (0 => auto-detect)
    safety_margin: float = 0.10  # fraction of the window reserved for the model's output
    ignore_globs: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_GLOBS))

    # ---- URL helpers -------------------------------------------------------
    @property
    def base_url(self) -> str:
        return f"http://{self.lmstudio_ip}:{self.lmstudio_port}"

    def active_repo_entry(self) -> RepoEntry | None:
        for r in self.repos:
            if r.path == self.active_repo:
                return r
        return self.repos[0] if self.repos else None

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
        repos = clean.get("repos") or []
        clean["repos"] = [
            RepoEntry(
                path=r.get("path", ""),
                label=r.get("label", ""),
                owner=r.get("owner", ""),
            )
            for r in repos
            if isinstance(r, dict) and r.get("path")
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
