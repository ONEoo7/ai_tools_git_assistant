"""The rules that ship with the application, and the versions they apply to.

One table per language, in ``resources/review_rules.json``, read-only. A rule
carries the version range it is true for -- f-strings are not a Python 2 rule,
`nullptr` is not a C++98 one -- and choosing a version filters the table before
it reaches a prompt. A rule quoted against a language version that never had the
feature is a confident, wrong finding, which is worse than no rule at all.

The version machinery stops here on purpose. ``rules.Rule`` does not grow
``since``/``until``: that store is serialised with ``asdict`` and shared with
every user's own tables, which would gain two null fields for a property only
these have.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache

from git_assistant.packaged import data_file
from git_assistant.review import languages
from git_assistant.review.rules import Rule, RuleTable

RULES_FILE = ("resources", "review_rules.json")


@dataclass(frozen=True)
class BuiltinRule:
    """A shipped rule, and the span of language versions it is true for.

    ``since`` and ``until`` are version ids from ``languages``; an empty one
    means "from the beginning" or "to the end".
    """

    rule_id: str
    details: str
    since: str = ""
    until: str = ""

    def applies_to(self, language: languages.Language, version: str) -> bool:
        """Whether this rule is true for ``version`` of ``language``.

        An unknown or unset version keeps every rule. Filtering against a
        version nobody established would silently drop rules, and a rule that
        was dropped looks exactly like a rule nothing broke.
        """
        at = language.index_of(version)
        if at < 0:
            return True
        if self.since and at < language.index_of(self.since):
            return False
        if self.until and at > language.index_of(self.until):
            return False
        return True

    def as_rule(self) -> Rule:
        return Rule(rule_id=self.rule_id, details=self.details)


@dataclass(frozen=True)
class BuiltinTable:
    """Every shipped rule for one language."""

    language: str
    label: str
    #: Bumped when the rules change, so a shared profile can say which set of
    #: them it was written against.
    schema: int = 1
    rules: tuple[BuiltinRule, ...] = field(default_factory=tuple)

    def name(self) -> str:
        """How this table is named on screen and in a shared profile."""
        return f"{self.label} (built in)"


def _read() -> dict[str, BuiltinTable]:
    path = data_file(*RULES_FILE)
    if path is None:
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        # A build that shipped a broken file still reviews, against the user's
        # own tables; it does not refuse to start.
        return {}
    tables: dict[str, BuiltinTable] = {}
    for language, entry in (data.get("tables") or {}).items():
        if not isinstance(entry, dict):
            continue
        tables[language] = BuiltinTable(
            language=language,
            label=str(entry.get("label", language)),
            schema=int(entry.get("schema", 1) or 1),
            rules=tuple(
                BuiltinRule(
                    rule_id=str(r.get("id", "")).strip(),
                    details=str(r.get("details", "")).strip(),
                    since=str(r.get("since", "")).strip(),
                    until=str(r.get("until", "")).strip(),
                )
                for r in entry.get("rules", [])
                if isinstance(r, dict) and str(r.get("id", "")).strip()
            ),
        )
    return tables


@lru_cache(maxsize=1)
def tables() -> dict[str, BuiltinTable]:
    """Every shipped table, by language id. Read once."""
    return _read()


def get(language: str) -> BuiltinTable | None:
    return tables().get(language)


def languages_covered() -> list[str]:
    return list(tables())


def table_for(language: str, version: str = "") -> RuleTable | None:
    """The shipped rules for a language at a version, as an ordinary table.

    ``None`` when nothing ships for that language. An unknown version keeps
    every rule -- see ``BuiltinRule.applies_to``.
    """
    builtin = tables().get(language)
    lang = languages.get(language)
    if builtin is None or lang is None:
        return None
    kept = [r.as_rule() for r in builtin.rules if r.applies_to(lang, version)]
    return RuleTable(name=builtin.name(), rules=kept, source="built in")


def ref_of(language: str) -> str:
    """How a shared profile names this table: stable, and not a display name."""
    return f"builtin:{language}"


def language_of(ref: str) -> str:
    """The language a ``builtin:`` reference points at, or ""."""
    return ref.split(":", 1)[1] if ref.startswith("builtin:") else ""
