"""The stdio server: read a line, answer a line, and never print anything else.

Three properties this file exists to guarantee:

*Nothing but MCP reaches stdout.* The client parses every line it reads, so one
stray ``print``, one warning from a C extension, one bootloader message, and the
session is over. File descriptor 1 is pointed at stderr before anything else can
write to it, and the real one is kept privately for the writer below.

*A notification is never answered.* Replying to a message with no ``id`` is the
most common way a hand-written server breaks a client.

*Both protocol eras work.* The current revision carries its version and identity
on every request; every release before it opened with an ``initialize``
handshake. Which one a client speaks is decided by how it opens.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from git_assistant.mcp import (
    INSTRUCTIONS,
    LATEST,
    META_CLIENT_INFO,
    META_SERVER_INFO,
    META_VERSION,
    SERVER_NAME,
    SUPPORTED,
)

log = logging.getLogger("git_assistant.mcp")

# JSON-RPC, and the one MCP adds.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022

#: Enough for the long tools to overlap without letting a client start a
#: hundred audits at once.
MAX_CONCURRENT = 4

USAGE = f"""\
{SERVER_NAME} MCP server.

    --mcp             run the server (required)
    --allow-writes    also offer the tools that change a repository
    --log-level LEVEL stderr verbosity: debug, info, warning, error

