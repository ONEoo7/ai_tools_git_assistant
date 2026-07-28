"""Check that a release tag matches the version declared in the source.

    python tools/check_tag_version.py v0.3.1

Prints the version and exits 0 when they agree; prints what to fix and exits 1
when they do not. Used by the pre-push hook so a mismatched tag is caught before
it reaches the remote, where correcting it means deleting a published tag.

The version lives in src/git_assistant/__init__.py only: pyproject derives it
from there, and a frozen build reports that literal because PyInstaller ships no
distribution metadata.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "src" / "git_assistant" / "__init__.py"


def declared_version() -> str:
    text = SOURCE.read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise SystemExit(f"no __version__ found in {SOURCE}")
    return m.group(1)


def check(tag: str) -> int:
    implied = tag[1:] if tag.startswith("v") else tag
    declared = declared_version()
    if implied == declared:
        print(declared)
        return 0

    rel = SOURCE.relative_to(ROOT).as_posix()
    print(
        f"Tag {tag} implies version {implied}, but {rel} declares {declared}.\n"
        f"\n"
        f"Either bump the source to match the tag:\n"
        f'  1. set __version__ = "{implied}" in {rel}\n'
        f"  2. uv lock && git commit -am 'chore: bump version to {implied}'\n"
        f"  3. git tag -f {tag} && push again\n"
        f"\n"
        f"or tag the version that is actually declared:\n"
        f"  git tag -d {tag} && git tag v{declared}\n",
        file=sys.stderr,
    )
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {Path(argv[0]).name} <tag>", file=sys.stderr)
        return 2
    return check(argv[1])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
