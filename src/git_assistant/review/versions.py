"""Which version of each language a repository is written in.

Read from what the project already declares -- ``pyproject.toml``,
``Cargo.toml``, ``tsconfig.json``, a ``.csproj``, ``pom.xml``, ``CMakeLists.txt``
-- because a repository that states its own answer should never be asked for it,
and because that answer is checkable: every result carries the file and the line
it came from, and the tab shows it.

Nothing here guesses. A language nothing declares comes back absent, and the
caller asks the user (or, later, the model). A wrong version is worse than no
version: rules are filtered by it, so a version that is too new quietly adds
rules the code could not have followed.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from git_assistant.review import languages

#: How far into a file to look. These declarations live near the top, and a
#: minified or generated file should not cost a megabyte of reading.
_READ_LIMIT = 200_000

#: Directories never worth walking for a manifest.
_SKIP = {".git", "node_modules", "target", "build", "dist", "venv", ".venv", "__pycache__"}

#: How deep to look for a manifest. A solution with projects two folders down is
#: normal; anything deeper is a monorepo, and one answer for it would be wrong.
_MAX_DEPTH = 3

#: How long ago everything a reading rests on must have changed for the reading
#: to be kept. Windows stamps a change with its clock as of the last tick, some
#: sixteen milliseconds, so a second change inside one tick leaves the stamp
#: where the first one put it: a reading taken between the two cannot tell them
#: apart by stamps. Two seconds is past any tick, and past a FAT drive's
#: two-second stamps.
_SETTLE_NS = 2_000_000_000


@dataclass(frozen=True)
class Detected:
    """A version, and where it was read from."""

    version: str
    source: str  # "pyproject.toml: requires-python >=3.11"

    def describe(self) -> str:
        return f"from {self.source}" if self.source else ""


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:_READ_LIMIT]
    except OSError:
        return ""


def _stamp(path: str | Path) -> tuple[int, int] | None:
    """When ``path`` last changed, and its size; None when nothing is there."""
    try:
        status = os.stat(path)
    except OSError:
        return None
    return status.st_mtime_ns, status.st_size


class _Repository:
    """One reading of a repository: the files detection may look at, listed once.

    Every folder is stamped before it is listed and every file before it is read,
    so the reading can be checked against the disk later without being done again.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.started = time.time_ns()
        self.stamps: dict[str, tuple[int, int] | None] = {}
        self._files: list[Path] | None = None
        self._texts: dict[Path, str] = {}

    def read(self, path: Path) -> str:
        if path not in self._texts:
            self.stamps[str(path)] = _stamp(path)
            self._texts[path] = _read(path)
        return self._texts[path]

    def files(self) -> list[Path]:
        """Every file a manifest could be, in the order ``Path.rglob("*")`` lists them.

        The order is kept because it decides which of several manifests is read
        first. That is a folder's own entries; then, folder by folder, the entries
        of each folder in it; then the folders in the last of those, as rglob's
        stack takes them, last first.

        What is not listed is what rglob went on to list and every detector then
        threw away -- everything under .git, node_modules and the rest of
        ``_SKIP``, and everything deeper than ``_MAX_DEPTH`` -- once for each
        detector: 11,668 paths walked six times over to find 345, in this
        application's own repository.
        """
        if self._files is None:
            self._files = self._walk()
        return self._files

    def _list(self, folder: Path) -> list[os.DirEntry]:
        self.stamps[str(folder)] = _stamp(folder)
        try:
            with os.scandir(folder) as entries:
                return list(entries)
        except OSError:
            return []

    def _walk(self) -> list[Path]:
        found: list[Path] = []

        def keep(entries: list[os.DirEntry]) -> None:
            for entry in entries:
                if entry.name in _SKIP:
                    continue
                try:
                    if entry.is_file():
                        found.append(Path(entry.path))
                except OSError:
                    pass

        top = self._list(self.root)
        keep(top)
        # A folder's entries, and how far below the repository they are.
        stack = [(top, 1)]
        while stack:
            entries, depth = stack.pop()
            for entry in entries:
                try:
                    # As rglob decides it: a link to a folder is not followed.
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if entry.name in _SKIP or depth + 1 > _MAX_DEPTH:
                    continue
                inside = self._list(Path(entry.path))
                keep(inside)
                if depth + 2 <= _MAX_DEPTH:
                    stack.append((inside, depth + 1))
        return found


def _find(repo: _Repository, names: tuple[str, ...], suffix: str = "") -> list[Path]:
    """Manifests at the top of the repository, or a little way down."""
    found = [
        path
        for path in repo.files()
        if path.name in names or (suffix and path.name.endswith(suffix))
    ]
    return found[:8]  # enough to decide; this is not a survey


