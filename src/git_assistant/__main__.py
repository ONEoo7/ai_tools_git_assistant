"""The one place that reads argv: tray application, or MCP server.

Both executables in a packaged build run this same script, so the choice cannot
be made by which one was started -- it is made here, before anything heavy is
imported. That ordering is the point: ``app.main`` builds a QApplication,
refuses to run without a system tray, and hands off to an already-running
instance through a named pipe. All three are right for a tray icon and fatal for
a subprocess a client is talking to over stdin.
"""

from __future__ import annotations

import sys
from pathlib import Path

MCP_FLAG = "--mcp"

_WRONG_EXE = """\
This is Git Assistant's MCP server. It speaks MCP over stdin/stdout and is
meant to be started by a client such as Claude Desktop or Claude Code, with
--mcp. Run GitAssistant.exe for the application itself, and see its MCP Server
tab to register this one.
"""


def _entry(argv: list[str], program: str) -> int:
    if MCP_FLAG in argv[1:]:
        # Imported here, not at module scope: this path must never load Qt.
        from git_assistant.mcp.server import main as serve

        return serve(argv[1:])
    if Path(program).stem.lower().endswith("mcp"):
        # The console companion, started without --mcp. Say so rather than
        # opening a second tray icon out of a console window.
        sys.stderr.write(_WRONG_EXE)
        return 2
    from git_assistant.app import main

    return main()


if __name__ == "__main__":
    raise SystemExit(_entry(sys.argv, sys.argv[0]))
