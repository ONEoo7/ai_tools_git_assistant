"""Updating through winget.

This application does not update itself. It asks winget what version of its own
package is published, and if that is newer than what is running it asks winget
to install it. Every byte that ends up on disk is fetched and hash-checked by
the Windows Package Manager against the manifest in `microsoft/winget-pkgs`,
which is where the trust now lives.

That is the whole point of the change. The previous updater downloaded an
executable and ran it, and verified it against TUF metadata signed by keys this
project held. It worked, but an unsigned binary that downloads and executes
something is behaviourally a dropper, which is why builds of it kept being
quarantined -- and answering that required shipping a second installer with the
capability compiled out. Shelling out to winget removes the capability
altogether: there is no code here that fetches or executes a release.

Nothing in this module parses a version out of prose. winget has no
machine-readable output for `search`, so the one thing that *is* parsed -- the
version column -- is found by locating the package identifier in the row and
taking the token after it. That is stable across display languages; matching the
`Version` header is not.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: The primary key in winget-pkgs. It cannot change once published, and it is
#: what every command here is aimed at. See installer/winget/ and docs/winget.md.
PACKAGE_ID = "StefanGhitescu.GitAssistant"

#: The source to ask. Named explicitly so a machine with a private or corporate
#: source configured is not asked a question the community source should answer,
#: and so a package of the same name on another source cannot be offered.
SOURCE = "winget"

#: Minutes between automatic checks.
CHECK_MINUTES = 5

#: A check is two short-lived processes and a metadata query; a minute is
#: generous. Bounded rather than open-ended because this runs on a timer, and a
#: winget that hangs must not leave a thread alive until the process exits.
CHECK_TIMEOUT = 60

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

#: Suppresses the interactive source-agreement prompt, which on a machine that
#: has never run winget would otherwise block forever on a pipe nobody reads.
_QUIET = ("--disable-interactivity", "--accept-source-agreements")

#: winget's exit code for "no package found matching input criteria".
_EXIT_NO_PACKAGE = 20
_NOT_FOUND = re.compile(r"No package found|No installed package", re.I)


class UpdateUnavailableError(RuntimeError):
    """Updating cannot be done here, with the reason to show the user."""


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """A published version that is newer than the one running."""

    version: str
    current: str
    #: Whether winget already knows this machine has the package. Decides
    #: `upgrade` versus `install`, and is worth carrying so the consent dialog
    #: can name the command it is about to run.
    installed: bool = True

    @property
    def command(self) -> list[str]:
        """What installing this actually runs, minus the path to winget."""
        verb = "upgrade" if self.installed else "install"
        return [verb, "--id", PACKAGE_ID, "--exact", "--source", SOURCE]


# ---- finding winget ----------------------------------------------------------
def winget_path() -> str | None:
    """Where winget is, or None.

    `shutil.which` first, then the App Execution Alias directly. The alias is
    the usual answer, but it lives in `WindowsApps`, which is on the *user's*
    PATH -- and a packaged application launched from the Start Menu inherits
    that, while one launched by the installer at the end of a per-machine
    install may not have it yet.
    """
    found = shutil.which("winget")
    if found:
        return found
    if sys.platform != "win32":
        return None
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    alias = Path(local) / "Microsoft" / "WindowsApps" / "winget.exe"
    return str(alias) if alias.is_file() else None


def is_installed() -> bool:
    """Is this a packaged build rather than a source checkout?

    `sys.frozen` is the honest signal: PyInstaller sets it and nothing running
    out of a source tree does. A checkout is refused because upgrading one
    means installing the packaged application beside it -- which is not what
    anybody running `python -m git_assistant` asked for, and because a
    developer should not be told about a release every five minutes.
    """
    return bool(getattr(sys, "frozen", False))


def unavailable_reason() -> str | None:
    """Why updating is off, or None if it is on.

    One place, so the readout, the timer and the error message cannot disagree
    -- they did under the previous updater, where every disabled reason
    surfaced as "no update URL is configured".
    """
    if sys.platform != "win32":
        return "winget is Windows-only, so this build cannot update itself"
    if winget_path() is None:
        return (
            "winget was not found on this machine; install App Installer from "
            "the Microsoft Store to enable updates"
        )
    if not is_installed():
        return (
            "this is a source checkout, not an installed build; "
            "updating applies to a packaged install"
        )
    return None


# ---- running it --------------------------------------------------------------
def _run(argv: list[str], runner=None) -> subprocess.CompletedProcess:
    """One winget invocation. `runner` is how the tests get in.

    `errors="replace"` rather than a strict decode: winget draws progress with
    box-drawing characters and the console code page is not always one that can
    represent them, and a version check must not fail on a spinner.
    """
    if runner is not None:
        return runner(argv)
    try:
        return subprocess.run(  # noqa: S603 - argv we built, no shell
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_NO_WINDOW,
            timeout=CHECK_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise UpdateUnavailableError(
            f"winget did not answer within {CHECK_TIMEOUT} seconds"
        ) from exc
    except OSError as exc:
        raise UpdateUnavailableError(f"winget could not be started: {exc}") from exc


def _version_in(output: str, package_id: str) -> str | None:
    """The version winget printed for this package, from its table.

    winget offers no `--output json` for `search` or `list`, so the table is
    the only source. The row is found by the identifier and the version is the
    token after it, which holds in every display language -- the header
    ("Name  Id  Version") is translated, the identifier is not.
    """
    for line in output.splitlines():
        parts = line.split()
        if package_id not in parts:
            continue
        index = parts.index(package_id)
        if index + 1 < len(parts):
            candidate = parts[index + 1]
            # "Unknown" is what winget prints for a package it found in
            # Programs and Features without a version it can read.
            if candidate and candidate[0].isdigit():
                return candidate
    return None


def available_version(runner=None) -> str | None:
    """The version published on winget, or None if the package is not there.

    None is not an error. Until the manifest is merged into winget-pkgs the
    package genuinely does not exist, and a machine whose source index is a few
    hours stale is the same case -- neither is worth interrupting anyone about,
    and on a five-minute timer an error toast for it would be relentless.
    """
    winget = winget_path()
    if winget is None:
        raise UpdateUnavailableError("winget was not found on this machine")
    done = _run(
        [winget, "search", "--id", PACKAGE_ID, "--exact", "--source", SOURCE, *_QUIET],
        runner,
    )
    if done.returncode != 0:
        # Exit 20 is "no package found", which is the ordinary answer here.
        # Anything else is a real failure and is reported with winget's words.
        if _NOT_FOUND.search(done.stdout or "") or done.returncode == _EXIT_NO_PACKAGE:
            return None
        raise UpdateUnavailableError(
            (done.stderr or done.stdout or f"winget search failed ({done.returncode})")
            .strip()
            .splitlines()[-1][:300]
        )
    return _version_in(done.stdout or "", PACKAGE_ID)


def is_known_to_winget(runner=None) -> bool:
    """Does winget already list this machine as having the package?

    Decides `winget upgrade` from `winget install`. It matters: this
    application is also distributed as an NSIS installer and as a portable
    build, and `winget upgrade` on an install winget never made reports "no
    installed package found" and does nothing. `winget install` on the same
    machine runs the same installer over the top, which is how this application
    has always been upgraded.
    """
    winget = winget_path()
    if winget is None:
        return False
    done = _run(
        [winget, "list", "--id", PACKAGE_ID, "--exact", *_QUIET],
        runner,
    )
    return done.returncode == 0 and _version_in(done.stdout or "", PACKAGE_ID) is not None


# ---- comparing ----------------------------------------------------------------
def _parts(version: str) -> tuple[int, ...]:
    """A version as numbers, for comparison. Non-numeric tails are dropped.

    Deliberately lenient: winget publishes whatever the manifest says, and a
    version this cannot parse must read as "not newer" rather than raise. The
    cost of being wrong in that direction is a missed update; the other
    direction offers an upgrade to something older.
    """
    found = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in found[:4])


def is_newer(candidate: str, current: str) -> bool:
    """Is `candidate` a version worth offering to someone running `current`?

    Padded to the same length before comparing, so 0.4 and 0.4.0 are equal
    rather than one being newer than the other by virtue of being shorter.
    """
    a, b = _parts(candidate), _parts(current)
    if not a:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def current_version() -> str:
    """This build's version, from the one place it is written."""
    from git_assistant import __version__

    return __version__


