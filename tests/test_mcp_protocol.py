"""The wire: framing, eras, errors, and what must never be answered."""

import io
import json

import pytest

from git_assistant.mcp import LATEST, SUPPORTED, server as srv

META = {"_meta": {"io.modelcontextprotocol/protocolVersion": LATEST}}


class Sink:
    """Stands in for the private stdout handle."""

    def __init__(self):
        self.written = b""

    def write(self, data):
        self.written += data

    def flush(self):
        pass

    def lines(self):
        return [line for line in self.written.split(b"\n") if line]

    def messages(self):
        return [json.loads(line) for line in self.lines()]


def run(*messages, allow_writes=False):
    """Feed messages in, collect what came back out."""
    sink = Sink()
    server = srv.Server(srv._Channel(sink), allow_writes=allow_writes)
    stream = io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages))
    code = server.serve(stream)
    return code, sink


def ask(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or META}


# ---- framing ------------------------------------------------------------------
def test_every_answer_is_one_line_of_json():
    _code, sink = run(ask("server/discover"), ask("tools/list", request_id=2))
    assert len(sink.lines()) == 2
    assert b"\r" not in sink.written
    for message in sink.messages():
        assert message["jsonrpc"] == "2.0"


def test_the_stream_is_utf8_whatever_the_console_is():
    _code, sink = run(ask("server/discover"))
    sink.written.decode("utf-8")  # must not raise


def test_closing_stdin_ends_the_server():
    code, sink = run()
    assert code == 0
    assert sink.lines() == []


def test_blank_lines_are_ignored():
    sink = Sink()
    server = srv.Server(srv._Channel(sink))
    assert server.serve(io.BytesIO(b"\n\n  \n")) == 0
    assert sink.lines() == []


# ---- discovery and eras --------------------------------------------------------
def test_discover_reports_versions_capabilities_and_identity():
    _code, sink = run(ask("server/discover"))
    result = sink.messages()[0]["result"]

    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == list(SUPPORTED)
    assert "tools" in result["capabilities"]
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "git-assistant"


def test_the_older_handshake_still_works():
    """Every client shipped today opens with initialize, not with discover."""
    _code, sink = run(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "x"}},
        }
    )
    result = sink.messages()[0]["result"]

    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "git-assistant"


def test_an_unknown_handshake_version_is_answered_with_one_we_speak():
    """A legacy client cannot ask twice, so it must get something usable."""
    _code, sink = run(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "1899-01-01"}}
    )
    assert sink.messages()[0]["result"]["protocolVersion"] in SUPPORTED


def test_an_unsupported_version_says_which_ones_are_supported():
    _code, sink = run(
        ask("tools/list", {"_meta": {"io.modelcontextprotocol/protocolVersion": "1900-01-01"}})
    )
    error = sink.messages()[0]["error"]

    assert error["code"] == srv.UNSUPPORTED_PROTOCOL_VERSION
    assert error["data"]["supported"] == list(SUPPORTED)
    assert error["data"]["requested"] == "1900-01-01"


def test_a_request_without_a_version_is_still_served():
    """Be liberal: the version is optional in practice and refusing helps nobody."""
    _code, sink = run(ask("tools/list", {}))
    assert "result" in sink.messages()[0]


