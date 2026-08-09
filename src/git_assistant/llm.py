"""What every provider has to supply, and how one is chosen.

`CommitGenerator` uses four things from a backend: list the models, report a
model's context window, run one chat completion, and confirm the connection
works. That is the whole contract, and it is small on purpose -- adding a
provider should mean writing those four methods, not touching the generator.

`ModelInfo` and the error type live here rather than in one provider's module
so a second provider does not have to import from the first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LLMError(RuntimeError):
    """A provider could not be reached, or answered with something unusable.

    Surfaced verbatim in the UI, so the message is written for the person
    reading it. Never include an API key in one.
    """


@dataclass
class ModelInfo:
    id: str
    max_context_length: int | None = None
    loaded: bool = False
    #: Something true about this model that is not part of its name -- what an
    #: alias last resolved to, for instance. Shown, never sent: `id` is what
    #: goes to the provider. See git_assistant.agent_cli.resolved.
    note: str = ""

    def label(self) -> str:
        if self.max_context_length:
            state = "loaded" if self.loaded else "available"
            return f"{self.id}  ({self.max_context_length:,} ctx, {state})"
        return f"{self.id}  ({self.note})" if self.note else self.id


class ChatClient(Protocol):
    """The four methods a provider must implement."""

    def list_models(self) -> list[ModelInfo]: ...

    def context_length_for(self, model_id: str) -> int | None: ...

    def chat(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        #: ``None`` means "whatever this client was configured with"; see
        #: Settings.temperature_for. A caller that names one overrules it.
        temperature: float | None = None,
    ) -> str: ...

    def ping(self) -> list[ModelInfo]: ...


def _endpoint(settings, provider) -> str:
    """Where this backend is: what the settings say, else the provider's own.

    The settings are the repository's -- a project can pin the address of the
    server it is meant to be generated against. ``settings`` here is always a
    ``repo_config.Bound``; see git_assistant.ui.workers.
    """
    chosen = getattr(settings, "provider_endpoint", None)
    chosen = chosen(provider.key) if callable(chosen) else ""
    return (chosen or provider.base_url).strip()


def build_client(settings, feature: str = "") -> ChatClient:
    """The client for the provider the user selected.

    Imports inside the branches: the Claude client needs the `anthropic`
    package, and a build or a checkout without it must still run LM Studio
    rather than failing at import time for a provider nobody selected.

    ``feature`` is what the completions will be spent on -- a commit message, a
    code review, an audit. It is recorded with the usage, and here is the last
    place it is known: by the time an answer comes back there is only a provider
    and a model. See git_assistant.usage.

    One call to this function is one run, so it is also where a Langfuse trace
    begins -- see git_assistant.tracing. With tracing off, the client returned
    is exactly the provider's own.
    """
    from git_assistant import providers

    provider = providers.get(settings.provider)
    if not provider.implemented:
        raise LLMError(
            f"{provider.label} is not available yet. Choose another provider "
            "in the Connection & Model tab."
        )

    return _traced(_provider_client(settings, provider, feature), settings, feature)


def _provider_client(settings, provider, feature: str) -> ChatClient:
    # What this model was configured to run at. Given to the client rather than
    # to each call, so every caller -- the generator, the reviewer, the audit --
    # gets it without having to know it exists, and a caller that needs
    # something else for one call can still say so.
    warmth = settings.temperature_for(provider.key, settings.provider_model(provider.key))

    if provider.cli:
        # A local program, not an endpoint: no key, no address, and its own
        # login. Neither CLI accepts a temperature, so `warmth` is not passed --
        # sending one silently ignored would be worse than not offering it.
        from git_assistant.agent_cli import CliClient

        return CliClient(provider.cli, provider_key=provider.key, feature=feature)

    if provider.key == "lmstudio":
        from git_assistant.lmstudio_client import LMStudioClient

        return LMStudioClient(
            _endpoint(settings, provider),
            provider_key=provider.key,
            feature=feature,
            temperature=warmth,
        )

    if provider.key == "claude":
        from git_assistant.claude_client import ClaudeClient

        return ClaudeClient(
            api_key=_require_key(provider),
            provider_key=provider.key,
            feature=feature,
        )

    if provider.openai_compatible:
        from git_assistant.openai_client import OpenAICompatibleClient

        return OpenAICompatibleClient(
            base_url=_require_endpoint(settings, provider),
            api_key=_require_key(provider),
            auth_header=provider.auth_header,
            extra_query=provider.extra_query(settings),
            # One client, several providers: usage has to be filed under the
            # one that was actually asked, not under "openai" for all of them.
            provider_key=provider.key,
            feature=feature,
            temperature=warmth,
        )

    raise LLMError(f"no client is wired up for {provider.label}")


def _traced(client: ChatClient, settings, feature: str) -> ChatClient:
    """The client, filing its completions with Langfuse if that is configured.

    Imported here rather than at module level: the SDK is a heavy import for a
    subsystem most installations never turn on, and a build without it must
    still generate commit messages.
    """
    try:
        from git_assistant import tracing
    except Exception:
        return client
    return tracing.wrap(client, settings, feature)


def _require_key(provider) -> str:
    """The stored key, or "" for a provider that can run without one.

    A missing key is reported here rather than as a 401 from the provider: it
    is a configuration answer, and the user should be sent to the field rather
    than to the network.
    """
    if not provider.needs_api_key:
        return ""

    from git_assistant import credentials

    key = credentials.get_secret(provider.key) or ""
    if not key and provider.api_key_required:
        raise LLMError(
            f"No API key is stored for {provider.label}. Add one in the "
            "Connection & Model tab; it is kept in the Windows Credential "
            "Manager, not in settings.json."
        )
    return key


def _require_endpoint(settings, provider) -> str:
    endpoint = _endpoint(settings, provider)
    if not endpoint:
        raise LLMError(
            f"No endpoint is configured for {provider.label}. Enter the one "
            "from your deployment in the Connection & Model tab."
        )
    return endpoint.rstrip("/")
