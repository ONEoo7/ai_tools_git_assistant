"""Which rules a repository is reviewed against, per language and version.

A profile is a set of pointers: for each language, which version it is written
in and which rule sets apply to it. The rules themselves stay where they are --
the shipped ones in ``builtin``, the user's own in ``rules.RuleStore`` -- so a
profile is a few hundred bytes and can live in the settings file beside the
prompt templates.

Three things a profile has to get right:

- **``*`` is a language.** It means "whatever the file is", and it is both what
  one table covering a whole repository looks like and what catches a language
  this build has never heard of.
- **Selections exclude, they do not include.** A rule added to a shipped table
  is then checked by everyone who had not deliberately turned it off, and a
  shared profile does not have to list every rule it wants.
- **Nothing is resolved twice.** ``rules_for`` is asked once per language and
  version in a run, and what it returns is what every file of that language is
  judged by.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from git_assistant.review import builtin, languages, rule_files
from git_assistant.review.rules import Rule, RuleStore, RuleTable, normalize_id

#: The profile every repository starts with: the shipped rules for whatever
#: language each file turns out to be.
DEFAULTS_NAME = "Default Rules"

#: What this profile used to be called. A repository assigned the old name must
#: still resolve: the profile is generated rather than stored, so a pointer at a
#: name nothing generates any more finds nothing and the repository silently
#: loses its rules.
LEGACY_DEFAULTS_NAMES = ("Built-in defaults",)

#: How a selection names a shipped table and one of the user's own.
BUILTIN = "builtin:"
TABLE = "table:"


def canonical_name(name: str) -> str:
    """The name a profile is known by now, given one it may have had before."""
    return DEFAULTS_NAME if name in LEGACY_DEFAULTS_NAMES else name


@dataclass
class Selection:
    """One rule set a language is checked against, minus what was turned off."""

    ref: str  # "builtin:python" or "table:House rules"
    exclude: list[str] = field(default_factory=list)

    @property
    def is_builtin(self) -> bool:
        return self.ref.startswith(BUILTIN)

    @property
    def target(self) -> str:
        """The language or table name this points at."""
        return self.ref.split(":", 1)[1] if ":" in self.ref else self.ref

    def keeps(self, rule_id: str) -> bool:
        wanted = normalize_id(rule_id)
        return all(normalize_id(x) != wanted for x in self.exclude)


@dataclass
class LanguageRules:
    """What one language is checked against, and which version it is."""

    language: str  # a language id, or languages.ANY
    version: str = ""
    selections: list[Selection] = field(default_factory=list)

    def label(self) -> str:
        return languages.label_of(self.language)


@dataclass
class Profile:
    """A named set of per-language rules, assigned to a repository."""

    name: str
    languages: list[LanguageRules] = field(default_factory=list)
    #: Settled answers for ambiguous extensions: ``{".h": "cpp"}``. Asked once,
    #: not once per review.
    overrides: dict[str, str] = field(default_factory=dict)
    #: "" for one of the user's own, "repository" for one a clone brought with
    #: it. A repository's profile is never written back into the user's.
    source: str = ""

    def entry_for(self, language: str) -> LanguageRules | None:
        """The rules for a language, falling back to the ``*`` entry."""
        for entry in self.languages:
            if entry.language == language:
                return entry
        for entry in self.languages:
            if entry.language == languages.ANY:
                return entry
        return None

    def version_for(self, language: str) -> str:
        entry = self.entry_for(language)
        return entry.version if entry and entry.language == language else ""

    def covers(self, language: str) -> bool:
        return self.entry_for(language) is not None

    def covers_exactly(self, language: str) -> bool:
        """Whether this language has an entry of its own, not just the ``*`` one."""
        return any(entry.language == language for entry in self.languages)

    def from_repository(self) -> bool:
        return self.source == "repository"

    def display(self) -> str:
        return f"{self.name} (from the repository)" if self.from_repository() else self.name

    # ---- storage ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "overrides": dict(self.overrides),
            "languages": [
                {
                    "language": entry.language,
                    "version": entry.version,
                    "selections": [
                        {"ref": s.ref, "exclude": list(s.exclude)} for s in entry.selections
                    ],
                }
                for entry in self.languages
            ],
        }

    @classmethod
    def from_dict(cls, data: object, source: str = "") -> "Profile | None":
        """Rebuild a profile, ignoring anything unusable. Never raises."""
        if not isinstance(data, dict) or not str(data.get("name", "")).strip():
            return None
        entries: list[LanguageRules] = []
        for raw in data.get("languages", []):
            if not isinstance(raw, dict) or not str(raw.get("language", "")).strip():
                continue
            entries.append(
                LanguageRules(
                    language=str(raw["language"]).strip(),
                    version=str(raw.get("version", "")).strip(),
                    selections=[
                        Selection(
                            ref=str(s.get("ref", "")).strip(),
                            exclude=[str(x) for x in s.get("exclude", []) if str(x).strip()],
                        )
                        for s in raw.get("selections", [])
                        if isinstance(s, dict) and str(s.get("ref", "")).strip()
                    ],
                )
            )
        overrides = data.get("overrides")
        return cls(
            name=str(data["name"]).strip(),
            languages=entries,
            overrides={
                str(k): str(v) for k, v in overrides.items() if str(v).strip()
            }
            if isinstance(overrides, dict)
            else {},
            source=source,
        )


# ---- the profiles every install has ---------------------------------------------
def defaults() -> Profile:
    """The shipped rules, one entry per language they cover."""
    return Profile(
        name=DEFAULTS_NAME,
        languages=[
            LanguageRules(language=language, selections=[Selection(builtin.ref_of(language))])
            for language in builtin.languages_covered()
        ],
    )


def imported(table_name: str) -> Profile:
    """The profile a repository that had one table gets.

    One entry for ``*``: the same rules for every file, which is what a review
    did before profiles existed. Nothing is added to it -- a review that
    suddenly reports thirty new findings is indistinguishable from a regression
    in the code.
    """
    return Profile(
        name=f"{table_name} (imported)",
        languages=[LanguageRules(language=languages.ANY, selections=[Selection(TABLE + table_name)])],
    )


# ---- turning a profile into rules --------------------------------------------------
def resolve(
    profile: Profile,
    store: RuleStore,
    language: str,
    version: str,
    *,
    inlined: dict[str, RuleTable] | None = None,
) -> RuleTable | None:
    """The rules one language at one version is judged by, or ``None``.

    ``inlined`` are tables that came with a shared profile rather than from the
    local library; they are looked at first, so a clone is reviewed against what
    the repository shipped and never against a local table that happens to share
    a name.
    """
    entry = profile.entry_for(language)
    if entry is None:
        return None

    merged: list[Rule] = []
    seen: set[str] = set()
    names: list[str] = []
    for selection in entry.selections:
        table = _table_of(selection, store, language, version, inlined or {})
        if table is None:
            continue
        kept = [r for r in table.rules if selection.keeps(r.rule_id)]
        if not kept:
            continue
        names.append(table.name)
        for rule in kept:
            key = normalize_id(rule.rule_id)
            if key in seen:  # first wins: an id means one rule, whoever wrote it
                continue
            seen.add(key)
            merged.append(rule)
    if not merged:
        return None
    return RuleTable(name=_name_of(names), rules=merged, source=profile.name)


def _table_of(
    selection: Selection,
    store: RuleStore,
    language: str,
    version: str,
    inlined: dict[str, RuleTable],
) -> RuleTable | None:
    if selection.ref in inlined:
        return inlined[selection.ref]
    if selection.is_builtin:
        # `builtin:*` means "whichever language this file turned out to be".
        wanted = selection.target
        # The files, not the shipped rules: `builtin:` names *which* table,
        # and what that table says is whatever the user's file for it says.
        return rule_files.table_for(
            language if wanted == languages.ANY else wanted, version
        )
    return store.find(selection.target)


def _name_of(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return " + ".join(names) if len(names) <= 3 else f"{len(names)} rule sets"


def rules_for(
    profile: Profile, store: RuleStore, *, inlined: dict[str, RuleTable] | None = None
):
    """A ``plan.RulesFor`` bound to this profile and library."""

    def lookup(language: str, version: str) -> RuleTable | None:
        return resolve(profile, store, language, version, inlined=inlined)

    return lookup


def versions_of(profile: Profile) -> dict[str, str]:
    """The versions the profile pins, for the languages it pins one for."""
    return {
        entry.language: entry.version
        for entry in profile.languages
        if entry.version and entry.language != languages.ANY
    }


# ---- keeping the pointers honest -----------------------------------------------------
def rename_table(profiles: list[Profile], old: str, new: str) -> None:
    """Repoint every selection naming a table that has been renamed.

    Without this a rename leaves a profile pointing at a table that no longer
    exists, and its next review runs against fewer rules -- which looks exactly
    like a clean review.
    """
    for profile in profiles:
        for entry in profile.languages:
            for selection in entry.selections:
                if not selection.is_builtin and selection.target == old:
                    selection.ref = TABLE + new


def remove_table(profiles: list[Profile], name: str) -> None:
    """Drop every selection naming a deleted table."""
    for profile in profiles:
        for entry in profile.languages:
            entry.selections = [
                s for s in entry.selections if s.is_builtin or s.target != name
            ]
