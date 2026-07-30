"""Committer identities, stored in their own file next to ``settings.json``.

Kept separate from the settings on purpose: identities are the one piece of
configuration a user has a reason to move between machines by hand, and a file
that holds nothing else can be exported and imported without dragging along an
LM Studio address or a list of local repository paths.

What is *not* stored here is which identity a given repository uses. That lives
in the repository's own git config, because git is what decides the answer.
Recording it here as well would create a second answer, free to disagree with
the one that actually stamps the commit.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant import git_ops
from git_assistant.config import APP_NAME, config_path

IDENTITIES_FILE = "committer_identities.json"

#: Bumped only for a change the reader cannot handle silently. Absent in files
#: written by hand or exported from a future version; treated as version 1.
SCHEMA_VERSION = 1

#: Characters that must never reach ``git config``. A newline in a value is
#: written into ``.git/config`` escaped rather than executed -- arguments are
#: passed as a list, so there is no shell to inject into -- but it still
#: produces an identity nobody intended, from a file that may have come from
#: somewhere else.
_FORBIDDEN = set("\n\r\t\0")


@dataclass
class Identity:
    """A committer identity: what git stamps on a commit, and what signs it.

    ``signingkey`` is optional and travels with the identity on purpose. A key
    belongs to one identity; leaving it behind when the email changes is how a
    commit ends up signed by a key that does not match its author, which every
    forge reports as unverified.
    """

    name: str
    email: str
    signingkey: str = ""

    def display(self) -> str:
        return self.email or self.name

    def describe(self) -> str:
        who = f"{self.name} <{self.email}>" if self.name else self.email
        return f"{who}, signed with {self.signingkey}" if self.signingkey else who


def identities_path() -> Path:
    """Path to the identities file (the directory may not yet exist)."""
    return Path(user_config_dir(APP_NAME, appauthor=False)) / IDENTITIES_FILE


def is_valid(identity: Identity) -> bool:
    """Reject anything that would make a nonsensical or unsafe git identity.

    An email is required (a commit cannot be made without one) and must look
    like an address. Control characters are refused in both fields; see
    ``_FORBIDDEN``.
    """
    if not identity.email or "@" not in identity.email:
        return False
    fields = (identity.name, identity.email, identity.signingkey)
    return not any(c in _FORBIDDEN for value in fields for c in value)


def dedupe(identities: list[Identity]) -> list[Identity]:
    """Drop repeats, keeping the first of each email and the original order.

    Matched case-insensitively. Git itself compares the string literally, so
    two entries differing only in case are two identities to git -- but to a
    person they are one, and offering both in a dropdown is a way to pick the
    wrong one. The stored spelling is left untouched.
    """
    seen: set[str] = set()
    out: list[Identity] = []
    for ident in identities:
        key = ident.email.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(ident)
    return out


def parse(data: object) -> list[Identity]:
    """Read identities from decoded JSON, ignoring anything unusable.

    Accepts both the envelope this module writes and a bare list, so a
    hand-written or third-party file still imports.
    """
    if isinstance(data, dict):
        raw = data.get("identities")
    elif isinstance(data, list):
        raw = data
    else:
        return []
    if not isinstance(raw, list):
        return []

    found = [
        Identity(
            name=str(d.get("name", "")).strip(),
            email=str(d.get("email", "")).strip(),
            # Absent in files written before signing keys were stored.
            signingkey=str(d.get("signingkey", "")).strip(),
        )
        for d in raw
        if isinstance(d, dict)
    ]
    return dedupe([i for i in found if is_valid(i)])


def _write(path: Path, identities: list[Identity]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": SCHEMA_VERSION, "identities": [asdict(i) for i in identities]},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def read_file(path: Path) -> list[Identity]:
    """Load identities from any path. Returns [] for a missing or broken file."""
    try:
        return parse(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []


# ---- first run --------------------------------------------------------------
def _from_settings() -> list[Identity]:
    """Recover identities from an older build that kept them in settings.json.

    The key is then removed, so the file that no longer owns this data does not
    keep a stale copy of it to disagree with later.
    """
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []
    if not isinstance(data, dict) or "identities" not in data:
        return []

    found = parse(data.get("identities"))
    try:
        data.pop("identities", None)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass  # migration still succeeded; the stale key is only untidy
    return found


def _from_git() -> list[Identity]:
    name, email = git_ops.get_global_identity()
    ident = Identity(
        name=name, email=email, signingkey=git_ops.get_global_signingkey()
    )
    return [ident] if is_valid(ident) else []


class IdentityStore:
    """The set of identities, loaded once and shared by the tab and the picker."""

    def __init__(self, identities: list[Identity] | None = None) -> None:
        self.identities: list[Identity] = list(identities or [])

    @classmethod
    def bootstrap(cls) -> "IdentityStore":
        """Load the file, creating it from what git already knows on first run.

        The file is written even when nothing was found, so "first run" happens
        once. Otherwise a user who deliberately emptied the list would have it
        repopulated from their global git config on every start.
        """
        path = identities_path()
        if path.exists():
            return cls(read_file(path))
        store = cls(_from_settings() or _from_git())
        store.save()
        return store

    # ---- mutation ----------------------------------------------------------
    def save(self) -> None:
        _write(identities_path(), self.identities)

    def replace(self, identities: list[Identity]) -> None:
        self.identities = dedupe([i for i in identities if is_valid(i)])
        self.save()

    def find(self, email: str) -> Identity | None:
        needle = (email or "").strip().lower()
        return next((i for i in self.identities if i.email.lower() == needle), None)

    # ---- transfer ----------------------------------------------------------
    def export_to(self, path: str | Path) -> None:
        _write(Path(path), self.identities)

    def import_from(self, path: str | Path) -> tuple[int, int]:
        """Merge a file into the store. Returns ``(added, skipped)``.

        Merging rather than replacing: import is how a second machine is set
        up, and replacing would delete identities that only exist here. An
        email already present is left alone rather than overwritten, so an old
        export cannot quietly rename the identity in use.
        """
        incoming = read_file(Path(path))
        added = [i for i in incoming if self.find(i.email) is None]
        self.identities.extend(added)
        self.save()
        return len(added), len(incoming) - len(added)
