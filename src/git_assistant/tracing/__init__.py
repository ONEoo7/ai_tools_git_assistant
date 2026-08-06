"""Send every completion to Langfuse, if the user asked for that.

Two records of a model call already exist and neither survives the day: the
**View LLM Calls** pane holds one run in memory, and ``usage`` holds tokens
without a word of what was sent. Langfuse is the durable one -- the prompt, the
reply, the model, the timing and the cost, together and searchable -- which is
why the answer is to send traces to it rather than to grow a third store here.

The whole subsystem hangs off one line in ``llm.build_client``, so a run started
from the tray, from a tab or from the MCP server is traced identically. Off
until it is configured, and unable to fail a generation when it is; see
git_assistant.tracing.tracer.
"""

from __future__ import annotations

from git_assistant.tracing.client import TracingClient
from git_assistant.tracing.settings import (
    CREDENTIAL_KEY,
    PUBLIC_CREDENTIAL_KEY,
    TraceSettings,
    from_settings,
    has_secret,
    public_key,
)
from git_assistant.tracing.tracer import available, check, flush, shutdown, status

__all__ = [
    "CREDENTIAL_KEY",
    "PUBLIC_CREDENTIAL_KEY",
    "TraceSettings",
    "TracingClient",
    "available",
    "check",
    "close",
    "flush",
    "from_settings",
    "has_secret",
    "public_key",
    "shutdown",
    "status",
    "who",
    "wrap",
]


def wrap(client, settings, feature: str = ""):
    """``client``, tracing its completions -- or ``client`` itself.

    Returns the argument untouched when tracing is off or unconfigured, so a
    build nobody has configured behaves exactly as it did before this module
    existed. Never raises: a tracer that could refuse to construct would be a
    tracer that can stop a commit message.
    """
    try:
        config = from_settings(settings)
        if not config.configured():
            return client
        return TracingClient(
            client,
            config,
            name=feature or "LLM run",
            metadata=_about(settings, feature),
            user=who(),
        )
    except Exception:
        return client


def who() -> str:
    """Whoever is running the application, as Langfuse's ``userId``.

    The account the process runs under -- not the git committer, which is a
    property of a repository and can differ per project, and not an email,
    which is more of a person than a trace of a token count needs.

    Empty when the platform will not say. A trace with no user is a trace with
    no user; a trace attributed to ``unknown`` looks like a real account.
    """
    import getpass

    try:
        return (getpass.getuser() or "").strip()
    except Exception:
        # getpass raises rather than returning when there is no login name,
        # which is a normal state for a service account.
        return ""


def _about(settings, feature: str) -> dict:
    """What the trace should say about this run, beyond the calls themselves.

    The repository's *name*, never its path: a path is
    ``D:\\work\\<client>\\...``, and who the user works for is not something
    tracing a code review needs to know.
    """
    from pathlib import Path

    about = {"feature": feature or "unattributed"}
    repo = str(getattr(settings, "active_repo", "") or "")
    if repo:
        about["repository"] = Path(repo).name
    provider = str(getattr(settings, "provider", "") or "")
    if provider:
        about["provider"] = provider
    return about


def close(client) -> None:
    """End the run behind ``client``, whatever kind of client it turned out to be.

    Call sites hold whichever object ``build_client`` handed back -- possibly
    wrapped again by the recorder -- and must not have to know which. A client
    with nothing to close is not an error.
    """
    ending = getattr(client, "close", None)
    if ending is None:
        return
    try:
        ending()
    except Exception:
        pass
