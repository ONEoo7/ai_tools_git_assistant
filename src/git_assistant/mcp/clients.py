"""Registering this server with Claude Desktop and Claude Code.

Claude Desktop reads a JSON file that also holds everything else the user has
ever configured there, so registration is a parse, one key, and a write back --
never a template. A file that will not parse is refused rather than replaced:
losing someone's preferences is far worse than an unregistered server.

Claude Code has a CLI for exactly this, so that is what gets used.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from git_assistant.mcp import SERVER_NAME
from git_assistant.mcp.launch import registration

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
#: Claude Code writes `local` into the current project only, which reads as a
#: broken registration from every other repository.
DEFAULT_SCOPE = "user"
SCOPES = ("local", "user", "project")


class ClientError(RuntimeError):
    """Registration could not be done, with the reason to show the user."""


@dataclass
class Registration:
    """What a client currently has recorded for this server."""

    present: bool = False
    command: list[str] | None = None
    detail: str = ""

    def describe(self, wanted: list[str] | None) -> str:
        if not self.present:
            return "Not registered."
        if wanted and self.command and self.command != wanted:
            return "Registered, but with a different command than the one above."
        writes = "--allow-writes" in (self.command or [])
        return f"Registered ({'writes allowed' if writes else 'read-only'})."


# ---- Claude Desktop ---------------------------------------------------------
def desktop_config_path() -> Path:
    roaming = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(roaming) / "Claude" / "claude_desktop_config.json"


def _read_desktop(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ClientError(
            f"{path.name} is not valid JSON ({exc}). Fix or move it; this will "
            "not overwrite it."
        ) from exc
    except OSError as exc:
        raise ClientError(f"could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ClientError(f"{path.name} does not hold a JSON object; leaving it alone.")
    return data


def _write_desktop(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        backup = path.with_name(path.name + ".git-assistant.bak")
        if path.exists() and not backup.exists():
            backup.write_bytes(path.read_bytes())  # once, before the first edit
        tmp = path.with_name(path.name + ".git-assistant.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise ClientError(f"could not write {path}: {exc}") from exc


def desktop_status(wanted: list[str] | None = None) -> Registration:
    path = desktop_config_path()
    try:
        data = _read_desktop(path)
    except ClientError as exc:
        return Registration(detail=str(exc))
    entry = (data.get("mcpServers") or {}).get(SERVER_NAME)
    if not isinstance(entry, dict):
        return Registration(detail=str(path))
    command = [str(entry.get("command", "")), *[str(a) for a in entry.get("args", [])]]
    return Registration(present=True, command=command, detail=str(path))


def register_desktop(command: list[str]) -> str:
    """Add this server, leaving every other setting in the file untouched."""
    path = desktop_config_path()
    data = _read_desktop(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_NAME] = registration(command)
    data["mcpServers"] = servers
    _write_desktop(path, data)
    return (
        f"Added to {path}.\n\nQuit Claude Desktop completely and start it again — "
        "it reads this file only at startup."
    )


def unregister_desktop() -> str:
    path = desktop_config_path()
    data = _read_desktop(path)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return f"{SERVER_NAME} was not registered in {path.name}."
    servers.pop(SERVER_NAME)
    if servers:
        data["mcpServers"] = servers
    else:
        data.pop("mcpServers")  # leave the file as it was found
    _write_desktop(path, data)
    return f"Removed from {path}. Restart Claude Desktop to apply it."


# ---- Claude Code -------------------------------------------------------------
def claude_cli() -> str | None:
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / (
        "claude.exe" if sys.platform == "win32" else "claude"
    )
    return str(fallback) if fallback.is_file() else None


def _run(argv: list[str], runner=None) -> subprocess.CompletedProcess:
    if runner is not None:
        return runner(argv)
    return subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=_NO_WINDOW,
    )


def add_argv(cli: str, command: list[str], scope: str = DEFAULT_SCOPE) -> list[str]:
    """`claude mcp add <name> --scope <scope> -- <command...>`.

    Everything after `--` is the server's own command line, which is what keeps
    `--mcp` from being read as a flag for the CLI itself.
    """
    return [cli, "mcp", "add", SERVER_NAME, "--scope", scope, "--", *command]


def remove_argv(cli: str, scope: str = DEFAULT_SCOPE) -> list[str]:
    return [cli, "mcp", "remove", SERVER_NAME, "--scope", scope]


def code_status(wanted: list[str] | None = None, *, runner=None) -> Registration:
    cli = claude_cli()
    if cli is None:
        return Registration(detail="The claude CLI was not found on this machine.")
    done = _run([cli, "mcp", "get", SERVER_NAME], runner)
    if done.returncode != 0:
        return Registration(detail=cli)
    return Registration(present=True, detail=(done.stdout or "").strip()[:400])


def register_code(command: list[str], scope: str = DEFAULT_SCOPE, *, runner=None) -> str:
    cli = claude_cli()
    if cli is None:
        raise ClientError(
            "The claude CLI was not found. Install Claude Code, or copy the "
            "command above and register it yourself."
        )
    if scope not in SCOPES:
        raise ClientError(f"scope must be one of: {', '.join(SCOPES)}")
    # Registering twice is an error rather than an update, so drop it first.
    _run(remove_argv(cli, scope), runner)
    done = _run(add_argv(cli, command, scope), runner)
    if done.returncode != 0:
        raise ClientError((done.stderr or done.stdout or "claude mcp add failed").strip())
    note = (done.stdout or "").strip()
    return f"Registered with Claude Code ({scope} scope).\n{note}".strip()


def unregister_code(scope: str = DEFAULT_SCOPE, *, runner=None) -> str:
    cli = claude_cli()
    if cli is None:
        raise ClientError("The claude CLI was not found.")
    done = _run(remove_argv(cli, scope), runner)
    if done.returncode != 0:
        raise ClientError((done.stderr or done.stdout or "claude mcp remove failed").strip())
    return f"Removed from Claude Code ({scope} scope)."


def snippet(command: list[str]) -> str:
    """The `mcpServers` fragment, for any other client that reads one."""
    return json.dumps({"mcpServers": {SERVER_NAME: registration(command)}}, indent=2)
