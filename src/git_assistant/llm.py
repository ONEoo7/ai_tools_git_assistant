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

    def label(self) -> str:
        if self.max_context_length:
            state = "loaded" if self.loaded else "available"
            return f"{self.id}  ({self.max_context_length:,} ctx, {state})"
        return self.id


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
        temperature: float = 0.2,
    ) -> str: ...

    def ping(self) -> list[ModelInfo]: ...


def build_client(settings) -> ChatClient:
    """The client for the provider the user selected.

    Imports inside the branches: the Claude client needs the `anthropic`
    package, and a build or a checkout without it must still run LM Studio
    rather than failing at import time for a provider nobody selected.
    """
    from git_assistant import providers

    provider = providers.get(settings.provider)
    if not provider.implemented:
        raise LLMError(
            f"{provider.label} is not available yet. Choose another provider "
            "in the Connection & Model tab."
        )

    if provider.key == "lmstudio":
        from git_assistant.lmstudio_client import LMStudioClient

        return LMStudioClient(settings.base_url, provider_key=provider.key)

    if provider.key == "claude":
        from git_assistant.claude_client import ClaudeClient

        return ClaudeClient(api_key=_require_key(provider), provider_key=provider.key)

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
        )

    raise LLMError(f"no client is wired up for {provider.label}")


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
    endpoint = (settings.provider_endpoint(provider.key) or provider.base_url).strip()
    if not endpoint:
        raise LLMError(
            f"No endpoint is configured for {provider.label}. Enter the one "
            "from your deployment in the Connection & Model tab."
        )
    return endpoint.rstrip("/")