def check_for_update(runner=None) -> UpdateResult | None:
    """Ask winget whether it has something newer than this build.

    Returns None when there is nothing to offer -- the package is not published,
    or what is published is not newer. Neither is an error.

    Raises:
        UpdateUnavailableError: updating cannot be done on this machine, or
            winget failed in a way that is not "nothing found".
    """
    reason = unavailable_reason()
    if reason is not None:
        raise UpdateUnavailableError(reason)

    published = available_version(runner)
    current = current_version()
    if published is None or not is_newer(published, current):
        return None
    return UpdateResult(
        version=published,
        current=current,
        installed=is_known_to_winget(runner),
    )


def upgrade(result: UpdateResult, runner=None) -> str:
    """Hand the installation to winget and return what it said.

    Blocking, and therefore for a worker thread: winget downloads the installer
    and runs it, and this returns once that has finished or failed. The caller
    is expected to quit shortly afterwards -- the installer stops the running
    application itself, but exiting cleanly is what gets settings written.

    A per-machine installation's installer asks for elevation, so winget raises
    a UAC prompt. Declining it is a failure reported here, not a silent no-op.

    Raises:
        UpdateUnavailableError: if winget could not install it.
    """
    winget = winget_path()
    if winget is None:
        raise UpdateUnavailableError("winget was not found on this machine")

    done = _run(
        [
            winget,
            *result.command,
            "--silent",  # the installer's own UI would appear behind the tray
            "--accept-package-agreements",
            *_QUIET,
        ],
        runner,
    )
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise UpdateUnavailableError(
            f"winget could not install {result.version}: "
            + (detail[-1][:300] if detail else f"exit code {done.returncode}")
        )
    return f"winget installed Git Assistant {result.version}."
