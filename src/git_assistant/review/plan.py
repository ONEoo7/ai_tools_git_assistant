"""What a review will do, decided before any of it is done.

One object answers every question asked before a run starts -- which files, in
which language, at which version, against which rules -- and the same object is
what the run then executes. The window that asks for confirmation, the token
estimate and the reviewer all read it, so the window cannot list a file the run
then skips, and the estimate cannot price rules the run does not send.

Building it costs one ``git diff`` and one read per file, and nothing else: no
provider, no network. That is what lets the confirmation window appear when the
button is pressed.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field

from git_assistant import git_ops
from git_assistant.config import Settings
from git_assistant.review import languages
from git_assistant.review.reviewer import Candidate, staged_files
from git_assistant.review.rules import RuleTable

#: How much of a file is read to settle an ambiguous extension. The markers that
#: decide it -- includes, a namespace, a shebang -- are at the top or nowhere.
HEAD_LINES = 40


@dataclass
class FilePlan:
    """One file, and everything decided about it before the call is made."""

    candidate: Candidate
    language: str = languages.UNKNOWN
    version: str = ""
    table: RuleTable | None = None
    #: Why this file will not be reviewed, if it will not be.
    skipped: str = ""

    @property
    def path(self) -> str:
        return self.candidate.path

    @property
    def reviewable(self) -> bool:
        return not self.skipped and self.table is not None and bool(self.table.rules)

    def language_label(self) -> str:
        return languages.label_of(self.language) if self.language else "Unknown"

    def version_label(self) -> str:
        lang = languages.get(self.language)
        return lang.label_for(self.version) if lang else ""

    def rules_label(self) -> str:
        """What this file will be checked against, for a row in the window."""
        if self.skipped:
            return self.skipped
        if self.table is None or not self.table.rules:
            return "no rules"
        where = self.table.name
        return f"{len(self.table.rules)} rule(s) - {where}"


@dataclass
class ReviewPlan:
    """Every file a run will touch, reviewable or not."""

    repo: str
    profile: str = ""
    files: list[FilePlan] = field(default_factory=list)

    def reviewable(self) -> list[FilePlan]:
        return [f for f in self.files if f.reviewable]

    def skipped(self) -> list[FilePlan]:
        return [f for f in self.files if not f.reviewable]

    def languages(self) -> list[str]:
        """The languages actually involved, most files first."""
        counts: dict[str, int] = {}
        for file in self.reviewable():
            counts[file.language] = counts.get(file.language, 0) + 1
        return [lang for lang, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    def tables(self) -> list[str]:
        """The rule tables in use, in the order they first appear."""
        names: list[str] = []
        for file in self.reviewable():
            if file.table is not None and file.table.name not in names:
                names.append(file.table.name)
        return names

    def fingerprint(self) -> str:
        """Identity of everything this plan will send.

        A stored review can then notice that the rules were rewritten under it,
        which one table's fingerprint could not answer once a run spans several.
        """
        blob = "\n".join(
            f"{f.path}\x1f{f.language}\x1f{f.version}\x1f"
            f"{f.table.fingerprint() if f.table else ''}"
            for f in self.reviewable()
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    @classmethod
    def of_table(
        cls, repo: str, candidates: list[Candidate], table: RuleTable
    ) -> "ReviewPlan":
        """One table for every file, which is what a review was before profiles.

        Kept as the plain way to say "these rules, these files" -- the migration
        path for a repository that has one table and no profile.
        """
        return cls(
            repo=repo,
            profile=table.name,
            files=[
                FilePlan(
                    candidate=c,
                    language=languages.ANY,
                    table=table,
                    skipped="" if c.reviewable else "filtered as noise",
                )
                for c in candidates
            ],
        )


#: Given a language and a version, the rules to check. Supplied by whatever
#: knows: a profile, or a caller with one table.
RulesFor = Callable[[str, str], RuleTable | None]


def build(
    settings: Settings,
    repo: str,
    paths: list[str],
    rules_for: RulesFor,
    *,
    versions: dict[str, str] | None = None,
    overrides: dict[str, str] | None = None,
    profile: str = "",
    candidates: list[Candidate] | None = None,
) -> ReviewPlan:
    """Work out what a review of ``paths`` would do.

    ``rules_for(language, version)`` is asked once per language present.
    ``versions`` is what has already been established per language, and
    ``overrides`` settles ambiguous extensions (``{".h": "cpp"}``).
    """
    found = candidates
    if found is None:
        found = staged_files(repo, settings.diff_mode, settings.ignore_globs)
    wanted = set(paths)
    hint = languages.hint_from([c.path for c in found])
    versions = versions or {}

    tables: dict[tuple[str, str], RuleTable | None] = {}
    plan = ReviewPlan(repo=repo, profile=profile)
    for candidate in found:
        if candidate.path not in wanted:
            continue
        if not candidate.reviewable:
            plan.files.append(
                FilePlan(candidate=candidate, skipped="filtered as noise")
            )
            continue

        head = _head(candidate.diff)
        language = languages.detect(
            candidate.path, head=head, repo_hint=hint, overrides=overrides
        )
        if not language:
            plan.files.append(
                FilePlan(
                    candidate=candidate,
                    skipped="no language could be identified for this file",
                )
            )
            continue

        version = versions.get(language, "")
        key = (language, version)
        if key not in tables:
            tables[key] = rules_for(language, version)
        table = tables[key]
        plan.files.append(
            FilePlan(
                candidate=candidate,
                language=language,
                version=version,
                table=table,
                skipped=""
                if table is not None and table.rules
                else f"no rules apply to {languages.label_of(language)}",
            )
        )
    return plan


def _head(diff: str) -> str:
    """The first added lines of a file's diff, as the file itself reads.

    Taken from the diff already in memory rather than from a second `git show`:
    settling `.h` must not cost a subprocess per header.
    """
    lines = []
    for line in diff.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            lines.append(line[1:])
        elif line.startswith(" "):
            lines.append(line[1:])
        if len(lines) >= HEAD_LINES:
            break
    return "\n".join(lines)


def repo_versions(repo: str, plan_languages: list[str]) -> dict[str, str]:
    """What the repository declares, for the languages that are actually in it."""
    from git_assistant.review import versions as version_detection

    found = version_detection.detect(repo, wanted=plan_languages)
    return {language: found[language].version for language in found}


def head_of(repo: str, path: str, mode: str) -> str:
    """The first lines of a file, for a version a file states about itself."""
    return "\n".join(git_ops.file_content(repo, path, mode).splitlines()[:HEAD_LINES])
