"""The files a repository usually starts with: README.md, .gitignore, .gitattributes
and a LICENSE.

What each one says is decided here, and so is how it meets a repository that
already has one. Nothing is overwritten that the caller did not ask to replace:

- README.md is created only where there is no README of any kind.
- LICENSE is replaced only when the caller says so, having asked.
- .gitignore gets one marked section per template it does not have yet,
  appended; running this twice adds nothing the second time.
- .gitattributes is merged rule by rule. The rule for every file goes *first*:
  git lets a later line override an earlier one, so a ``*`` rule appended at the
  end would undo every specific rule above it. A pattern the file already
  decides differently is left alone and reported.

The templates ship with the application (``resources/starter_templates.json``,
refreshed by ``tools/update_starter_templates.py``); nothing is downloaded.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Literal

from git_assistant import git_ops
from git_assistant.packaged import data_file
from git_assistant.review import languages

TEMPLATES_FILE = ("resources", "starter_templates.json")

README = "README.md"
GITIGNORE = ".gitignore"
GITATTRIBUTES = ".gitattributes"
LICENSE = "LICENSE"

#: The github/gitignore template for each language Code Review knows. It is the
#: same list, so a language added there needs a decision here (a test holds
#: that). None where the collection has no template: CSS, HTML and shell and
#: PowerShell scripts leave nothing behind that needs ignoring.
GITIGNORE_TEMPLATE: dict[str, str | None] = {
    "c": "C",
    "cpp": "C++",
    "csharp": "VisualStudio",
    "css": None,
    "html": None,
    "java": "Java",
    "javascript": "Node",
    "python": "Python",
    "rust": "Rust",
    "typescript": "Node",
    "bash": None,
    "powershell": None,
}

LICENSES = ("MIT", "Apache-2.0")
LICENSE_LABELS = {"MIT": "MIT", "Apache-2.0": "Apache 2.0"}


# ---- the shipped templates -----------------------------------------------------------
@dataclass(frozen=True)
class Bundled:
    """The templates this build ships, and the commit they were taken from."""

    gitignore_commit: str
    templates: dict[str, str]
    licenses: dict[str, str]


@lru_cache(maxsize=1)
def bundled() -> Bundled | None:
    """The shipped templates, or None when this build has lost them."""
    path = data_file(*TEMPLATES_FILE)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        ignore = data["gitignore"]
        return Bundled(
            gitignore_commit=str(ignore["commit"]),
            templates=dict(ignore["templates"]),
            licenses=dict(data["licenses"]["texts"]),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


# ---- .gitignore ----------------------------------------------------------------------
_BEGIN = "# >>> git-assistant: "
_END = "# <<< git-assistant: "


@dataclass(frozen=True)
class IgnoreSection:
    """One template's rules, and the languages that wanted it."""

    template: str
    languages: tuple[str, ...]
    rules: str

    def text(self, commit: str) -> str:
        return (
            f"{_BEGIN}{self.template} ({', '.join(self.languages)}), "
            f"from github/gitignore @{commit[:7]}\n"
            f"{self.rules.rstrip()}\n"
            f"{_END}{self.template}\n"
        )


def has_template(language_id: str) -> bool:
    """Whether choosing this language adds anything to a .gitignore."""
    return bool(GITIGNORE_TEMPLATE.get(language_id))


def ignore_sections(language_ids: Iterable[str]) -> list[IgnoreSection]:
    """A section per template the chosen languages need, in Code Review's order.

    JavaScript and TypeScript both want Node's, which is written once.
    """
    shipped = bundled()
    if shipped is None:
        return []
    chosen = set(language_ids)
    wanted: dict[str, list[str]] = {}
    for language in languages.LANGUAGES:
        template = GITIGNORE_TEMPLATE.get(language.id)
        if language.id in chosen and template in shipped.templates:
            wanted.setdefault(template, []).append(language.label)
    return [
        IgnoreSection(template, tuple(labels), shipped.templates[template])
        for template, labels in wanted.items()
    ]


def _has_section(existing: str, template: str) -> bool:
    return bool(
        re.search(rf"^{re.escape(_BEGIN + template)}(\s|$)", existing, re.MULTILINE)
    )


# ---- .gitattributes ------------------------------------------------------------------
Ending = Literal["lf", "crlf", "native", "binary"]
TextEnding = Literal["lf", "crlf", "native"]

ENDINGS: tuple[Ending, ...] = ("lf", "crlf", "native", "binary")
ENDING_LABELS: dict[str, str] = {
    "lf": "LF",
    "crlf": "CRLF",
    "native": "Native (CRLF on Windows, LF elsewhere)",
    "binary": "Binary (never converted)",
}

