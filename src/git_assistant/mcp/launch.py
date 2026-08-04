"""The command line a client should be given to start this server.

Kept away from the server itself: the tab imports this to display and register a
command, and must not drag the protocol machinery into the GUI process to do it.
"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_FLAG = "--mcp"
WRITES_FLAG = "--allow-writes"

#: What the console companion is called. The local specs build the first; a
#: build named after the distribution produces the second.
COMPANION_NAMES = ("GitAssistantMcp.exe", "git-assistant-mcp.exe")

NO_SERVER = (
    "This build does not include the MCP server executable. Install Git "
    "Assistant, or run the server from a source checkout."
)


def companion() -> Path | None:
    """The MCP executable beside this one, when there is one."""
    if not getattr(sys, "frozen", False):
        return None
    here = Path(sys.executable).parent
    for name in COMPANION_NAMES:
        if (here / name).is_file():
            return here / name
    return None


def server_command(*, allow_writes: bool = False) -> list[str] | None:
    """Argv that starts the server, or None when this build has no way to.

    Returning None rather than guessing matters: a command that does not exist
    fails silently inside a client, where the only symptom is a server that
    never appears.
    """
    flags = [MCP_FLAG] + ([WRITES_FLAG] if allow_writes else [])
    if getattr(sys, "frozen", False):
        exe = companion()
        return [str(exe), *flags] if exe else None
    return [sys.executable, "-m", "git_assistant", *flags]


def display(command: list[str] | None) -> str:
    """The command as a line someone can read, copy and paste."""
    if not command:
        return ""
    return " ".join(f'"{part}"' if " " in part else part for part in command)


def registration(command: list[str]) -> dict:
    """The `mcpServers` entry, the shape every client that reads JSON expects."""
    return {"command": command[0], "args": list(command[1:])}
