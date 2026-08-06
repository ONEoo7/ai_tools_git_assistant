"""Finding an agent CLI, and installing one that is not there yet.

The awkward part is not the search, it is that **a running process never sees a
PATH change**. Every one of these installers writes ``HKCU\\Environment\\Path``
and broadcasts ``WM_SETTINGCHANGE``; the broadcast reaches applications that
listen for it, and nothing at all reaches this process's ``os.environ``, which
was copied from its parent at launch. So an install that succeeds is followed by
a detection that fails, and the user is told to restart the application for no
reason.

Two answers, and both are needed:

- read the PATH back out of the registry rather than out of the environment, and
- know where each installer puts things, because those locations are fixed and
  a direct check answers even when the registry read does not.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

#: Where each installer puts the program, checked directly when PATH does not
#: answer. Relative to the user's home; the installers are per-user.
KNOWN_LOCATIONS: dict[str, tuple[str, ...]] = {
    "claude": (
        ".local/bin/claude.exe",
        ".local/bin/claude",
        "AppData/Local/Programs/claude/claude.exe",
    ),
    "agy": (
        "AppData/Local/agy/bin/agy.exe",
        "AppData/Local/agy/bin/agy",
        ".local/bin/agy.exe",
    ),
}

#: The vendors' own documented installers, run through PowerShell. Shown to the
#: user in full before anything runs: this downloads a script and executes it,
#: and that is not something to do behind a spinner.
INSTALL_COMMANDS: dict[str, str] = {
    "claude": "irm https://claude.ai/install.ps1 | iex",
    "agy": "irm https://antigravity.google/cli/install.ps1 | iex",
}

#: Only ever run to ask a version. Anything longer is a hung UI.
PROBE_TIMEOUT = 20.0
#: An installer downloads a runtime; it is allowed to take a while.
INSTALL_TIMEOUT = 900.0


@dataclass(frozen=True)
class Found:
    """Where a CLI is, and what it says it is."""

    name: str
    path: str = ""
    version: str = ""
    problem: str = ""

    @property
    def installed(self) -> bool:
        return bool(self.path)

    def describe(self) -> str:
        if self.problem:
            return self.problem
        if not self.path:
            return f"'{self.name}' is not installed, or not on PATH."
        where = f"Found at {self.path}"
        return f"{where} (version {self.version})." if self.version else f"{where}."


# ---- PATH, read from where it actually lives ---------------------------------------
def registry_path() -> str:
    """PATH as Windows has it *now*, not as this process inherited it.

    Both halves, user then machine, in the order the shell would search them.
    Returns "" off Windows or when the registry cannot be read, and the caller
    falls back to the environment -- a stale PATH is worse than the live one and
    better than none.
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg
    except ImportError:
        return ""

    parts: list[str] = []
    for root, key in (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    ):
        try:
            with winreg.OpenKey(root, key) as handle:
                value, _kind = winreg.QueryValueEx(handle, "Path")
        except OSError:
            continue
        # REG_EXPAND_SZ holds things like %USERPROFILE%\.local\bin verbatim.
        parts.append(os.path.expandvars(str(value)))
    return os.pathsep.join(p for p in parts if p)


def search_path() -> str:
    """Everywhere to look: the live registry PATH and this process's own."""
    inherited = os.environ.get("PATH", "")
    live = registry_path()
    return os.pathsep.join(p for p in (live, inherited) if p) or inherited


def locate(name: str) -> str:
    """The program's full path, or "".

    PATH first, then the places the installers are known to use. The second
    check is not a fallback for tidiness -- it is what makes an install
    detectable in the same session that performed it, whatever the registry did.
    """
    found = shutil.which(name, path=search_path())
    if found:
        return found
    home = Path.home()
    for relative in KNOWN_LOCATIONS.get(name, ()):
        candidate = home / relative
        if candidate.is_file():
            return str(candidate)
    return ""


def child_env() -> dict[str, str]:
    """The environment to hand a CLI.

    Two edits. The PATH is the live one, so the CLI's own child processes
    resolve. And ``CLAUDECODE`` is removed, because Claude Code refuses to start
    inside another Claude Code session -- which would break this for exactly the
    people most likely to want it.
    """
    env = dict(os.environ)
    env["PATH"] = search_path()
    for name in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        env.pop(name, None)
    return env


# ---- is it there, and does it run --------------------------------------------------
def probe(name: str, version_args: tuple[str, ...] = ("--version",)) -> Found:
    """Find it and ask its version. Never raises."""
    path = locate(name)
    if not path:
        return Found(name=name)
    try:
        done = subprocess.run(
            [path, *version_args],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            env=child_env(),
            **_no_window(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Found(name=name, path=path, problem=f"{path} could not be run: {exc}")
    first = (done.stdout or done.stderr or "").strip().splitlines()
    return Found(name=name, path=path, version=first[0].strip() if first else "")


def _no_window() -> dict:
    """Keep a console window from flashing up on Windows."""
    if sys.platform != "win32":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


# ---- installing one ------------------------------------------------------------------
def install_command(name: str) -> str:
    """The vendor's own one-liner, or "" for a CLI we do not install."""
    return INSTALL_COMMANDS.get(name, "")


def install(name: str, progress=None) -> Found:
    """Run the vendor's installer, then look again. Never raises.

    The caller is responsible for having asked first -- this downloads a script
    and executes it, and `INSTALL_COMMANDS` is shown in full in that question.
    """
    say = progress or (lambda _text: None)
    command = install_command(name)
    if not command:
        return Found(name=name, problem=f"No installer is known for '{name}'.")
    if sys.platform != "win32":
        return Found(
            name=name, problem="These installers are PowerShell, so Windows only."
        )

    say(f"Running: {command}")
    try:
        done = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                command,
            ],
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT,
            env=child_env(),
            **_no_window(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Found(name=name, problem=f"The installer could not be run: {exc}")

    # Looked for again from the registry, not from this process's PATH, which
    # the installer had no way to change. See the module docstring.
    say("Installed. Looking for it...")
    found = probe(name)
    if found.installed:
        return found
    detail = (done.stderr or done.stdout or "").strip().splitlines()
    return Found(
        name=name,
        problem=(
            "The installer finished but the program is still not where it was "
            "expected. " + (detail[-1][:200] if detail else "")
        ).strip(),
    )