_RULE_ATTRIBUTES = {
    "lf": "text eol=lf",
    "crlf": "text eol=crlf",
    "native": "text",
    "binary": "binary",
}
_DEFAULT_ATTRIBUTES = {
    "lf": "text=auto eol=lf",
    "crlf": "text=auto eol=crlf",
    "native": "text=auto",
}

_ATTRIBUTES_HEADER = (
    "# Line endings are decided here, not by each machine's git config.\n"
)


@dataclass(frozen=True)
class EolRule:
    """The line endings files matching ``pattern`` are checked out with."""

    pattern: str
    ending: Ending

    def line(self) -> str:
        return f"{self.pattern} {_RULE_ATTRIBUTES[self.ending]}"


@dataclass(frozen=True)
class Attributes:
    """A .gitattributes: the ending for every text file, then the exceptions."""

    default: TextEnding = "lf"
    rules: tuple[EolRule, ...] = ()

    def default_line(self) -> str:
        return f"* {_DEFAULT_ATTRIBUTES[self.default]}"

    def lines(self) -> list[str]:
        return [self.default_line(), *(rule.line() for rule in self.rules)]


#: Scripts whose interpreter needs one ending whatever the default is -- the ones
#: the Audit tab checks for -- and files that must never be converted. The same
#: choices as this project's own .gitattributes.
DEFAULT_RULES: tuple[EolRule, ...] = (
    EolRule("*.sh", "lf"),
    EolRule("*.bat", "crlf"),
    EolRule("*.cmd", "crlf"),
    EolRule("*.ps1", "crlf"),
    *(
        EolRule(f"*.{extension}", "binary")
        for extension in ("png", "jpg", "gif", "ico", "zip", "exe", "dll")
    ),
)


def pattern_problem(pattern: str) -> str:
    """Why git would not read ``pattern`` the way it looks, or "" if it would."""
    if not pattern.strip():
        return "A pattern cannot be empty."
    if any(character.isspace() for character in pattern):
        return "A pattern cannot contain spaces."
    if pattern.startswith("!"):
        return "Negative patterns are not allowed in .gitattributes."
    if pattern.startswith("#"):
        return "A line starting with # is a comment."
    if pattern.endswith("/"):
        return "A pattern ending in / matches nothing here; use folder/** instead."
    return ""


def gitattributes_text(attributes: Attributes) -> str:
    return _ATTRIBUTES_HEADER + "\n".join(attributes.lines()) + "\n"


def _ending_of(attributes: list[str]) -> str | None:
    """What one line's attributes decide about line endings, if anything."""
    if "binary" in attributes or "-text" in attributes:
        return "binary"
    eol = [value[4:] for value in attributes if value in ("eol=lf", "eol=crlf")]
    if eol:
        return eol[-1]
    if "text" in attributes or "text=auto" in attributes:
        return "native"
    return None


def _rules_in(text: str) -> list[tuple[str, str]]:
    """``(pattern, ending)`` for each line that decides line endings, in order."""
    found = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", "[attr]")):
            continue
        if line.startswith('"'):
            close = line.find('"', 1)
            if close < 0:
                continue
            pattern, rest = line[1:close], line[close + 1 :]
        else:
            pattern, _, rest = line.partition(" ")
            if "\t" in pattern:
                pattern, _, more = pattern.partition("\t")
                rest = f"{more} {rest}"
        ending = _ending_of(rest.split())
        if ending is not None:
            found.append((pattern, ending))
    return found


# ---- LICENSE -------------------------------------------------------------------------
def license_text(kind: str, *, holder: str = "", year: int | str = "") -> str:
    """The license, with the holder and year filled in where it has room for them.

    MIT names its copyright holder at the top. Apache 2.0 is kept word for word:
    its ``[yyyy] [name of copyright owner]`` belongs to the notice it tells you
    to put in each source file, not to the license itself.
    """
    shipped = bundled()
    if shipped is None or kind not in shipped.licenses:
        raise KeyError(kind)
    text = shipped.licenses[kind]
    if kind == "MIT":
        text = text.replace("[year]", str(year)).replace("[fullname]", holder)
    return text


# ---- planning ------------------------------------------------------------------------
Action = Literal["create", "append", "replace", "skip"]


@dataclass(frozen=True)
class Choices:
    """Which starter files to add, and what goes in them."""

    readme: bool = False
    #: Language ids for .gitignore; None to leave .gitignore alone.
    gitignore: tuple[str, ...] | None = None
    attributes: Attributes | None = None
    #: "MIT" or "Apache-2.0"; "" to leave the license alone.
    license: str = ""
    holder: str = ""
    year: int | str = ""


