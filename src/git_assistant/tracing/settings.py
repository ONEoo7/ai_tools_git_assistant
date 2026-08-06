"""What the user configured, gathered into one object.

Kept apart from the tracer so that reading the configuration -- which touches
the Credential Manager -- cannot be confused with acting on it, and so the UI
can ask "is this usable yet" without building a client or opening a socket.

Neither key is in ``settings.json``. Both live in the Windows Credential
Manager beside the provider API keys; see git_assistant.credentials for why.

Langfuse's *public* key would be defensible in a settings file -- it is the half
of the pair their own documentation puts in browser code -- but keeping the two
halves in one place is worth more than that argument. Credentials belong in the
credential store; a rule with an exception in it is a rule nobody can check.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The Credential Manager entries, as `git-assistant:langfuse` and
#: `git-assistant:langfuse-public`. Not a provider, but the same namespace and
#: the same store: one place the user can look for everything this application
#: has been trusted with.
CREDENTIAL_KEY = "langfuse"
PUBLIC_CREDENTIAL_KEY = "langfuse-public"

DEFAULT_ENVIRONMENT = "development"


@dataclass(frozen=True)
class TraceSettings:
    """Everything needed to send a trace, and whether it adds up to enough."""

    enabled: bool = False
    host: str = ""
    public_key: str = ""
    secret_key: str = ""
    environment: str = DEFAULT_ENVIRONMENT
    release: str = ""
    #: Whether the prompt and the reply travel with the trace. Off, a span
    #: still carries the model, the timing, the tokens and any error -- which
    #: is everything except the source code.
    send_prompts: bool = True

    def configured(self) -> bool:
        return bool(self.enabled and self.host and self.public_key and self.secret_key)

    def missing(self) -> str:
        """Why this is not usable yet, in the words of the field to go and fill.

        One thing at a time and in the order of the form: a message naming
        three empty fields is read as three problems.
        """
        if not self.enabled:
            return "Not sending traces."
        for value, name in (
            (self.host, "host"),
            (self.public_key, "public key"),
            (self.secret_key, "secret key"),
        ):
            if not value:
                return f"Enabled, but no {name} is set yet."
        return ""


def from_settings(settings) -> TraceSettings:
    """Read the configuration, both keys included. Never raises.

    A credential store that refuses to answer is the same as one holding no
    key: tracing does not start, and the tab says which field is empty.
    """
    return TraceSettings(
        enabled=bool(getattr(settings, "langfuse_enabled", False)),
        host=str(getattr(settings, "langfuse_host", "") or "").strip().rstrip("/"),
        public_key=_stored(PUBLIC_CREDENTIAL_KEY),
        secret_key=_secret(),
        environment=str(
            getattr(settings, "langfuse_environment", "") or DEFAULT_ENVIRONMENT
        ).strip(),
        release=str(getattr(settings, "langfuse_release", "") or "").strip(),
        send_prompts=bool(getattr(settings, "langfuse_send_prompts", True)),
    )


def _stored(key: str) -> str:
    from git_assistant import credentials

    try:
        return (credentials.get_secret(key) or "").strip()
    except Exception:
        return ""


def _secret() -> str:
    return _stored(CREDENTIAL_KEY)


def has_secret() -> bool:
    """Asked by the UI, which must never read the value itself."""
    return bool(_secret())


def public_key() -> str:
    """The stored public key.

    Returned rather than merely counted, unlike the secret: this half is public
    -- Langfuse's own documentation puts it in browser code -- so the tab shows
    which one is configured instead of asking the user to remember.
    """
    return _stored(PUBLIC_CREDENTIAL_KEY)
