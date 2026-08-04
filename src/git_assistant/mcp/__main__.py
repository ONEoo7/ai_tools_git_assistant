"""``python -m git_assistant.mcp`` -- the server, without the tray application."""

import sys

from git_assistant.mcp.server import main

raise SystemExit(main(sys.argv[1:]))