@dataclass(frozen=True)
class Planned:
    """What adding one file would do, before anything is written."""

    #: The file, relative to the repository.
    name: str
    action: Action
    #: One line for the person deciding.
    summary: str
    #: The whole file as it would be written.
    content: bytes = b""
    #: The text being added -- the new file, or what goes into the existing one.
    added: str = ""
    #: What was left as it was, and why.
    kept: tuple[str, ...] = ()
    #: Which starter file this is -- README, GITIGNORE, GITATTRIBUTES or LICENSE
    #: -- whatever the file already there is called (``readme.md``, ``COPYING``).
    kind: str = ""


def plan(repo: str | Path | None, choices: Choices) -> list[Planned]:
    """What adding ``choices`` to ``repo`` would do, file by file.

    ``repo`` None plans for a repository that does not exist yet. The order is
    the order to write in: .gitattributes first, so the files after it are
    written under its rules.
    """
    root = Path(repo) if repo else None
    planned = []
    if choices.attributes is not None:
        item = _plan_attributes(root, choices.attributes)
        planned.append(dataclasses.replace(item, kind=GITATTRIBUTES))
    if choices.gitignore is not None:
        item = _plan_gitignore(root, choices.gitignore)
        planned.append(dataclasses.replace(item, kind=GITIGNORE))
    if choices.license:
        item = _plan_license(root, choices)
        planned.append(dataclasses.replace(item, kind=LICENSE))
    if choices.readme:
        planned.append(dataclasses.replace(_plan_readme(root), kind=README))
    return planned


def _existing(root: Path | None, name: str) -> bytes | None:
    if root is None:
        return None
    try:
        return (root / name).read_bytes()
    except OSError:
        return None


def _find(root: Path | None, pattern: str) -> str:
    """The name of a file at the top of ``root`` matching ``pattern``, or ""."""
    if root is None or not root.is_dir():
        return ""
    names = sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_file() and re.fullmatch(pattern, entry.name, re.IGNORECASE)
    )
    return names[0] if names else ""


def _utf16(data: bytes) -> bool:
    return data.startswith((b"\xff\xfe", b"\xfe\xff"))


def _newline_of(data: bytes) -> bytes:
    """The line ending most of ``data`` already uses."""
    return b"\r\n" if data.count(b"\r\n") * 2 > data.count(b"\n") else b"\n"


def _in_style(text: str, newline: bytes) -> bytes:
    return text.replace("\n", newline.decode()).encode("utf-8")


def _unreadable(name: str) -> Planned:
    return Planned(
        name,
        "skip",
        f"Skipped: {name} is saved as UTF-16, which git cannot read. "
        "Save it as UTF-8 first.",
    )


def _plan_readme(root: Path | None) -> Planned:
    found = _find(root, r"readme(\..+)?")
    if found:
        return Planned(found, "skip", f"Skipped: {found} already exists.")
    return Planned(README, "create", "Creates an empty README.md.")


def _plan_license(root: Path | None, choices: Choices) -> Planned:
    label = LICENSE_LABELS.get(choices.license, choices.license)
    try:
        text = license_text(
            choices.license, holder=choices.holder.strip(), year=choices.year
        )
    except KeyError:
        return Planned(LICENSE, "skip", f"Skipped: this build has no {label} text.")
    if choices.license == "MIT" and not choices.holder.strip():
        return Planned(LICENSE, "skip", "Skipped: MIT needs a copyright holder.")
    found = _find(root, r"(licen[cs]e|copying)(\..+)?")
    if not found:
        return Planned(
            LICENSE, "create", f"Creates LICENSE ({label}).", text.encode(), text
        )
    current = _existing(root, found) or b""
    if current.replace(b"\r\n", b"\n") == text.encode():
        return Planned(found, "skip", f"Skipped: {found} already says this.")
    return Planned(
        found,
        "replace",
        f"Replaces {found} with the {label} license.",
        _in_style(text, _newline_of(current)),
        text,
    )


def _plan_gitignore(root: Path | None, language_ids: tuple[str, ...]) -> Planned:
    sections = ignore_sections(language_ids)
    if not sections:
        reason = (
            "none of the chosen languages has a template"
            if language_ids
            else "no languages were chosen"
        )
        return Planned(GITIGNORE, "skip", f"Skipped: {reason}.")
    commit = bundled().gitignore_commit
    existing = _existing(root, GITIGNORE)
    if existing is None:
        text = "\n".join(section.text(commit) for section in sections)
        return Planned(
            GITIGNORE,
            "create",
            f"Creates .gitignore for {_names(sections)}.",
            text.encode(),
            text,
        )
    if _utf16(existing):
        return _unreadable(GITIGNORE)
    current = existing.decode("utf-8", "replace")
    new = [s for s in sections if not _has_section(current, s.template)]
    kept = tuple(
        f"Already has the {s.template} section." for s in sections if s not in new
    )
    if not new:
        return Planned(
            GITIGNORE, "skip", "Skipped: .gitignore already has these.", kept=kept
        )
    addition = "\n".join(section.text(commit) for section in new)
    newline = _newline_of(existing)
    joint = b"" if not existing or existing.endswith(b"\n") else newline
    if existing.strip():
        joint += newline
    return Planned(
        GITIGNORE,
        "append",
        f"Adds {_names(new)} to .gitignore.",
        existing + joint + _in_style(addition, newline),
        addition,
        kept,
    )


