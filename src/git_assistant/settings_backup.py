"""The settings this build ships with, kept so a broken settings.json has a way back.

    <config dir>/settings.json                 what the application is using
    <config dir>/default_settings.json         what it shipped with
    <config dir>/default_settings.json.sha256  a checksum of that

Written once, from the constants in ``config.Settings`` -- so this is the
*factory* answer and not a rolling backup of whatever was last saved. That
distinction is what makes the checksum worth having: the contents are decided
by the build, so a file that does not match its checksum has been changed by
something other than this application, and there is nothing left here that
knows what it should have said. That case is reported as a damaged
installation, because that is what it is.

Restoring keeps the repository list. Which repositories you manage is not a
setting this build has an opinion about -- it is work, and a factory reset that
threw it away would be a reset nobody dares press.

What this is and is not: it catches a file corrupted by a crash, a half-written
save, or a hand-edit that went wrong. It cannot stop someone who edits the file
*and* recomputes the checksum -- both are writable by whoever can write either.
It is an integrity check, not a signature, and it is not pretending otherwise.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, fields
from enum import StrEnum
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant.config import APP_NAME, Settings

DEFAULTS_FILE = "default_settings.json"
CHECKSUM_SUFFIX = ".sha256"

#: What a restore puts back, and what it leaves alone. These are the user's
#: work rather than their preferences: the repositories they manage, where they
#: were found, and which one they were last looking at.
KEPT = ("repos", "active_repo", "recent_repos", "scan_roots", "watched_roots")


class Integrity(StrEnum):
    """What can be said about the shipped settings on disk."""

    INTACT = "intact"
    MISSING = "missing"  # never written; the first run has not happened yet
    DAMAGED = "damaged"  # present, and not what it was written as


@dataclass(frozen=True)
class Check:
    state: Integrity
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state is Integrity.INTACT

    @property
    def needs_reinstall(self) -> bool:
        """Only damage does. A file that was never written is written now."""
        return self.state is Integrity.DAMAGED

    def summary(self) -> str:
        if self.state is Integrity.INTACT:
            return "The shipped settings are intact."
        if self.state is Integrity.MISSING:
            return "The shipped settings have not been written yet."
        return f"The shipped settings have been altered: {self.detail}"


def defaults_path() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False)) / DEFAULTS_FILE


def checksum_path() -> Path:
    return defaults_path().with_name(DEFAULTS_FILE + CHECKSUM_SUFFIX)


def digest(text: str) -> str:
    """The checksum of ``text``, over its bytes as they are written."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- writing -------------------------------------------------------------------------
def _shipped_text() -> str:
    """The factory settings, rendered exactly as they are written to disk.

    Rendered once here and hashed from the same string, so the checksum is of
    the file rather than of an idea about the file.
    """
    factory = Settings()
    data = factory.to_dict()
    # The repository list is the user's, and a factory file that carried one
    # would be a factory file with somebody's folders in it.
    for key in KEPT:
        data.pop(key, None)
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)


def write_defaults() -> str:
    """Write the shipped settings and their checksum. A problem, or ``""``."""
    text = _shipped_text()
    try:
        path = defaults_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        for target, body in ((path, text), (checksum_path(), digest(text))):
            tmp = target.with_name(f"{target.name}.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, target)
    except OSError as exc:
        return str(exc)
    return ""


def ensure_defaults() -> bool:
    """Write them if they are not there. True if written.

    Not if they are damaged: replacing a file that failed its checksum would
    erase the one piece of evidence that anything is wrong, and would do it
    silently on the next launch.
    """
    if defaults_path().exists():
        return False
    return write_defaults() == ""


# ---- reading -------------------------------------------------------------------------
def check() -> Check:
    """Whether the shipped settings on disk are the ones that were written."""
    path, sums = defaults_path(), checksum_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Check(Integrity.MISSING)
    except (OSError, UnicodeDecodeError) as exc:
        return Check(Integrity.DAMAGED, f"{path.name} could not be read ({exc})")

    try:
        recorded = sums.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return Check(Integrity.DAMAGED, f"{sums.name} is missing")
    except (OSError, UnicodeDecodeError) as exc:
        return Check(Integrity.DAMAGED, f"{sums.name} could not be read ({exc})")

    found = digest(text)
    if found != recorded:
        return Check(
            Integrity.DAMAGED,
            f"the checksum is {recorded[:12] or '(empty)'}... but the file is "
            f"{found[:12]}...",
        )
    return Check(Integrity.INTACT)


def shipped() -> Settings | None:
    """The shipped settings, or ``None`` when they cannot be trusted."""
    if not check().ok:
        return None
    try:
        data = json.loads(defaults_path().read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return None
    return Settings.from_dict(data) if isinstance(data, dict) else None


def apply_over(current: Settings) -> str:
    """Put the shipped settings onto ``current``. A problem, or ``""``.

    Onto, and not in place of. One ``Settings`` object is passed to every panel,
    the tray and every worker, and they hold the object rather than a way of
    finding it -- so handing the window a *new* one would restore the settings
    of one window and leave everything else running on the settings that were
    just thrown away.

    Saving is the caller's: this changes what is in memory, and the caller is
    the one that knows whether that is what the user agreed to.
    """
    factory = shipped()
    if factory is None:
        return check().summary()
    for field in fields(Settings):
        if field.name not in KEPT:
            setattr(current, field.name, getattr(factory, field.name))
    return ""


def reinstall_command() -> str:
    """What to run when the shipped settings are damaged beyond restoring."""
    from git_assistant.updating import PACKAGE_ID

    return f"winget install --id {PACKAGE_ID} --exact --force"
