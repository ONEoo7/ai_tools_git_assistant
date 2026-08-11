"""The one Langfuse client, and the promise that it can never break a run.

Everything here is written to the same rule as ``usage.record``: a failure is
silent by design, and the worst case is a missing trace. A commit message that
took forty seconds to generate must not be lost because an observability server
was restarting -- so every entry point catches ``Exception``, the SDK is
imported inside a ``try`` (a build without the package is a working build), and
the client is created on demand rather than at start-up.

The other side of that rule is that silence must not be indistinguishable from
success. ``status()`` is the account this module gives of itself, and the
Advanced tab shows it beside a button that does one real round trip.

Nothing here ever puts the secret key into a status line, a log or an exception.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading

from git_assistant.tracing.settings import TraceSettings

#: What the traces say produced them, as OpenTelemetry's `service.name`. Not
#: filterable in Langfuse -- resource attributes land under
#: `metadata.resourceAttributes` and its documentation says those are not
#: queryable -- so `client.TAG` carries the same answer where it can be searched
#: on. This is here for everything downstream of Langfuse that speaks OTEL.
SERVICE_NAME = "git-assistant"

#: Set once the SDK's loggers have been quietened. The MCP server speaks its
#: protocol on stdout, and a library that logs to a stream nobody configured is
#: one root handler away from corrupting it.
_QUIETENED = False

_LOCK = threading.RLock()
_CLIENT = None
#: What ``_CLIENT`` was built from, so a change in the tab replaces it rather
#: than being ignored until the next restart. The secret is hashed, not held:
#: this value has no business existing in two places.
_BUILT_FROM: tuple | None = None
_STATUS = "Not sending traces."


def available() -> bool:
    """Is the SDK part of this build at all?"""
    return _sdk() is not None


def _sdk():
    try:
        import langfuse
    except Exception:  # ImportError, but also a broken install
        return None
    return langfuse


def _quieten() -> None:
    """Keep the SDK's own logging off any stream this application owns."""
    global _QUIETENED
    if _QUIETENED:
        return
    for name in ("langfuse", "opentelemetry"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        # No handler of our own: a library that emits through the root logger
        # is the caller's business, and the MCP server configures its own.
        logger.propagate = False
    _QUIETENED = True


def _fingerprint(config: TraceSettings) -> tuple:
    return (
        config.host,
        config.public_key,
        hashlib.sha256(config.secret_key.encode("utf-8")).hexdigest(),
        config.environment,
        config.release,
    )


def status() -> str:
    """The last thing that happened, in a sentence. Never contains a key."""
    return _STATUS


def _say(message: str) -> None:
    global _STATUS
    _STATUS = message


def client(config: TraceSettings):
    """The client for this configuration, or ``None``. Never raises.

    Rebuilt when the configuration changes and shut down when it stops being
    usable, so unticking the box in the tab actually stops the exporter rather
    than leaving a thread posting to an address the user has moved on from.
    """
    global _CLIENT, _BUILT_FROM

    if not config.configured():
        shutdown()
        _say(config.missing())
        return None

    langfuse = _sdk()
    if langfuse is None:
        shutdown()
        _say("This build does not include the Langfuse SDK.")
        return None

    wanted = _fingerprint(config)
    with _LOCK:
        if _CLIENT is not None and _BUILT_FROM == wanted:
            return _CLIENT
        shutdown()
        _quieten()
        _name_the_service()
        try:
            from git_assistant import net

            _CLIENT = langfuse.Langfuse(
                public_key=config.public_key,
                secret_key=config.secret_key,
                host=config.host,
                environment=config.environment,
                release=config.release or _version(),
                tracing_enabled=True,
                # A self-hosted Langfuse behind the same corporate proxy fails
                # to verify for the same reason everything else does. See
                # git_assistant.net.
                httpx_client=net.http_client(),
            )
        except Exception as exc:
            _CLIENT = None
            _BUILT_FROM = None
            _say(f"Could not start tracing: {_reason(exc, config.secret_key)}")
            return None
        _BUILT_FROM = wanted
        _say(f"Sending traces to {config.host}.")
        return _CLIENT


def _name_the_service() -> None:
    """Say which application these traces came from.

    Through the environment because that is where OpenTelemetry reads the
    resource from, and the SDK builds its own provider. ``setdefault``: someone
    running this under a collector that already names the service means it, and
    a library overruling that would be the library being wrong.
    """
    os.environ.setdefault("OTEL_SERVICE_NAME", SERVICE_NAME)


def _version() -> str:
    try:
        from git_assistant import __version__

        return __version__
    except Exception:
        return ""


def check(config: TraceSettings) -> tuple[bool, str]:
    """One real round trip, for the Test button. Returns ``(ok, message)``."""
    if not config.configured():
        return False, config.missing()
    built = client(config)
    if built is None:
        return False, status()
    try:
        ok = bool(built.auth_check())
    except Exception as exc:
        message = f"Could not reach {config.host}: {_reason(exc, config.secret_key)}"
        _say(message)
        return False, message
    if not ok:
        # Deliberately not "your key is wrong": which of the two keys, or the
        # host, is a guess, and the server did not say.
        message = f"{config.host} refused these credentials."
        _say(message)
        return False, message
    message = f"Connected to {config.host}."
    _say(message)
    return True, message


def flush() -> None:
    """Push whatever is buffered. Never raises, never blocks for long."""
    with _LOCK:
        if _CLIENT is None:
            return
        try:
            _CLIENT.flush()
        except Exception:
            pass


def shutdown() -> None:
    """Stop the exporter and forget the client. Safe to call at any time.

    Called when the configuration changes, and by the MCP server before it
    exits: a stdio server ends when its client closes the pipe, and must not be
    held open by a background thread with something left to post.
    """
    global _CLIENT, _BUILT_FROM
    with _LOCK:
        stale, _CLIENT, _BUILT_FROM = _CLIENT, None, None
    if stale is None:
        return
    try:
        stale.shutdown()
    except Exception:
        pass


#: Stands in for the secret wherever an error quoted it back.
REDACTED = "***"


def _reason(exc: Exception, secret: str = "") -> str:
    """An exception as one short line, with the secret taken out of it.

    An HTTP client can put the whole request -- headers included -- into
    ``str(exc)``, and this string is shown in the tab. Removing the secret is
    cheap and certain; hoping no library ever includes it is neither.
    """
    first = str(exc).strip().splitlines()
    text = first[0] if first else ""
    if secret:
        text = text.replace(secret, REDACTED)
    return f"{type(exc).__name__}: {text[:160]}" if text else type(exc).__name__