def _names(sections: list[IgnoreSection]) -> str:
    return _listing([section.template for section in sections])


def _plan_attributes(root: Path | None, attributes: Attributes) -> Planned:
    for rule in attributes.rules:
        if problem := pattern_problem(rule.pattern):
            return Planned(GITATTRIBUTES, "skip", f"Skipped: {rule.pattern}: {problem}")
    existing = _existing(root, GITATTRIBUTES)
    if existing is None:
        text = gitattributes_text(attributes)
        return Planned(
            GITATTRIBUTES, "create", "Creates .gitattributes.", text.encode(), text
        )
    if _utf16(existing):
        return _unreadable(GITATTRIBUTES)

    rules = _rules_in(existing.decode("utf-8", "replace"))
    decided = dict(rules)  # a later line for the same pattern wins, as in git
    kept: list[str] = []
    first: list[str] = []
    catch_all = [ending for pattern, ending in rules if pattern in ("*", "**")]
    if not catch_all:
        first.append(attributes.default_line())
    elif catch_all[-1] != attributes.default:
        kept.append(
            "Kept your rule for all files: "
            f"{ENDING_LABELS[catch_all[-1]]}, not {ENDING_LABELS[attributes.default]}."
        )
    last: list[str] = []
    for rule in attributes.rules:
        have = decided.get(rule.pattern)
        if have is None:
            last.append(rule.line())
        elif have != rule.ending:
            kept.append(
                f"Kept your rule for {rule.pattern}: "
                f"{ENDING_LABELS[have]}, not {ENDING_LABELS[rule.ending]}."
            )
    added = first + last
    if not added:
        return Planned(
            GITATTRIBUTES,
            "skip",
            "Skipped: .gitattributes already decides all of these.",
            kept=tuple(kept),
        )

    newline = _newline_of(existing)
    lines = existing.splitlines(keepends=True)
    if lines and not lines[-1].endswith(b"\n"):
        lines[-1] += newline
    # Under the comments a file opens with, above its first rule.
    top = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip() and not line.lstrip().startswith(b"#")
        ),
        len(lines),
    )
    content = b"".join(
        [
            *lines[:top],
            *(_in_style(line + "\n", newline) for line in first),
            *lines[top:],
            *(_in_style(line + "\n", newline) for line in last),
        ]
    )
    where = " (the rule for all files at the top)" if first else ""
    return Planned(
        GITATTRIBUTES,
        "append",
        f"Adds {len(added)} rule{'s' if len(added) != 1 else ''} to "
        f".gitattributes{where}.",
        content,
        "\n".join(added) + "\n",
        tuple(kept),
    )


# ---- writing -------------------------------------------------------------------------
_DONE = (("create", "Created"), ("append", "Added to"), ("replace", "Replaced"))


@dataclass
class Written:
    """What writing did, file by file."""

    #: action ("create", "append", "replace") -> the files it was done to.
    done: dict[str, list[str]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """``Created README.md and .gitignore. Added to .gitattributes.``"""
        return " ".join(
            f"{verb} {_listing(self.done[action])}."
            for action, verb in _DONE
            if self.done.get(action)
        )


def _listing(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def write(
    repo: str | Path, planned: Iterable[Planned], *, replace: bool = False
) -> Written:
    """Write what was planned into ``repo``. Replacements only when ``replace``.

    Files this creates or replaces are then given the line endings git would
    check them out with. A file only appended to keeps the ones it had: the
    lines added match them already.
    """
    root = Path(repo)
    report = Written()
    fresh: list[str] = []
    for item in planned:
        if item.action == "skip" or (item.action == "replace" and not replace):
            continue
        try:
            _write_atomically(root / item.name, item.content)
        except OSError as exc:
            report.problems.append(f"Could not write {item.name}: {exc}")
            continue
        report.done.setdefault(item.action, []).append(item.name)
        if item.action in ("create", "replace"):
            fresh.append(item.name)
    if fresh:
        try:
            normalized = git_ops.normalize_line_endings(root, fresh)
        except git_ops.GitError as exc:
            report.problems.append(f"Line endings were not applied: {exc}")
        else:
            report.problems += [
                f"Line endings were not applied to {path}: {why}"
                for path, why in normalized.failed
            ]
    return report


def _write_atomically(target: Path, data: bytes) -> None:
    """``data`` into ``target`` in one step: a temporary file beside it, renamed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(data)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
