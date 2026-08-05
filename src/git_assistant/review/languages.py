"""Which language a file is in, and which versions that language has.

A review spans whatever is staged, and that is rarely one language. The rules
worth checking differ per language, and several of them differ per *version* of
it -- so both have to be decided per file before anything is sent.

Detection is by extension, because that is what is reliable. Where an extension
is genuinely ambiguous (``.h`` is C or C++ and has been for thirty years) the
answer is looked for in the repository and in the file's own first lines, and if
it is still not clear the file says so rather than guessing: a wrong language
means a whole file reviewed against rules that were never meant for it.

The version lists are ordered oldest to newest. That order is the whole
mechanism: a rule marked ``since: "c++14"`` applies to every version at or after
that index, and to nothing before it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from git_assistant.metrics import ext_of

#: The language of a file nothing could identify. Never reviewed: a file
#: reviewed against another language's rules is worse than one skipped.
UNKNOWN = ""

#: Stands for "whatever the file is" in a profile. It is what a single table
#: covering a whole repository looks like, and what catches a language this
#: build has never heard of.
ANY = "*"


@dataclass(frozen=True)
class Language:
    """One language, its files, and the versions a rule can be pinned to."""

    id: str
    label: str
    extensions: tuple[str, ...]
    #: Oldest first. A rule's `since`/`until` are ids from this list, and
    #: comparing their positions is what decides whether it applies.
    versions: tuple[str, ...]
    version_labels: tuple[str, ...] = ()
    #: Files with no extension that are this language, by shebang word.
    shebangs: tuple[str, ...] = ()

    def label_for(self, version: str) -> str:
        """How a version is written for a reader."""
        if not version:
            return ""
        if version in self.versions and self.version_labels:
            return self.version_labels[self.versions.index(version)]
        return version

    def index_of(self, version: str) -> int:
        """Where a version sits in the ordered list, or -1 if it is not one."""
        return self.versions.index(version) if version in self.versions else -1


LANGUAGES: tuple[Language, ...] = (
    Language(
        id="c",
        label="C",
        extensions=(".c", ".h"),
        versions=("c89", "c99", "c11", "c17", "c23"),
        version_labels=("C89/90", "C99", "C11", "C17", "C23"),
    ),
    Language(
        id="cpp",
        label="C++",
        extensions=(".cpp", ".cc", ".cxx", ".c++", ".hpp", ".hh", ".hxx", ".ipp"),
        versions=("c++98", "c++11", "c++14", "c++17", "c++20", "c++23", "c++26"),
        version_labels=(
            "C++98/03",
            "C++11",
            "C++14",
            "C++17",
            "C++20",
            "C++23",
            "C++26",
        ),
    ),
    Language(
        id="csharp",
        label="C#",
        extensions=(".cs",),
        versions=("cs5", "cs6", "cs7", "cs8", "cs9", "cs10", "cs11", "cs12", "cs13"),
        version_labels=(
            "C# 1.0-5",
            "C# 6",
            "C# 7",
            "C# 8",
            "C# 9",
            "C# 10",
            "C# 11",
            "C# 12",
            "C# 13/14",
        ),
    ),
    Language(
        id="css",
        label="CSS",
        extensions=(".css",),
        versions=("css2", "css3", "css2020", "css2024"),
        version_labels=("CSS 2.1", "CSS 3", "CSS 2020+", "CSS Next-Gen 2024+"),
    ),
    Language(
        id="html",
        label="HTML",
        extensions=(".html", ".htm", ".xhtml"),
        versions=("html4", "html5", "html2020", "html2024"),
        version_labels=(
            "HTML 4 / XHTML",
            "HTML 5",
            "HTML Living Standard 2020+",
            "HTML Next-Gen 2024+",
        ),
    ),
    Language(
        id="java",
        label="Java",
        extensions=(".java",),
        versions=("java8", "java11", "java17", "java21", "java22"),
        version_labels=("Java 8", "Java 11", "Java 17", "Java 21", "Java 22+"),
    ),
    Language(
        id="javascript",
        label="JavaScript",
        extensions=(".js", ".mjs", ".cjs", ".jsx"),
        versions=("es5", "es2015", "es2017", "es2020", "es2022", "es2025"),
        version_labels=("ES5", "ES6/2015", "ES2017", "ES2020", "ES2022", "ES2025"),
        shebangs=("node",),
    ),
    Language(
        id="python",
        label="Python",
        extensions=(".py", ".pyi", ".pyw"),
        versions=("py2", "py36", "py38", "py310", "py312"),
        version_labels=(
            "Python 2",
            "Python 3.6",
            "Python 3.8",
            "Python 3.10",
            "Python 3.12+",
        ),
        shebangs=("python", "python3"),
    ),
    Language(
        id="rust",
        label="Rust",
        extensions=(".rs",),
        versions=("rust2015", "rust2018", "rust2021", "rust2024"),
        version_labels=("Rust 2015", "Rust 2018", "Rust 2021", "Rust 2024"),
    ),
    Language(
        id="typescript",
        label="TypeScript",
        extensions=(".ts", ".tsx", ".mts", ".cts"),
        versions=("ts1", "ts2", "ts3", "ts4", "ts5"),
        version_labels=("TS 1.x", "TS 2.x", "TS 3.x", "TS 4.x", "TS 5.0+"),
    ),
    Language(
        id="bash",
        label="Shell",
        extensions=(".sh", ".bash", ".zsh"),
        versions=("posix", "bash4", "bash5"),
        version_labels=("POSIX sh", "Bash 4", "Bash 5"),
        shebangs=("sh", "bash", "zsh", "dash"),
    ),
    Language(
        id="powershell",
        label="PowerShell",
        extensions=(".ps1", ".psm1", ".psd1"),
        versions=("ps5", "ps7"),
        version_labels=("Windows PowerShell 5.1", "PowerShell 7+"),
        shebangs=("pwsh", "powershell"),
    ),
)

_BY_ID = {lang.id: lang for lang in LANGUAGES}

#: Extension -> language id, for the extensions only one language claims.
_BY_EXT: dict[str, str] = {}
for _lang in LANGUAGES:
    for _ext in _lang.extensions:
        _BY_EXT.setdefault(_ext, _lang.id)

#: Extensions two languages could claim, and who else claims them. ``.h`` is the
#: only real one: C and C++ have shared it since C++ existed.
AMBIGUOUS: dict[str, tuple[str, ...]] = {".h": ("c", "cpp")}

#: What a C++ header looks like from the first lines of its own diff. Cheap and
#: one-sided: any of these means C++, none of them means nothing.
_CPP_MARKERS = re.compile(
    r"\b(namespace|template\s*<|class\s+\w|public:|private:|protected:|"
    r"std::|nullptr|constexpr|#include\s*<(vector|string|memory|map|iostream))",
)

_SHEBANG = re.compile(r"^#!\s*(?:/usr/bin/env\s+|\S*/)?([\w.-]+)")


def get(language_id: str) -> Language | None:
    return _BY_ID.get(language_id)


def label_of(language_id: str) -> str:
    """A language's name, for a row in a table."""
    if language_id == ANY:
        return "Any language"
    lang = _BY_ID.get(language_id)
    return lang.label if lang else "Unknown"


