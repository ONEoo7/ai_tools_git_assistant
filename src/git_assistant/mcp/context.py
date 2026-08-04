"""What a tool is allowed to touch, and which settings it sees.

Two rules live here.

*Only configured repositories.* A server that runs git wherever a caller points
is a much larger thing than this one; every tool resolves its ``repo`` argument
against the list in the window, and refuses anything else.

*Settings are re-read, not remembered.* The server outlives many edits: a client
starts it once and keeps it for the session, while the user changes provider or
adds a repository in the tray window meanwhile.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from git_assistant import git_ops
from git_assistant.config import Settings, config_path, norm_path

log = logging.getLogger("git_assistant.mcp")

#: Said the same way wherever it is reached from, and it says what to do.
NO_REPOSITORIES = (
    "No repositories are configured. Add one in Git Assistant's Repositories tab."
)


class ToolError(Exception):
    """Reported to the caller as a failed tool, with something to do about it."""


class SettingsCache:
    """The settings file, re-read when it changes on disk."""

    def __init__(self) -> None:
        self._value: Settings | None = None
        self._stamp: int | None = None

    def current(self) -> Settings:
        try:
            stamp = config_path().stat().st_mtime_ns
        except OSError:
            stamp = self._stamp  # unreadable: keep what we had
        if self._value is None or stamp != self._stamp:
            loaded = Settings.load()
            # `Settings.save()` is a plain write and `load()` turns any read
            # failure into defaults, so a read landing mid-write looks like
            # "all your configuration vanished". Keep the last good one and
            # try again on the next request.
            if self._value is None or loaded.repos or not self._value.repos:
                self._value = loaded
                self._stamp = stamp
            else:
                log.warning("settings read came back empty; keeping the previous one")
        return self._value


@dataclass
class ToolContext:
    """Everything the handlers share."""

    allow_writes: bool = False
    settings_cache: SettingsCache = field(default_factory=SettingsCache)

    def settings(self) -> Settings:
        """A private copy, so concurrent calls cannot see each other's edits.

        `CommitGenerator` reads `active_repo` and `diff_mode` off the settings
        rather than taking them as arguments, so a shared instance would let two
        calls describe each other's repository.
        """
        return Settings.from_dict(self.settings_cache.current().to_dict())

    # ---- repositories ----------------------------------------------------
    def resolve(self, settings: Settings, wanted: str | None) -> str:
        """The repository a tool should act on, from a path, a label or nothing."""
        known = self._known(settings)
        if not known:
            raise ToolError(NO_REPOSITORIES)
        if not wanted:
            active = settings.active_repo
            if active and norm_path(active) in {norm_path(p) for p in known}:
                return active
            return known[0]

        target = norm_path(wanted)
        for path in known:
            if norm_path(path) == target:
                return path
        for entry in settings.repos:  # by label, as the window shows it
            if entry.label and entry.label.lower() == wanted.lower():
                return entry.path
        tail = [p for p in known if Path(p).name.lower() == wanted.lower()]
        if len(tail) == 1:
            return tail[0]

        listed = "\n".join(f"  {p}" for p in known[:20])
        raise ToolError(
            f"{wanted!r} is not a configured repository. Known repositories:\n{listed}"
        )

    def _known(self, settings: Settings) -> list[str]:
        """Configured repositories, and the submodules inside them."""
        paths: list[str] = []
        seen: set[str] = set()
        for entry in settings.repos:
            for path in (entry.path, *git_ops.find_submodules(entry.path)):
                key = norm_path(path)
                if key not in seen:
                    seen.add(key)
                    paths.append(path)
        return paths
