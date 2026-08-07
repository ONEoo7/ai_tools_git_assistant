"""Updating: ask winget whether a newer release of this package is published.

Nothing here downloads or executes a release. The application asks winget what
version of `StefanGhitescu.GitAssistant` is published, and if it is newer than
the one running it asks winget to install it. What lands on disk is fetched and
hash-checked by the Windows Package Manager against the manifest in
`microsoft/winget-pkgs`.

This replaced a TUF-verified self-updater that fetched an installer and ran it.
That mechanism was sound, but the capability itself -- an unsigned binary that
downloads and executes something -- is what endpoint protection reacts to, and
it required shipping a second build with the capability compiled out. There is
now one build, and no such capability in it.

See `git_assistant.updating.winget` for the mechanics and `docs/winget.md` for
the manifests.
"""

from git_assistant.updating.winget import (
    CHECK_MINUTES,
    PACKAGE_ID,
    UpdateResult,
    UpdateUnavailableError,
    available_version,
    check_for_update,
    current_version,
    is_newer,
    unavailable_reason,
    upgrade,
    winget_path,
)

__all__ = [
    "CHECK_MINUTES",
    "PACKAGE_ID",
    "UpdateResult",
    "UpdateUnavailableError",
    "available_version",
    "check_for_update",
    "current_version",
    "is_newer",
    "unavailable_reason",
    "upgrade",
    "winget_path",
]
