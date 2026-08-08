"""The review rules, as files a person can edit.

    <config dir>/code_review/cpp.json
    <config dir>/code_review/python.json

One file per language, written out from the shipped rules the first time the
application runs and never rewritten over afterwards. Editing one is editing
what the next review checks.

One file per language and not one per language *version*, which was the other
way to do it. A rule carries the span of versions it is true for -- ``since:
c++11`` -- and materialising that into a file per version copies the rule into
five of them, so fixing its wording later means fixing it five times and a
review of C++17 disagreeing with a review of C++20 becomes an ordinary sort of
accident. The span stays; the file is the language.

The shipped rules remain in ``builtin`` and are still the answer when a file is
missing or unreadable. Nothing here can leave a review with no rules: the worst
case is the rules this build shipped with, which is where everybody starts.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant.config import APP_NAME
from git_assistant.review import builtin, languages
from git_assistant.review.rules import RuleTable

SCHEMA_VERSION = 1
RULES_DIR = "code_review"


def rules_dir() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False)) / RULES_DIR


def path_for(language: str) -> Path:
    return rules_dir() / f"{language}.json"


def exists(language: str) -> bool:
    return path_for(language).is_file()


# ---- what a file says --------------------------------------------------------------
def to_dict(table: builtin.BuiltinTable) -> dict:
    """A table as its file holds it. Empty spans are left out, not written null."""
    rules = []
    for rule in table.rules:
        entry = {"id": rule.rule_id, "details": rule.details}
        if rule.since:
            entry["since"] = rule.since
        if rule.until:
            entry["until"] = rule.until
        rules.append(entry)
    return {
        "version": SCHEMA_VERSION,
        "language": table.language,
        "label": table.label,
        "schema": table.schema,
        "rules": rules,
    }


def text_of(table: builtin.BuiltinTable) -> str:
    return json.dumps(to_dict(table), indent=2, ensure_ascii=False) + "\n"


def from_dict(language: str, data: object) -> builtin.BuiltinTable | None:
    """A table from a file's contents, or ``None`` if it is not one.

    Tolerant in the same way every other reader here is: a rule with no id is
    not a rule and is dropped, and a key this build has never heard of is left
    where it is.
    """
    if not isinstance(data, dict):
        return None
    shipped = builtin.get(language)
    rules = []
    for entry in data.get("rules") or []:
        if not isinstance(entry, dict):
            continue
        rule_id = str(entry.get("id", "")).strip()
        if not rule_id:
            continue
        rules.append(
            builtin.BuiltinRule(
                rule_id=rule_id,
                details=str(entry.get("details", "")).strip(),
                since=str(entry.get("since", "")).strip(),
                until=str(entry.get("until", "")).strip(),
            )
        )
    label = str(data.get("label", "")).strip() or (
        shipped.label if shipped else language
    )
    return builtin.BuiltinTable(
        language=language,
        label=label,
        schema=int(data.get("schema", 1) or 1),
        rules=tuple(rules),
    )


def check(text: str) -> str:
    """Why ``text`` is not a rules file, or ``""`` if it is one."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"Not valid JSON: {exc}"
    if not isinstance(data, dict):
        return "A rules file holds an object, not a list."
    if not isinstance(data.get("rules", []), list):
        return "`rules` holds a list of rules."
    return ""


# ---- reading -------------------------------------------------------------------------
def read_text(language: str) -> str:
    try:
        return path_for(language).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _read(language: str) -> tuple[builtin.BuiltinTable | None, str]:
    """``(table, problem)``. ``(None, "")`` when there is simply no file."""
    path = path_for(language)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ""
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{path.name} could not be read: {exc}"
    problem = check(text)
    if problem:
        return None, f"{path.name}: {problem}"
    return from_dict(language, json.loads(text)), ""


def problem_with(language: str) -> str:
    """Why this language's file is being ignored, or ``""``."""
    return _read(language)[1]


def table(language: str) -> builtin.BuiltinTable | None:
    """The rules in force for a language: the user's file, else the shipped ones.

    Read every time. A review asks once per language, and a file edited between
    two reviews has to be the one the second review uses.
    """
    found, problem = _read(language)
    if found is not None and not problem:
        return found
    return builtin.get(language)


def tables() -> dict[str, builtin.BuiltinTable]:
    found = {}
    for language in builtin.languages_covered():
        entry = table(language)
        if entry is not None:
            found[language] = entry
    return found


def languages_covered() -> list[str]:
    return list(tables())


def table_for(language: str, version: str = "") -> RuleTable | None:
    """The rules in force for a language at a version, as an ordinary table.

    The same shape ``builtin.table_for`` returns, and the same filtering by
    version -- the spans are why the file is per language.
    """
    entry = table(language)
    lang = languages.get(language)
    if entry is None or lang is None:
        return None
    kept = [r.as_rule() for r in entry.rules if r.applies_to(lang, version)]
    source = path_for(language).name if exists(language) else "built in"
    return RuleTable(name=entry.name(), rules=kept, source=source)


# ---- writing -------------------------------------------------------------------------
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_text(language: str, text: str) -> str:
    """Save one language's rules. Returns a problem, or ``""``.

    Refuses what cannot be read back: the run that finds a rules file broken is
    a review that quietly checked nothing.
    """
    problem = check(text)
    if problem:
        return problem
    try:
        _write(path_for(language), text)
    except OSError as exc:
        return str(exc)
    return ""


def restore(language: str) -> str:
    """Put one language's file back to the rules this build ships with."""
    shipped = builtin.get(language)
    if shipped is None:
        return f"Nothing ships for {language}."
    return write_text(language, text_of(shipped))


def ensure_files() -> list[str]:
    """Write out any language that has no file yet. Returns what was written.

    Never over an existing file: these are meant to be edited, and a build that
    rewrote them on start-up would be a build that ate the edit.
    """
    written = []
    for language in builtin.languages_covered():
        if exists(language):
            continue
        if restore(language) == "":
            written.append(language)
    return written