It speaks MCP over stdin/stdout and is meant to be started by a client, not by
hand. Register it from Git Assistant's MCP Server tab.
"""


class _Channel:
    """The only way anything reaches the client."""

    def __init__(self, stream) -> None:
        self._stream = stream
        self._lock = threading.Lock()

    def send(self, message: dict) -> None:
        # Bytes, not text: Windows text mode would turn the delimiter into
        # \r\n, and the console encoding would mangle a diff. json.dumps
        # escapes control characters, so a line break inside a value cannot
        # split a message in two.
        data = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self._stream.write(data.encode("utf-8") + b"\n")
            self._stream.flush()


def _claim_stdout() -> _Channel:
    """Take file descriptor 1 for the protocol and give everyone else stderr."""
    private = os.dup(1)
    os.dup2(2, 1)  # anything writing to fd 1 from here on lands on stderr
    sys.stdout = sys.stderr  # ...including Python's own print()
    return _Channel(os.fdopen(private, "wb", buffering=0))


@dataclass
class Session:
    """What the client told us, and which era it is speaking."""

    #: Set once the client opens with `initialize`. The modern revision has no
    #: handshake, so this stays False for it.
    legacy: bool = False
    version: str = LATEST
    client: dict = field(default_factory=dict)
    cancelled: dict = field(default_factory=dict)  # request id -> Event

    def note_version(self, requested: str | None) -> None:
        if requested:
            self.version = requested


class ProtocolError(Exception):
    """Answered as a JSON-RPC error rather than as a tool failure."""

    def __init__(self, code: int, message: str, data=None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class Server:
    def __init__(self, channel: _Channel, allow_writes: bool = False) -> None:
        from git_assistant.mcp import context, tools

        self.channel = channel
        self.session = Session()
        self.context = context.ToolContext(allow_writes=allow_writes)
        self.tools = tools
        self.pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)

    # ---- the loop --------------------------------------------------------
    def serve(self, stream) -> int:
        """Read messages until the client closes stdin."""
        for raw in iter(stream.readline, b""):
            if not raw.strip():
                continue
            try:
                self.handle(raw)
            except Exception:  # nothing may escape into the client's stdout
                log.exception("dropping a message this server could not handle")
        self.pool.shutdown(wait=False, cancel_futures=True)
        return 0

    def handle(self, raw: bytes) -> None:
        try:
            message = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._fail(None, PARSE_ERROR, f"could not parse the message: {exc}")
            return
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            self._fail(message.get("id") if isinstance(message, dict) else None,
                       INVALID_REQUEST, "a request needs a string 'method'")
            return

        method = message["method"]
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}

        if request_id is None:
            self._notify(method, params)  # never answered, whatever happens
            return

        try:
            self._check_version(method, params)
            if method in ("tools/call",) :
                self._call_tool(request_id, params)
                return
            self._reply(request_id, self._answer(method, params))
        except ProtocolError as exc:
            self._fail(request_id, exc.code, str(exc), exc.data)
        except Exception as exc:
            log.exception("%s failed", method)
            self._fail(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    def _notify(self, method: str, params: dict) -> None:
        if method == "notifications/cancelled":
            event = self.session.cancelled.get(params.get("requestId"))
            if event is not None:
                event.set()
        elif method == "notifications/initialized":
            pass  # the legacy handshake's third leg; nothing to do
        else:
            log.debug("ignoring notification %s", method)

    # ---- versions and eras ------------------------------------------------
    def _check_version(self, method: str, params: dict) -> None:
        """Reject a version we do not speak, before doing any work for it."""
        if method == "initialize":
            return  # handled in _answer: the legacy handshake negotiates
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        requested = meta.get(META_VERSION)
        if requested and requested not in SUPPORTED:
            raise ProtocolError(
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                {"supported": list(SUPPORTED), "requested": requested},
            )
        if meta.get(META_CLIENT_INFO):
            self.session.client = meta[META_CLIENT_INFO]
        self.session.note_version(requested)

    def _answer(self, method: str, params: dict) -> dict:
        if method == "server/discover":
            return {
                "resultType": "complete",
                "supportedVersions": list(SUPPORTED),
                "capabilities": {"tools": {}},
                "instructions": INSTRUCTIONS,
                "_meta": {META_SERVER_INFO: self._info()},
            }
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return {
                "resultType": "complete",
                "tools": self.tools.catalogue(allow_writes=self.context.allow_writes),
            }
        raise ProtocolError(METHOD_NOT_FOUND, f"unknown method: {method}")

    def _initialize(self, params: dict) -> dict:
        """The older era's handshake, kept because every client shipped today uses it."""
        self.session.legacy = True
        wanted = params.get("protocolVersion")
        # Echo what they asked for when we speak it; otherwise name ours and let
        # them decide -- a legacy client has no way to ask a second time.
        self.session.version = wanted if wanted in SUPPORTED else SUPPORTED[1]
        self.session.client = params.get("clientInfo") or {}
        return {
            "protocolVersion": self.session.version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": self._info(),
            "instructions": INSTRUCTIONS,
        }

    def _info(self) -> dict:
        from git_assistant import __version__

        return {"name": SERVER_NAME, "version": __version__}

    # ---- tools ------------------------------------------------------------
    def _call_tool(self, request_id, params: dict) -> None:
        name = params.get("name")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        tool = self.tools.find(name, allow_writes=self.context.allow_writes)
        if tool is None:
            raise ProtocolError(INVALID_PARAMS, self.tools.explain_missing(name))

        bad = self.tools.check_arguments(tool, arguments)
        if bad:
            raise ProtocolError(INVALID_PARAMS, f"{name}: {'; '.join(bad)}")

        event = threading.Event()
        self.session.cancelled[request_id] = event
        token = (params.get("_meta") or {}).get("progressToken")

        def work() -> None:
            try:
                result = self.tools.run(
                    tool,
                    arguments,
                    self.context,
                    is_cancelled=event.is_set,
                    progress=lambda text: self._progress(token, text),
                )
                if not event.is_set():
                    self._reply(request_id, result)
            except Exception as exc:  # a tool failing is an answer, not a crash
                log.exception("tool %s failed", name)
                if not event.is_set():
                    self._reply(request_id, self.tools.failure(f"{type(exc).__name__}: {exc}"))
            finally:
                self.session.cancelled.pop(request_id, None)

        if tool.slow:
            self.pool.submit(work)  # an audit runs for minutes; keep reading
        else:
            work()

    def _progress(self, token, text: str) -> None:
        if token is None:
            log.info("%s", text)
            return
        self.channel.send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/progress",
                "params": {"progressToken": token, "message": text},
            }
        )

    # ---- sending ----------------------------------------------------------
    def _reply(self, request_id, result: dict) -> None:
        self.channel.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _fail(self, request_id, code: int, message: str, data=None) -> None:
        # Reached only for messages that are requests, or that are too
        # malformed to tell -- a well-formed notification returns from handle()
        # before it can get here, because answering one breaks the client.
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.channel.send({"jsonrpc": "2.0", "id": request_id, "error": error})


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--mcp" not in args:
        sys.stderr.write(USAGE)
        return 2
    level = "warning"
    if "--log-level" in args:
        index = args.index("--log-level")
        level = args[index + 1] if index + 1 < len(args) else level
    known = {"--mcp", "--allow-writes", "--log-level", level}
    unknown = [a for a in args if a not in known]
    if unknown:
        sys.stderr.write(f"unknown option(s): {' '.join(unknown)}\n\n{USAGE}")
        return 2

    channel = _claim_stdout()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )
    server = Server(channel, allow_writes="--allow-writes" in args)
    log.info("serving on stdio (writes %s)", "on" if server.context.allow_writes else "off")
    try:
        return server.serve(sys.stdin.buffer)
    finally:
        # This process ends when its client closes the pipe. A background
        # exporter with something left to post would hold it open past that,
        # and the client is no longer there to be waited for.
        from git_assistant import tracing

        tracing.shutdown()