def ids() -> list[str]:
    return [lang.id for lang in LANGUAGES]


def detect(
    path: str,
    *,
    head: str = "",
    repo_hint: str = "",
    overrides: dict[str, str] | None = None,
) -> str:
    """The language of ``path``, or ``UNKNOWN``.

    ``overrides`` is the answer already given for an extension (``{".h":
    "cpp"}``), and it wins: it was a decision, not a guess. Otherwise an
    ambiguous extension is settled by ``head`` -- the first lines of the file,
    which the caller already holds as part of its diff -- and then by
    ``repo_hint``, the language the rest of the repository is written in.
    """
    extension = ext_of(path).lower()
    chosen = (overrides or {}).get(extension, "")
    if chosen:
        return chosen if chosen in _BY_ID else UNKNOWN

    if extension in AMBIGUOUS:
        return _settle(extension, head=head, repo_hint=repo_hint)
    if extension in _BY_EXT:
        return _BY_EXT[extension]
    # No extension at all: ext_of hands back the basename, so a shebang is the
    # only thing left to go on.
    return _from_shebang(head)


def _settle(extension: str, *, head: str, repo_hint: str) -> str:
    """Which of the languages claiming ``extension`` this file is."""
    claimants = AMBIGUOUS[extension]
    if extension == ".h" and _CPP_MARKERS.search(head or ""):
        return "cpp"
    if repo_hint in claimants:
        return repo_hint
    return claimants[0]


def _from_shebang(head: str) -> str:
    match = _SHEBANG.match((head or "").lstrip())
    if not match:
        return UNKNOWN
    word = match.group(1).lower()
    for lang in LANGUAGES:
        if word in lang.shebangs:
            return lang.id
    return UNKNOWN


def hint_from(paths: list[str]) -> str:
    """Which of an ambiguous pair a repository is written in.

    A repository with a single ``.cpp`` in it is a C++ repository, and its
    headers are C++ headers. One that has only ``.c`` files is not.
    """
    unambiguous = {
        _BY_EXT.get(ext, "")
        for ext in (ext_of(p).lower() for p in paths)
        if ext not in AMBIGUOUS  # the files being settled cannot settle themselves
    }
    if "cpp" in unambiguous:
        return "cpp"
    if "c" in unambiguous:
        return "c"
    return ""


def is_source(path: str) -> bool:
    """Whether this is a file any of the twelve languages claims."""
    extension = ext_of(path).lower()
    return extension in _BY_EXT or extension in AMBIGUOUS
