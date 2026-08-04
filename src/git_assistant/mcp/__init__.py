"""Git Assistant as an MCP server.

The same work the window does -- describe a repository, audit it, write a commit
message with the configured provider -- offered to Claude Desktop and Claude
Code over stdio.

Written against the protocol directly rather than with the MCP SDK. The stdio
half of MCP is a few hundred lines, and the SDK would pull pydantic, anyio and
starlette into three PyInstaller bundles for it -- the same trade this codebase
already made choosing httpx over vendor SDKs.

Nothing in this package imports Qt. The server runs as its own process, started
by the client, with the tray application untouched beside it.
"""

from __future__ import annotations

SERVER_NAME = "git-assistant"

#: Newest first. The current revision is stateless -- version, identity and
#: capabilities ride on every request -- while everything before it opened with
#: an `initialize` handshake. Both are answered; see server.Session.
LATEST = "2026-07-28"
SUPPORTED: tuple[str, ...] = (
    LATEST,
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
#: The first revision that carries its metadata per request rather than
#: negotiating once.
MODERN_FROM = "2026-07-28"

#: `_meta` keys the protocol reserves.
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

INSTRUCTIONS = (
    "Git Assistant exposes the repositories configured in its window. Use "
    "list_repos first: every other tool takes that repository's path or label. "
    "generate_commit_message writes a message for what is currently staged; "
    "run_agent audits a repository's size or its configuration."
)
