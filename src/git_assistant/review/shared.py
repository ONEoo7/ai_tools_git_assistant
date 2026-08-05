"""The review profile a repository carries, for whoever clones it.

    <repo>/.git-assistant/code-review-profile.json

This is the only file this application ever writes into a working tree, and it
is written on one explicit press. Everything else it keeps lives in the user's
config directory, filed under a hash of the repository path -- see
``config.repo_key`` -- precisely so that cloning a repository does not import
somebody else's settings. A shared review profile is the exception because it is
the point: a colleague should be reviewed against the standard the project holds
to, not against whatever they happen to have locally.

So it is self-contained. Custom tables are written out in full, with their
fingerprint; the shipped ones are named by id and schema, because the
application already has those. And it is read defensively: it arrives from a
clone, which is to say from somebody else.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from git_assistant.review import builtin
from git_assistant.review.profiles import Profile, Selection
from git_assistant.review.rules import Rule, RuleStore, RuleTable

FOLDER = ".git-assistant"
PROFILE_FILE = "code-review-profile.json"
SCHEMA_VERSION = 1

#: Caps on what a clone may hand over. `rules.parse` has none, and it never
#: needed any: this is the one input that comes from somebody else.
MAX_TABLES = 40
MAX_RULES = 500
MAX_DETAILS = 4_000


def profile_path(repo: str) -> Path:
    return Path(repo) / FOLDER / PROFILE_FILE


def exists(repo: str) -> bool:
    return bool(repo) and profile_path(repo).is_file()


# ---- writing ---------------------------------------------------------------------
def to_document(profile: Profile, store: RuleStore) -> dict:
    """The profile, plus every custom table it names, in full."""
    tables = []
    for entry in profile.languages:
        for selection in entry.selections:
            if selection.is_builtin:
                continue
            table = store.find(selection.target)
            if table is None or any(t["ref"] == selection.ref for t in tables):
                continue
            tables.append(
                {
                    "ref": selection.ref,
                    "name": table.name,
                    "fingerprint": table.fingerprint(),
                    "rules": [
                        {"rule_id": r.rule_id, "details": r.details} for r in table.rules
                    ],
                }
            )
    document = profile.to_dict()
    document.update(
        {
            "version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tables": tables,
            # Which set of shipped rules this was written against, so a build
            # with a different one can say so rather than quietly differ.
            "builtin_schema": {
                language: table.schema for language, table in builtin.tables().items()
            },
        }
    )
    return document


def write(repo: str, profile: Profile, store: RuleStore) -> Path:
    """Write the profile into the repository. Raises OSError if it cannot.

    Replaced atomically and formatted the same way every time -- LF, two-space
    indent, no ASCII escaping -- so a Windows collaborator does not get a
    whole-file diff for a one-line change.
    """
    path = profile_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(to_document(profile, store), indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)
    return path


# ---- reading -----------------------------------------------------------------------
def read(repo: str) -> tuple[Profile | None, dict[str, RuleTable]]:
    """The repository's profile and the tables it brought, or ``(None, {})``.

    Never raises. A file that will not parse reads as absent: refusing to open
    the tab because somebody committed broken JSON would be worse than
    reviewing against the local profile and saying so.
    """
    if not exists(repo):
        return None, {}
    try:
        data = json.loads(profile_path(repo).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return None, {}

    profile = Profile.from_dict(data, source="repository")
    if profile is None:
        return None, {}
    return profile, _tables_from(data)


def _tables_from(data: object) -> dict[str, RuleTable]:
    if not isinstance(data, dict):
        return {}
    out: dict[str, RuleTable] = {}
    for entry in (data.get("tables") or [])[:MAX_TABLES]:
        if not isinstance(entry, dict) or not str(entry.get("ref", "")).strip():
            continue
        rules = [
            Rule(
                rule_id=str(r.get("rule_id", "")).strip(),
                details=str(r.get("details", "")).strip()[:MAX_DETAILS],
            )
            for r in (entry.get("rules") or [])[:MAX_RULES]
            if isinstance(r, dict) and str(r.get("rule_id", "")).strip()
        ]
        if not rules:
            continue
        out[str(entry["ref"])] = RuleTable(
            name=str(entry.get("name", "")) or str(entry["ref"]),
            rules=rules,
            source="the repository",
        )
    return out


def fingerprint(repo: str) -> str:
    """Identity of the repository's file as it stands, or "".

    Used to ask about a disagreement once per change rather than once per
    launch.
    """
    if not exists(repo):
        return ""
    try:
        import hashlib

        return hashlib.sha256(profile_path(repo).read_bytes()).hexdigest()[:16]
    except OSError:
        return ""


def copy_into(store: RuleStore, tables: dict[str, RuleTable]) -> list[str]:
    """Add a repository's tables to the user's own library, on request.

    Only ever on request: a ``git pull`` must not quietly add tables to
    somebody's library. Names already taken are kept under a free one, as
    ``RuleStore.add`` does, so nothing local is overwritten.
    """
    added = []
    for table in tables.values():
        stored = store.add(RuleTable(name=table.name, rules=list(table.rules), source="the repository"))
        added.append(stored.name)
    return added


def selections_of(profile: Profile) -> list[Selection]:
    return [s for entry in profile.languages for s in entry.selections]
