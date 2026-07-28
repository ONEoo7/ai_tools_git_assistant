"""Semantic-version parsing and bumping for release tags.

Pure helpers (no Qt, no git) so the bump rules are unit-testable. A tag's
prefix is preserved, so a repo tagging ``v0.2.0`` keeps getting ``v``-prefixed
tags while one tagging ``0.2.0`` does not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# v1.2.3 / 1.2.3 / release-1.2.3, optionally with a -suffix we drop when bumping.
_VERSION_RE = re.compile(
    r"^(?P<prefix>[^0-9]*)"
    r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?P<suffix>.*)$"
)

PARTS = ("major", "minor", "patch")
DEFAULT_FIRST_VERSION = "v0.1.0"


@dataclass(frozen=True)
class Version:
    prefix: str
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.prefix}{self.major}.{self.minor}.{self.patch}"

    @property
    def sort_key(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)


def parse_version(tag: str) -> Version | None:
    """Parse a tag into a :class:`Version`, or None if it isn't semver-like."""
    m = _VERSION_RE.match((tag or "").strip())
    if not m:
        return None
    return Version(
        prefix=m.group("prefix"),
        major=int(m.group("major")),
        minor=int(m.group("minor")),
        patch=int(m.group("patch")),
    )


def bump(version: Version, part: str) -> Version:
    """Return ``version`` with ``part`` incremented and lower parts reset."""
    if part == "major":
        return Version(version.prefix, version.major + 1, 0, 0)
    if part == "minor":
        return Version(version.prefix, version.major, version.minor + 1, 0)
    if part == "patch":
        return Version(version.prefix, version.major, version.minor, version.patch + 1)
    raise ValueError(f"unknown version part: {part!r}")


def latest_version(tags: list[str]) -> Version | None:
    """Highest semver-like tag from ``tags`` (non-semver tags are ignored)."""
    parsed = [v for v in (parse_version(t) for t in tags) if v is not None]
    if not parsed:
        return None
    return max(parsed, key=lambda v: v.sort_key)


def proposals(current: Version | None) -> dict[str, str]:
    """Map each bump part to the tag it would produce.

    With no existing version every option proposes the first release, so the
    user still gets a sensible default to edit.
    """
    if current is None:
        return {part: DEFAULT_FIRST_VERSION for part in PARTS}
    return {part: str(bump(current, part)) for part in PARTS}
