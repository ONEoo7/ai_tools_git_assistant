"""Anthropic's Messages API.

Not an OpenAI-compatible endpoint, which is why this is a module rather than a
base-URL entry beside OpenAI's. Four differences the shared client cannot
absorb:

  * the system prompt is a top-level parameter, not a message with a "system"
    role;
  * `max_tokens` is required;
  * the response `content` is a list of typed blocks, so the text has to be
    gathered from the ones of type "text" rather than read out of
    `choices[0].message.content`;
  * `temperature` is *rejected* on current models -- Opus 5 and Sonnet 5 return
    a 400 for it. The generator passes one because LM Studio wants it, so it is
    accepted here and dropped.
"""

from __future__ import annotations

from git_assistant import usage
from git_assistant.llm import LLMError, ModelInfo

#: Used when the user has not chosen a model. The current Opus is the default
#: rather than a cheaper tier: which model to pay for is the user's call, and
#: they can pick any of the others from the dropdown.
DEFAULT_MODEL = "claude-opus-5"

#: Requests can be long -- a large diff split into chunks is many calls, and one
#: call on a big chunk is not fast. Matches the LM Studio client's ceiling.
CHAT_TIMEOUT = 600.0
LIST_TIMEOUT = 15.0


def _sdk():
    """Import the SDK, turning its absence into something a user can act on."""
    try:
        import anthropic
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise LLMError(
            "The 'anthropic' package is not installed, so the Claude provider "
            "cannot be used in this build. Install it with: pip install anthropic"
        ) from exc
    return anthropic


class ClaudeClient:
    """Talks to the Messages API. Same four methods as every other provider."""

    def __init__(
        self,
        api_key: str,
        timeout: float = CHAT_TIMEOUT,
        provider_key: str = "claude",
        feature: str = "",
    ) -> None:
        self._api_key = api_key
        #: Which provider the recorded usage is filed under, and what it is
        #: being spent on; see git_assistant.usage.
        self.provider_key = provider_key
        self.feature = feature
        self._timeout = timeout

    def _client(self, timeout: float):
        anthropic = _sdk()
        return anthropic.Anthropic(api_key=self._api_key, timeout=timeout)

    # ---- models ------------------------------------------------------------
    def list_models(self) -> list[ModelInfo]:
        """Every model the key can reach, with its real context window.

        `max_input_tokens` comes straight from the API, so unlike LM Studio
        there is no second endpoint to consult and no guessing.
        """
        anthropic = _sdk()
        try:
            with self._client(LIST_TIMEOUT) as client:
                return [
                    ModelInfo(
                        id=model.id,
                        max_context_length=getattr(model, "max_input_tokens", None),
                        loaded=True,  # hosted: nothing to load, everything is ready
                    )
                    for model in client.models.list()
                ]
        except anthropic.AuthenticationError as exc:
            raise LLMError(
                "Anthropic rejected the API key. Check the key stored for "
                "Claude in the Connection & Model tab."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic returned an error: {exc}") from exc

    def context_length_for(self, model_id: str) -> int | None:
        for model in self.list_models():
            if model.id == model_id:
                return model.max_context_length
        return None

    # ---- chat --------------------------------------------------------------
    def chat(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float = 0.2,
    ) -> str:
        """One completion, returned as plain text.

        ``temperature`` is accepted and ignored -- see the module docstring.
        """
        anthropic = _sdk()
        try:
            with self._client(self._timeout) as client:
                message = client.messages.create(
                    model=model or DEFAULT_MODEL,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                )
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic rejected the API key.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(
                "Anthropic rate limit reached. Wait a moment and try again."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic returned an error: {exc}") from exc

        text = _text_of(message)
        # Anthropic reports its own count, and that is the one on the bill, so
        # it is preferred over anything measured here.
        counted = getattr(message, "usage", None)
        if counted is not None:
            usage.record(
                self.provider_key,
                model or DEFAULT_MODEL,
                getattr(counted, "input_tokens", 0),
                getattr(counted, "output_tokens", 0),
                feature=self.feature,
            )
        else:
            estimated = usage.estimate(system=system, user=user, reply=text)
            usage.record(
                self.provider_key,
                model or DEFAULT_MODEL,
                *estimated,
                feature=self.feature,
                estimated=True,
            )
        return text

    def ping(self) -> list[ModelInfo]:
        models = self.list_models()
        if not models:
            raise LLMError("The Anthropic API returned no models for this key.")
        return models


def _text_of(message) -> str:
    """Join the text blocks of a response.

    `content` is a list of typed blocks, and a response may carry thinking
    blocks alongside the answer, so the type has to be checked rather than
    taking the first block. `stop_reason` is inspected first: a refusal returns
    HTTP 200 with empty or partial content, and reading it as an answer would
    hand the user a blank commit message with no explanation.
    """
    if getattr(message, "stop_reason", None) == "refusal":
        raise LLMError(
            "Claude declined to generate a message for this diff. This is "
            "usually a false positive on security-adjacent code; try a "
            "different provider or a smaller diff."
        )

    parts = [
        block.text
        for block in getattr(message, "content", [])
        if getattr(block, "type", None) == "text"
    ]
    text = "".join(parts).strip()
    if not text:
        raise LLMError(
            f"Claude returned no text (stop reason: "
            f"{getattr(message, 'stop_reason', 'unknown')})."
        )
    return text