# ---- notifications --------------------------------------------------------------
def test_a_notification_is_never_answered():
    """Answering one is the classic way a hand-written server breaks a client."""
    _code, sink = run({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert sink.lines() == []


def test_an_unknown_notification_is_not_answered_either():
    _code, sink = run({"jsonrpc": "2.0", "method": "notifications/whatever"})
    assert sink.lines() == []


def test_a_message_with_no_method_is_answered_with_a_null_id():
    """Not a notification -- there is no method to tell one by. JSON-RPC says
    an invalid request is answered, with a null id when none can be read."""
    _code, sink = run({"jsonrpc": "2.0", "params": {}})
    assert sink.messages()[0]["error"]["code"] == srv.INVALID_REQUEST
    assert sink.messages()[0]["id"] is None


# ---- errors ----------------------------------------------------------------------
def test_unparseable_input_is_a_parse_error():
    sink = Sink()
    server = srv.Server(srv._Channel(sink))
    server.serve(io.BytesIO(b"{not json\n"))
    assert sink.messages()[0]["error"]["code"] == srv.PARSE_ERROR


def test_an_unknown_method_says_so():
    _code, sink = run(ask("nope"))
    assert sink.messages()[0]["error"]["code"] == srv.METHOD_NOT_FOUND


def test_an_unknown_tool_is_an_invalid_parameter():
    _code, sink = run(ask("tools/call", {"name": "nope", "arguments": {}, **META}))
    error = sink.messages()[0]["error"]
    assert error["code"] == srv.INVALID_PARAMS
    assert "unknown tool" in error["message"]


def test_bad_arguments_name_the_offending_field():
    _code, sink = run(
        ask("tools/call", {"name": "run_agent", "arguments": {"agent": "nonsense"}, **META})
    )
    assert "must be one of" in sink.messages()[0]["error"]["message"]


def test_a_missing_required_argument_is_reported():
    _code, sink = run(ask("tools/call", {"name": "run_agent", "arguments": {}, **META}))
    assert "'agent' is required" in sink.messages()[0]["error"]["message"]


def test_a_tool_that_raises_becomes_a_failed_result_not_a_crash(monkeypatch):
    from git_assistant.mcp import tools

    def boom(*_a, **_kw):
        raise RuntimeError("the disk caught fire")

    monkeypatch.setattr(tools.TOOLS[0], "run", boom)
    _code, sink = run(ask("tools/call", {"name": "list_repos", "arguments": {}, **META}))

    result = sink.messages()[0]["result"]
    assert result["isError"] is True
    assert "the disk caught fire" in result["content"][0]["text"]


def test_one_bad_message_does_not_end_the_session():
    code, sink = run(
        {"jsonrpc": "2.0", "id": 1, "method": 42},  # method is not a string
        ask("server/discover", request_id=2),
    )
    assert code == 0
    assert len(sink.lines()) == 2
    assert "result" in sink.messages()[1]


# ---- tools/list -------------------------------------------------------------------
def test_write_tools_are_invisible_without_the_flag():
    _code, sink = run(ask("tools/list"))
    names = [t["name"] for t in sink.messages()[0]["result"]["tools"]]

    assert "commit" not in names
    assert "list_repos" in names


def test_write_tools_appear_when_the_server_was_started_for_them():
    _code, sink = run(ask("tools/list"), allow_writes=True)
    names = [t["name"] for t in sink.messages()[0]["result"]["tools"]]

    assert {"commit", "push", "create_tag", "push_tag", "switch_branch"} <= set(names)


def test_calling_a_gated_tool_names_the_setting_that_enables_it():
    _code, sink = run(ask("tools/call", {"name": "push", "arguments": {}, **META}))
    message = sink.messages()[0]["error"]["message"]

    assert "write access" in message
    assert "MCP Server tab" in message


# ---- cancellation -------------------------------------------------------------------
def test_a_cancelled_request_gets_no_answer(monkeypatch):
    from git_assistant.mcp import tools

    def slow(ctx, args, *, progress, is_cancelled):
        while not is_cancelled():
            pass
        return tools.text("should never be sent")

    monkeypatch.setattr(tools.TOOLS[0], "run", slow)
    monkeypatch.setattr(tools.TOOLS[0], "slow", True)
    _code, sink = run(
        ask("tools/call", {"name": "list_repos", "arguments": {}, **META}),
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
    )
    assert sink.lines() == []


# ---- argv ---------------------------------------------------------------------------
def test_the_server_refuses_to_start_without_its_flag(capsys):
    assert srv.main([]) == 2
    assert "--mcp" in capsys.readouterr().err


def test_an_unknown_option_is_refused(capsys):
    assert srv.main(["--mcp", "--wat"]) == 2
    assert "--wat" in capsys.readouterr().err


@pytest.mark.parametrize("flag,expected", [([], False), (["--allow-writes"], True)])
def test_the_write_flag_is_what_opens_the_write_tools(flag, expected):
    server = srv.Server(srv._Channel(Sink()), allow_writes="--allow-writes" in flag)
    assert server.context.allow_writes is expected