# ---- one detector per language -------------------------------------------------------
def _python(repo: _Repository) -> Detected | None:
    for name in ("pyproject.toml", "setup.cfg"):
        text = repo.read(repo.root / name)
        match = re.search(r"""(?:requires-python|python_requires)\s*=\s*["']?([^"'\n]+)""", text)
        if match:
            wanted = match.group(1).strip()
            digits = re.search(r"3\.(\d+)", wanted)
            if digits:
                minor = int(digits.group(1))
                version = (
                    "py312" if minor >= 12 else
                    "py310" if minor >= 10 else
                    "py38" if minor >= 8 else
                    "py36"
                )
                return Detected(version, f"{name}: {wanted}")
            if wanted.startswith("2"):
                return Detected("py2", f"{name}: {wanted}")
    text = repo.read(repo.root / ".python-version").strip()
    digits = re.match(r"3\.(\d+)", text)
    if digits:
        minor = int(digits.group(1))
        return Detected(
            "py312" if minor >= 12 else "py310" if minor >= 10 else "py38",
            f".python-version: {text}",
        )
    return None


def _rust(repo: _Repository) -> Detected | None:
    text = repo.read(repo.root / "Cargo.toml")
    match = re.search(r"""^\s*edition\s*=\s*["'](\d{4})["']""", text, re.M)
    if not match:
        return None
    edition = match.group(1)
    version = f"rust{edition}"
    lang = languages.get("rust")
    if lang and version in lang.versions:
        return Detected(version, f"Cargo.toml: edition = \"{edition}\"")
    return None


def _typescript(repo: _Repository) -> Detected | None:
    for path in _find(repo, ("tsconfig.json",)):
        # The compiler's own version is what matters, and package.json is where
        # it is pinned; the tsconfig only proves the project is TypeScript.
        pinned = _package_dep(repo, "typescript")
        if pinned:
            major = re.search(r"(\d+)", pinned)
            if major:
                number = min(5, max(1, int(major.group(1))))
                return Detected(f"ts{number}", f"package.json: typescript {pinned}")
        return Detected("ts5", f"{path.name}: present, no version pinned")
    return None


def _javascript(repo: _Repository) -> Detected | None:
    for path in _find(repo, ("tsconfig.json", ".babelrc", "babel.config.json")):
        target = re.search(r'"target"\s*:\s*"(es\w+)"', repo.read(path), re.I)
        if target:
            wanted = target.group(1).lower()
            lang = languages.get("javascript")
            if lang and wanted in lang.versions:
                return Detected(wanted, f"{path.name}: target {target.group(1)}")
            if wanted in ("esnext", "es6"):
                return Detected(
                    "es2025" if wanted == "esnext" else "es2015",
                    f"{path.name}: target {target.group(1)}",
                )
    return None


def _package_dep(repo: _Repository, name: str) -> str:
    match = re.search(
        rf'"{name}"\s*:\s*"([^"]+)"', repo.read(repo.root / "package.json")
    )
    return match.group(1) if match else ""


def _csharp(repo: _Repository) -> Detected | None:
    for path in _find(repo, (), suffix=".csproj"):
        text = repo.read(path)
        explicit = re.search(r"<LangVersion>\s*([\d.]+)\s*</LangVersion>", text)
        if explicit:
            major = int(float(explicit.group(1)))
            return Detected(_cs_version(major), f"{path.name}: LangVersion {explicit.group(1)}")
        framework = re.search(r"<TargetFrameworks?>\s*([^<]+)</TargetFrameworks?>", text)
        if framework:
            wanted = framework.group(1).split(";")[0].strip()
            net = re.match(r"net(\d+)\.", wanted)
            if net:
                # The language version each .NET release defaults to.
                by_net = {5: 9, 6: 10, 7: 11, 8: 12, 9: 13, 10: 13}
                return Detected(
                    _cs_version(by_net.get(int(net.group(1)), 13)),
                    f"{path.name}: TargetFramework {wanted}",
                )
    return None


def _cs_version(major: int) -> str:
    lang = languages.get("csharp")
    wanted = f"cs{max(5, min(13, major))}"
    return wanted if lang and wanted in lang.versions else "cs13"


def _java(repo: _Repository) -> Detected | None:
    for path in _find(repo, ("pom.xml", "build.gradle", "build.gradle.kts")):
        text = repo.read(path)
        for pattern in (
            r"<maven\.compiler\.(?:release|source|target)>\s*(\d+)",
            r"JavaLanguageVersion\.of\((\d+)\)",
            r"(?:sourceCompatibility|targetCompatibility)\s*=?\s*['\"]?(?:JavaVersion\.VERSION_)?(\d+)",
        ):
            match = re.search(pattern, text)
            if match:
                release = int(match.group(1))
                version = (
                    "java22" if release >= 22 else
                    "java21" if release >= 21 else
                    "java17" if release >= 17 else
                    "java11" if release >= 11 else
                    "java8"
                )
                return Detected(version, f"{path.name}: Java {release}")
    return None


