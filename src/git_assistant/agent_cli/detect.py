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

And one thing found must never be run as it is: a **batch file**. See `command`.
"""

from __future__ import annotations

import os
import re
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

    A batch file found first on PATH -- ``npm install -g`` puts a ``.cmd`` there
    -- gives way to a real program of the same name found anywhere else: that is
    what the vendors' own installers put down, and it needs no shim read to be
    started safely (see `command`).
    """
    path = search_path()
    found = shutil.which(name, path=path)
    if found and not is_batch_file(found):
        return found
    home = Path.home()
    native = [shutil.which(f"{name}.exe", path=path)] if sys.platform == "win32" else []
    native += [str(home / relative) for relative in KNOWN_LOCATIONS.get(name, ())]
    for candidate in native:
        if candidate and Path(candidate).is_file() and not is_batch_file(candidate):
            return candidate
    return found or ""


# ---- starting it without cmd.exe ---------------------------------------------------
#: What Windows starts through cmd.exe rather than as a program.
BATCH_SUFFIXES = (".bat", ".cmd")

#: The line an npm shim ends with: ``"%dp0%\node_modules\...\cli.js" %*`` (older
#: npm wrote ``%~dp0``). The script is what the shim exists to start.
_NPM_SCRIPT_RE = re.compile(r'"%~?dp0%?\\([^"%]+?\.[cm]?js)"\s+%\*', re.IGNORECASE)

#: A Scoop shim is a real program that starts whatever its ``.shim`` file names.
_SCOOP_TARGET_RE = re.compile(r'^\s*path\s*=\s*"?([^"\r\n]+?)"?\s*$', re.MULTILINE)


class UnsafeProgram(Exception):
    """A program that cannot be handed a prompt without cmd.exe reading it."""


def is_batch_file(path: str) -> bool:
    return Path(path).suffix.lower() in BATCH_SUFFIXES


def command(path: str) -> list[str]:
    """How to start the program at ``path`` so that nothing reads its arguments
    as commands.

    A program is started as it is. A batch file is not, ever: Windows runs one
    through cmd.exe, and cmd.exe reads the arguments as a command line of its
    own. Python quotes an argument for programs, escaping a quote as ``\\"``,
    which cmd.exe does not understand -- so a quote and an ``&`` in a prompt,
    and the rest of the prompt is a command. The prompt is a diff, and a diff
    is whatever a repository contains.

    An npm shim is started as what it starts, ``node <script>``. Any other
    batch file is refused. A Scoop shim, a program that starts the file its
    ``.shim`` names, is judged by that file.

    Raises `UnsafeProgram` saying what to do instead.
    """
    target = Path(path)
    named = _scoop_target(target)
    if named is not None:
        target = named
    if not is_batch_file(str(target)):
        return [path]
    started = _npm_command(target)
    if started is not None:
        return started
    install = INSTALL_COMMANDS.get(target.stem.lower(), "")
    raise UnsafeProgram(
        f"{target} is a batch file, and a prompt handed to one is read by cmd.exe, "
        "where the text of a diff can run as a command. It will not be started. "
        + (
            f"Install the program itself instead, from PowerShell: {install}"
            if install
            else "Install the program itself, not a script that starts it."
        )
    )


def _npm_command(shim: Path) -> list[str] | None:
    """``[node, script]`` for an npm shim, or None when it is not one."""
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = _NPM_SCRIPT_RE.search(text)
    if found is None:
        return None
    script = shim.parent / found.group(1)
    if not script.is_file():
        return None
    # The shim's own rule: a node.exe beside it, else whichever node is on PATH.
    beside = shim.parent / "node.exe"
    node = str(beside) if beside.is_file() else node_program()
    return [node, str(script)] if node else None


def node_program() -> str:
    """The node on PATH, when it is a program rather than another batch file."""
    found = shutil.which("node", path=search_path())
    return found if found and not is_batch_file(found) else ""


def _scoop_target(program: Path) -> Path | None:
    """What a Scoop shim starts, read from the ``.shim`` file beside it."""
    try:
        text = program.with_suffix(".shim").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    found = _SCOOP_TARGET_RE.search(text)
    return Path(found.group(1)) if found else None


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
        started = command(path)
    except UnsafeProgram as exc:
        # Said now, on the Connection & Model tab, rather than at the first run.
        return Found(name=name, path=path, problem=str(exc))
    try:
        done = subprocess.run(
            [*started, *version_args],
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
