"""The AI providers the application knows how to name, and what each one needs.

Only some are implemented. The rest are listed because the provider is a choice
the user makes, and a list that appears one entry at a time as each backend
lands tells nobody what is coming; but a listed provider that silently
generated through a different one would be worse than not listing it at all. So
`implemented` is part of the model, the UI shows which is which, and generating
with an unimplemented provider fails with a sentence saying so rather than
quietly using LM Studio.

The remaining two are OpenAI-compatible, so each is configuration plus a test
rather than a new client -- see git_assistant.openai_client.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """A backend that can generate a commit message."""

    #: Stored in settings.json, and the Credential Manager entry name. Never
    #: shown, so it can stay stable while the label is reworded.
    key: str
    #: Shown in the UI.
    label: str
    #: False until a client for it exists.
    implemented: bool = False

    #: Offers an API key field. Kept in the Windows Credential Manager, never
    #: in settings.json -- see git_assistant.credentials.
    needs_api_key: bool = False
    #: Whether a missing key is an error. Separate from `needs_api_key`: a
    #: self-hosted proxy usually wants one and can be run without, so the field
    #: is offered while an empty one still connects.
    api_key_required: bool = True
    #: True when the user supplies the address (a private deployment), False
    #: when the vendor has one fixed address.
    needs_endpoint: bool = False
    #: The address, or the default for a provider whose address the user may
    #: override. Localhost defaults mean the common setup needs no typing.
    base_url: str = ""
    #: Speaks `POST {base}/chat/completions` with `choices[0].message.content`.
    #: LM Studio does too, but keeps its own client for the native endpoint
    #: that reports context length.
    openai_compatible: bool = False
    #: Header the key goes in. Anthropic and Azure do not use `Authorization`.
    auth_header: str = "Authorization"
    #: Shown under the key field, so nobody has to guess where to get one.
    key_help: str = ""
    #: Placeholder for the endpoint field.
    endpoint_hint: str = ""
    #: The agent CLI this provider drives, if it drives one. See
    #: git_assistant.agent_cli -- a local program invoked once per completion
    #: rather than an HTTP endpoint, with its own login and no API key here.
    cli: str = ""
    #: The most requests this backend may have in flight, or 0 for "as many as
    #: the context allows". A cap here is for backends where concurrency is not
    #: merely unhelpful but wrong; see `parallel.effective_parallel`, which is
    #: the one place every fan-out asks.
    max_parallel: int = 0
    #: Shipped, but with limitations a user should know before choosing it.
    #: Separate from `implemented`: this one *works*, and the honest thing is to
    #: say what it costs rather than to hide it or to pretend it is like the
    #: others.
    experimental: bool = False

    def display(self) -> str:
        if not self.implemented:
            return f"{self.label} (not yet available)"
        return f"{self.label} (experimental)" if self.experimental else self.label

    def extra_query(self, settings) -> dict[str, str]:
        """Query parameters every request needs.

        Azure pins the contract with an api-version, and a missing one is a
        confusing 404 rather than a clear error.
        """
        if self.key == "azure-ai-foundry":
            return {"api-version": settings.azure_api_version}
        return {}


PROVIDERS: tuple[Provider, ...] = (
    Provider(
        "lmstudio",
        "LM Studio",
        implemented=True,
        key_help="Runs locally; no API key required.",
    ),
    Provider(
        "claude",
        "Claude",
        implemented=True,
        needs_api_key=True,
        auth_header="x-api-key",
        key_help="Create a key at console.anthropic.com under API Keys.",
    ),
    Provider(
        "openai",
        "OpenAI",
        implemented=True,
        openai_compatible=True,
        needs_api_key=True,
        base_url="https://api.openai.com/v1",
        key_help="Create a key at platform.openai.com under API keys.",
    ),
    Provider(
        "azure-ai-foundry",
        "Azure AI Foundry",
        implemented=True,
        openai_compatible=True,
        needs_api_key=True,
        needs_endpoint=True,
        auth_header="api-key",
        key_help="Key and endpoint are on your deployment's page in the Azure portal.",
        endpoint_hint="https://<resource>.openai.azure.com/openai/v1",
    ),
    Provider(
        "litellm-proxy",
        "Litellm Proxy",
        implemented=True,
        openai_compatible=True,
        needs_api_key=True,
        # A proxy you run yourself may have no auth at all, so an empty key
        # still connects; the field is offered because most deployments issue
        # a virtual key and there would otherwise be nowhere to put it.
        api_key_required=False,
        needs_endpoint=True,
        base_url="http://localhost:4000/v1",
        key_help="Your proxy's virtual key, if it requires one. Leave empty if not.",
        endpoint_hint="http://localhost:4000/v1",
    ),
    Provider(
        "ollama",
        "Ollama",
        implemented=True,
        openai_compatible=True,
        # No key field: Ollama has no authentication of its own. Behind a proxy
        # that adds some, use the Litellm Proxy entry, which has one.
        needs_endpoint=True,
        base_url="http://localhost:11434/v1",
        key_help="Runs locally; no API key required.",
        endpoint_hint="http://localhost:11434/v1",
    ),
    Provider(
        "lemonade",
        "Lemonade Server",
        implemented=True,
        openai_compatible=True,
        # Same reasoning as Ollama: no authentication of its own, so no key
        # field. Its OpenAI-compatible routes live under /api/v1, not /v1.
        needs_endpoint=True,
        base_url="http://localhost:13305/api/v1",
        key_help="Runs locally; no API key required.",
        endpoint_hint="http://localhost:13305/api/v1",
    ),
    # ---- agent CLIs, driven as backends ------------------------------------
    # Installed and logged in separately, so there is no key and no address
    # here. Both are marked experimental for reasons measured rather than
    # assumed; see docs/cli-providers.md.
    Provider(
        "claude-cli",
        "Claude Code CLI",
        implemented=True,
        experimental=True,
        # One at a time. Each call is a whole process, and four of them
        # is four runtimes starting at once for no throughput gain -- the
        # five seconds is start-up, not queueing.
        max_parallel=1,
        cli="claude",
        key_help=(
            "Uses your Claude Code login, so no API key is needed here. "
            "Experimental: about five seconds of process start-up per call, "
            "which a per-file code review pays once per file."
        ),
    ),
    Provider(
        "agy-cli",
        "Antigravity CLI",
        implemented=True,
        experimental=True,
        # One at a time. Each call is a whole process, and four of them
        # is four runtimes starting at once for no throughput gain -- the
        # five seconds is start-up, not queueing.
        max_parallel=1,
        cli="agy",
        key_help=(
            "Uses your Antigravity login, so no API key is needed here. "
            "Experimental: about six seconds of start-up per call, and roughly "
            "17,000 tokens of its own prompt on every call, which it offers no "
            "way to remove."
        ),
    ),
)

#: What a settings file that predates this choice, or names something unknown,
#: falls back to. It is the one that needs no account.
DEFAULT_PROVIDER = "lmstudio"


def get(key: str) -> Provider:
    """The provider for ``key``, falling back to the default.

    Unknown keys resolve rather than raise: a settings file naming a provider
    this build has never heard of -- hand-edited, or written by a newer version
    -- must still start the application.
    """
    for provider in PROVIDERS:
        if provider.key == key:
            return provider
    return get(DEFAULT_PROVIDER) if key != DEFAULT_PROVIDER else PROVIDERS[0]


def is_known(key: str) -> bool:
    return any(provider.key == key for provider in PROVIDERS)