def _cpp_std(repo: _Repository) -> tuple[str, str] | None:
    """``(standard, source)`` for whichever of C/C++ the build files declare."""
    for path in _find(repo, ("CMakeLists.txt", "Makefile", "meson.build")):
        text = repo.read(path)
        match = re.search(r"CMAKE_CXX_STANDARD\s+(\d+)", text) or re.search(
            r"cxx_std_(\d+)", text
        ) or re.search(r"-std=(?:gnu|c)\+\+(\d+)", text) or re.search(
            r"""cpp_std\s*[:=]\s*['"]c\+\+(\d+)""", text
        )
        if match:
            return f"c++{_two_digit(match.group(1))}", f"{path.name}: C++{match.group(1)}"
    return None


def _c_std(repo: _Repository) -> tuple[str, str] | None:
    for path in _find(repo, ("CMakeLists.txt", "Makefile", "meson.build")):
        text = repo.read(path)
        match = re.search(r"CMAKE_C_STANDARD\s+(\d+)", text) or re.search(
            r"-std=(?:gnu|c)(\d+)", text
        ) or re.search(r"""\bc_std\s*[:=]\s*['"]c(\d+)""", text)
        if match:
            return f"c{_two_digit(match.group(1))}", f"{path.name}: C{match.group(1)}"
    return None


def _two_digit(number: str) -> str:
    """``20`` stays ``20``; ``2a``-style names are already excluded by the regex."""
    return number if len(number) <= 2 else number[-2:]


def _cpp(repo: _Repository) -> Detected | None:
    found = _cpp_std(repo)
    if not found:
        return None
    version, source = found
    lang = languages.get("cpp")
    return Detected(version, source) if lang and version in lang.versions else None


def _c(repo: _Repository) -> Detected | None:
    found = _c_std(repo)
    if not found:
        return None
    version, source = found
    lang = languages.get("c")
    return Detected(version, source) if lang and version in lang.versions else None


_DETECTORS = {
    "python": _python,
    "rust": _rust,
    "typescript": _typescript,
    "javascript": _javascript,
    "csharp": _csharp,
    "java": _java,
    "cpp": _cpp,
    "c": _c,
}


@dataclass
class _Reading:
    """What detection found in a repository, and the stamps that answer rests on."""

    found: dict[str, Detected]
    stamps: dict[str, tuple[int, int] | None]
    #: Nothing stamped had changed in the `_SETTLE_NS` before the reading began,
    #: so any change since has moved a stamp.
    settled: bool

    def still_holds(self) -> bool:
        return self.settled and all(
            _stamp(path) == stamp for path, stamp in self.stamps.items()
        )


#: The latest reading of each repository, by folder.
_readings: dict[str, _Reading] = {}


def _read_repository(root: Path) -> _Reading:
    repository = _Repository(root)
    found: dict[str, Detected] = {}
    for language, detector in _DETECTORS.items():
        try:
            detected = detector(repository)
        except OSError:
            detected = None
        if detected is not None:
            found[language] = detected
    newest = max((stamp[0] for stamp in repository.stamps.values() if stamp), default=0)
    return _Reading(
        found, repository.stamps, settled=newest < repository.started - _SETTLE_NS
    )


def detect(repo: str, *, wanted: list[str] | None = None) -> dict[str, Detected]:
    """What each language's version is, for the languages the repository declares.

    Only the languages in ``wanted`` are returned when it is given. A language
    with nothing to read is simply absent from the result.

    A repository is read once and the answer kept for as long as nothing it was
    read from has changed: every folder listed and every file read is stamped,
    and checking those stamps is a few dozen ``stat`` calls where reading again
    is a walk. A folder's stamp moves when anything in it is added, removed or
    renamed, which is how a new manifest shows.
    """
    root = Path(repo)
    if not root.is_dir():
        return {}
    key = os.path.normcase(os.path.abspath(root))
    reading = _readings.get(key)
    if reading is None or not reading.still_holds():
        reading = _read_repository(root)
        _readings[key] = reading
    return {
        language: found
        for language, found in reading.found.items()
        if wanted is None or language in wanted
    }


def forget() -> None:
    """Read every repository afresh the next time it is asked about."""
    _readings.clear()


def from_content(language: str, head: str) -> Detected | None:
    """A version a single file states about itself.

    Only two languages do: HTML says so in its doctype, and a shell script and a
    PowerShell script say so in their first line.
    """
    text = (head or "").lstrip()
    if language == "html":
        doctype = re.match(r"<!DOCTYPE\s+html\s*>", text, re.I)
        if doctype:
            return Detected("html5", "the doctype")
        if re.match(r"<!DOCTYPE\s+html\s+PUBLIC", text, re.I):
            return Detected("html4", "the doctype")
    if language == "bash":
        first = text.splitlines()[0] if text else ""
        if first.startswith("#!"):
            if "bash" in first:
                return Detected("bash5", "the shebang")
            if re.search(r"/(?:da)?sh\b", first):
                return Detected("posix", "the shebang")
    if language == "powershell":
        requires = re.search(r"#Requires\s+-Version\s+(\d+)", text, re.I)
        if requires:
            major = int(requires.group(1))
            return Detected("ps7" if major >= 6 else "ps5", "#Requires -Version")
    return None
