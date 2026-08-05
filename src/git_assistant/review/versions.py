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

import re
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


def _find(repo: Path, names: tuple[str, ...], suffix: str = "") -> list[Path]:
    """Manifests at the top of the repository, or a little way down."""
    found: list[Path] = []
    root_depth = len(repo.parts)
    for path in repo.rglob("*"):
        if len(path.parts) - root_depth > _MAX_DEPTH:
            continue
        if any(part in _SKIP for part in path.parts[root_depth:]):
            continue
        if not path.is_file():
            continue
        if path.name in names or (suffix and path.name.endswith(suffix)):
            found.append(path)
        if len(found) >= 8:  # enough to decide; this is not a survey
            break
    return found


# ---- one detector per language -------------------------------------------------------
def _python(repo: Path) -> Detected | None:
    for name in ("pyproject.toml", "setup.cfg"):
        text = _read(repo / name)
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
    text = _read(repo / ".python-version").strip()
    digits = re.match(r"3\.(\d+)", text)
    if digits:
        minor = int(digits.group(1))
        return Detected(
            "py312" if minor >= 12 else "py310" if minor >= 10 else "py38",
            f".python-version: {text}",
        )
    return None


def _rust(repo: Path) -> Detected | None:
    text = _read(repo / "Cargo.toml")
    match = re.search(r"""^\s*edition\s*=\s*["'](\d{4})["']""", text, re.M)
    if not match:
        return None
    edition = match.group(1)
    version = f"rust{edition}"
    lang = languages.get("rust")
    if lang and version in lang.versions:
        return Detected(version, f"Cargo.toml: edition = \"{edition}\"")
    return None


def _typescript(repo: Path) -> Detected | None:
    for path in _find(repo, ("tsconfig.json",)):
        text = _read(path)
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


def _javascript(repo: Path) -> Detected | None:
    for path in _find(repo, ("tsconfig.json", ".babelrc", "babel.config.json")):
        target = re.search(r'"target"\s*:\s*"(es\w+)"', _read(path), re.I)
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


def _package_dep(repo: Path, name: str) -> str:
    match = re.search(
        rf'"{name}"\s*:\s*"([^"]+)"', _read(repo / "package.json")
    )
    return match.group(1) if match else ""


def _csharp(repo: Path) -> Detected | None:
    for path in _find(repo, (), suffix=".csproj"):
        text = _read(path)
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


def _java(repo: Path) -> Detected | None:
    for path in _find(repo, ("pom.xml", "build.gradle", "build.gradle.kts")):
        text = _read(path)
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


def _cpp_std(repo: Path) -> tuple[str, str] | None:
    """``(standard, source)`` for whichever of C/C++ the build files declare."""
    for path in _find(repo, ("CMakeLists.txt", "Makefile", "meson.build")):
        text = _read(path)
        match = re.search(r"CMAKE_CXX_STANDARD\s+(\d+)", text) or re.search(
            r"cxx_std_(\d+)", text
        ) or re.search(r"-std=(?:gnu|c)\+\+(\d+)", text) or re.search(
            r"""cpp_std\s*[:=]\s*['"]c\+\+(\d+)""", text
        )
        if match:
            return f"c++{_two_digit(match.group(1))}", f"{path.name}: C++{match.group(1)}"
    return None


def _c_std(repo: Path) -> tuple[str, str] | None:
    for path in _find(repo, ("CMakeLists.txt", "Makefile", "meson.build")):
        text = _read(path)
        match = re.search(r"CMAKE_C_STANDARD\s+(\d+)", text) or re.search(
            r"-std=(?:gnu|c)(\d+)", text
        ) or re.search(r"""\bc_std\s*[:=]\s*['"]c(\d+)""", text)
        if match:
            return f"c{_two_digit(match.group(1))}", f"{path.name}: C{match.group(1)}"
    return None


def _two_digit(number: str) -> str:
    """``20`` stays ``20``; ``2a``-style names are already excluded by the regex."""
    return number if len(number) <= 2 else number[-2:]


def _cpp(repo: Path) -> Detected | None:
    found = _cpp_std(repo)
    if not found:
        return None
    version, source = found
    lang = languages.get("cpp")
    return Detected(version, source) if lang and version in lang.versions else None


def _c(repo: Path) -> Detected | None:
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


def detect(repo: str, *, wanted: list[str] | None = None) -> dict[str, Detected]:
    """What each language's version is, for the languages the repository declares.

    Only the languages in ``wanted`` are looked for when it is given -- there is
    no point reading a `.csproj` for a repository with no C# in it. A language
    with nothing to read is simply absent from the result.
    """
    root = Path(repo)
    if not root.is_dir():
        return {}
    out: dict[str, Detected] = {}
    for language, detector in _DETECTORS.items():
        if wanted is not None and language not in wanted:
            continue
        try:
            found = detector(root)
        except OSError:
            found = None
        if found is not None:
            out[language] = found
    return out


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
