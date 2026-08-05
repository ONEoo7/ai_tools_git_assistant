"""Code-review rule tables, stored beside ``settings.json``.

A rule table is what a team already has: a spreadsheet with a ``ruleID`` column
and a ``ruleDetails`` column. Several can be kept -- one per language, per
project, per standard -- and each repository picks the one it is reviewed
against (``RepoEntry.review_rules``).

    <config dir>/code_review_rules.json

Its own file, following ``committer_identities.json``: a rule set runs to tens
of kilobytes, and ``Settings.save()`` rewrites the whole settings file on every
debounced keystroke in the window. The envelope and the never-raise reader come
from that module; the atomic write comes from ``agents.history``, because losing
an imported table to a half-written file is not an acceptable failure.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant.config import APP_NAME

RULES_FILE = "code_review_rules.json"

#: Bumped only for a change the reader cannot handle silently. Absent from a
#: hand-written file; treated as version 1.
SCHEMA_VERSION = 1


@dataclass
class Rule:
    """One rule: the id the model must quote back, and what it means."""

    rule_id: str
    details: str

    def line(self) -> str:
        """How the rule is written into a prompt."""
        return f"{self.rule_id}: {self.details}"


@dataclass
class RuleTable:
    """A named set of rules, and where it came from."""

    name: str
    rules: list[Rule] = field(default_factory=list)
    source: str = ""  # the file it was imported from, for the tooltip
    imported_at: str = ""  # ISO-8601 UTC

    def __len__(self) -> int:
        return len(self.rules)

    def find(self, rule_id: str) -> Rule | None:
        """Match a rule id the way a model would spell it back.

        Case, spaces, dashes and underscores are all things a model rewrites
        without meaning to; ``R-12``, ``r 12`` and ``R_12`` are one rule.
        """
        needle = normalize_id(rule_id)
        if not needle:
            return None
        return next((r for r in self.rules if normalize_id(r.rule_id) == needle), None)

    def fingerprint(self) -> str:
        """Identity of the *content*, so a stored review can notice a rewrite.

        A review is only meaningful against the rules it actually ran with. The
        name is not enough: a table edited after the fact keeps its name.
        """
        blob = "\n".join(f"{r.rule_id}\x1f{r.details}" for r in self.rules)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def normalize_id(rule_id: str) -> str:
    return "".join(c for c in (rule_id or "").lower() if c.isalnum())


def rules_path() -> Path:
    """Path to the rules file (the directory may not yet exist)."""
    return Path(user_config_dir(APP_NAME, appauthor=False)) / RULES_FILE


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- reading ------------------------------------------------------------------
def parse(data: object) -> list[RuleTable]:
    """Read tables from decoded JSON, ignoring anything unusable.

    Accepts the envelope this module writes and a bare list, so a hand-written
    or third-party file still imports.
    """
    if isinstance(data, dict):
        raw = data.get("tables")
    elif isinstance(data, list):
        raw = data
    else:
        return []
    if not isinstance(raw, list):
        return []

    tables: list[RuleTable] = []
    for entry in raw:
        if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
            continue
        rules = [
            Rule(rule_id=str(r.get("rule_id", "")).strip(), details=str(r.get("details", "")).strip())
            for r in entry.get("rules", [])
            if isinstance(r, dict) and str(r.get("rule_id", "")).strip()
        ]
        tables.append(
            RuleTable(
                name=str(entry["name"]).strip(),
                rules=rules,
                source=str(entry.get("source", "")),
                imported_at=str(entry.get("imported_at", "")),
            )
        )
    return _dedupe(tables)


def _dedupe(tables: list[RuleTable]) -> list[RuleTable]:
    """First spelling of a name wins, order preserved."""
    seen: set[str] = set()
    out: list[RuleTable] = []
    for table in tables:
        key = table.name.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(table)
    return out


def read_file(path: Path) -> list[RuleTable]:
    """Load tables from any path. Returns [] for a missing or broken file."""
    try:
        return parse(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return []


# ---- writing ------------------------------------------------------------------
def _write(path: Path, tables: list[RuleTable]) -> None:
    """Replaced, never truncated: an interrupted write must not eat the tables."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SCHEMA_VERSION,
        "tables": [asdict(t) for t in tables],
    }
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class RuleStore:
    """Every rule table the user has, loaded once and shared by the tab."""

    def __init__(self, tables: list[RuleTable] | None = None) -> None:
        self.tables: list[RuleTable] = list(tables or [])

    @classmethod
    def load(cls) -> "RuleStore":
        return cls(read_file(rules_path()))

    # ---- reading -----------------------------------------------------------
    def names(self) -> list[str]:
        return [t.name for t in self.tables]

    def find(self, name: str) -> RuleTable | None:
        needle = (name or "").strip().lower()
        return next((t for t in self.tables if t.name.lower() == needle), None)

    def unique_name(self, base: str) -> str:
        """``base``, or ``base (2)`` -- never a name already taken."""
        base = (base or "Rules").strip() or "Rules"
        if self.find(base) is None:
            return base
        n = 2
        while self.find(f"{base} ({n})") is not None:
            n += 1
        return f"{base} ({n})"

    # ---- mutation ----------------------------------------------------------
    def save(self) -> None:
        _write(rules_path(), self.tables)

    def add(self, table: RuleTable) -> RuleTable:
        """Store a table under a name nothing else is using."""
        table.name = self.unique_name(table.name)
        table.imported_at = table.imported_at or now_stamp()
        self.tables.append(table)
        self.save()
        return table

    def replace(self, name: str, table: RuleTable) -> RuleTable | None:
        """Overwrite a table's rules in place, keeping its name."""
        existing = self.find(name)
        if existing is None:
            return None
        existing.rules = list(table.rules)
        existing.source = table.source or existing.source
        existing.imported_at = now_stamp()
        self.save()
        return existing

    def rename(self, old: str, new: str) -> bool:
        table = self.find(old)
        new = (new or "").strip()
        if table is None or not new:
            return False
        other = self.find(new)
        if other is not None and other is not table:
            return False
        table.name = new
        self.save()
        return True

    def remove(self, name: str) -> bool:
        table = self.find(name)
        if table is None:
            return False
        self.tables = [t for t in self.tables if t is not table]
        self.save()
        return True

    # ---- transfer ----------------------------------------------------------
    def export_to(self, path: str | Path, name: str = "") -> None:
        """Write every table, or just one, as JSON."""
        chosen = self.tables
        if name:
            table = self.find(name)
            chosen = [table] if table is not None else []
        _write(Path(path), chosen)

    def import_from(self, path: str | Path) -> tuple[int, int]:
        """Merge a JSON file into the store. Returns ``(added, renamed)``.

        Merging rather than replacing, as identities do: an import is how a
        second machine is set up, and replacing would delete tables that only
        exist here. A name already taken is imported under a free one rather
        than overwriting -- an old export must not quietly rewrite the rules a
        repository is being reviewed against.
        """
        incoming = read_file(Path(path))
        renamed = 0
        for table in incoming:
            wanted = table.name
            table.name = self.unique_name(wanted)
            if table.name != wanted:
                renamed += 1
            table.imported_at = table.imported_at or now_stamp()
            self.tables.append(table)
        if incoming:
            self.save()
        return len(incoming), renamed
