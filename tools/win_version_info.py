"""Windows version resource (VS_VERSIONINFO) for the built executables.

Imported by both PyInstaller specs so the portable and installed builds carry
identical metadata, derived from the same ``__version__`` that everything else
in the project derives from.

Why this exists at all: the executables shipped without a version resource,
which is unusual enough to count against them. Windows SmartScreen and
Defender's ML both read this block, and effectively all legitimate software
has one -- a signed-looking name, publisher and version is weak evidence of
provenance, and its *absence* is weak evidence the other way. Packers used by
malware routinely omit it.

It is weak evidence either way. It is not a substitute for code signing, which
is the only strong signal available here. It is simply the part that costs
nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

COMPANY = "Stefan Ghitescu"
PRODUCT = "Git Assistant"
DESCRIPTION = "Local-LLM git commit message assistant"


def read_version(root: Path) -> str:
    """The version from ``src/git_assistant/__init__.py`` -- the one place it lives."""
    text = (root / "src" / "git_assistant" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise SystemExit("could not find __version__ in src/git_assistant/__init__.py")
    return match.group(1)


def _quad(version: str) -> tuple[int, int, int, int]:
    """Turn "0.3.8" into the 4-integer tuple the resource format requires.

    Anything non-numeric (a "0.4.0rc1" style suffix) is dropped rather than
    guessed at: the resource holds four numbers and nothing else, and a
    pre-release tag has nowhere to go. The full string is still written to
    FileVersion/ProductVersion below, which is free text.
    """
    parts = []
    for piece in version.split(".")[:4]:
        digits = re.match(r"\d+", piece)
        parts.append(int(digits.group()) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts)  # type: ignore[return-value]


def version_resource(version: str, exe_name: str) -> VSVersionInfo:
    """Build the VS_VERSIONINFO block for ``exe_name``."""
    quad = _quad(version)
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=quad,
            prodvers=quad,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,  # VOS_NT_WINDOWS32
            fileType=0x1,  # VFT_APP
            subtype=0x0,
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",  # US English, Unicode
                        [
                            StringStruct("CompanyName", COMPANY),
                            StringStruct("FileDescription", DESCRIPTION),
                            StringStruct("FileVersion", version),
                            StringStruct("InternalName", exe_name),
                            StringStruct("LegalCopyright", f"(c) {COMPANY}"),
                            StringStruct("OriginalFilename", f"{exe_name}.exe"),
                            StringStruct("ProductName", PRODUCT),
                            StringStruct("ProductVersion", version),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ],
    )
