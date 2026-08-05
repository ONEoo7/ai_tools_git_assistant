"""OpenAI and anything that speaks its chat-completions shape.

One module for both OpenAI and Azure AI Foundry because the wire format is the
same one LM Studio serves: `POST {base}/chat/completions` with `messages`, and
the answer at `choices[0].message.content`. What differs between them is
configuration, not code:

  * **the address** -- OpenAI has one; Azure's is per-resource, so the user
    supplies it;
  * **the header the key goes in** -- `Authorization: Bearer` for OpenAI,
    `api-key` for Azure;
  * **query parameters** -- Azure pins the contract with `api-version`.

Written on httpx rather than the `openai` package, matching the LM Studio
client: the surface used here is two endpoints, and every provider added to
this build is one more thing to declare as a PyInstaller hidden import.
"""

from __future__ import annotations

import httpx

from git_assistant import usage
from git_assistant.llm import LLMError, ModelInfo

CHAT_TIMEOUT = 600.0
LIST_TIMEOUT = 15.0
CONNECT_TIMEOUT = 5.0


class OpenAICompatibleClient:
    """Chat completions against any OpenAI-shaped endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        auth_header: str = "Authorization",
        extra_query: dict[str, str] | None = None,
        chat_timeout: float = CHAT_TIMEOUT,
        list_timeout: float = LIST_TIMEOUT,
        provider_key: str = "openai",
        feature: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # This client serves several providers, so it has to be told which one
        # its usage is filed under -- and what it is being spent on; see
        # git_assistant.usage.
        self.provider_key = provider_key
        self.feature = feature
        self._api_key = api_key
        self._auth_header = auth_header
        # Azure rejects an empty api-version rather than defaulting, so an
        # empty value is dropped instead of being sent as "".
        self._extra_query = {k: v for k, v in (extra_query or {}).items() if v}
        self.chat_timeout = chat_timeout
        self.list_timeout = list_timeout

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        if self._auth_header.lower() == "authorization":
            return {"Authorization": f"Bearer {self._api_key}"}
        return {self._auth_header: self._api_key}

    def _request(self, method: str, path: str, timeout: float, **kwargs):
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(
                timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT),
                headers=self._headers(),
                params=self._extra_query or None,
            ) as client:
                response = client.request(method, url, **kwargs)
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(_explain(exc)) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach {self.base_url}: {exc}") from exc

    # ---- models ------------------------------------------------------------
    def list_models(self) -> list[ModelInfo]:
        """List models. Context length is not part of this API's response.

        `/v1/models` reports ids and nothing about capacity, so the context
        window stays unknown and the user sets it themselves -- which is why
        the Context window size field offers "Auto-detect" rather than assuming
        it.
        """
        payload = self._request("GET", "/models", self.list_timeout)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise LLMError(f"unexpected response listing models: {payload}")
        return [
            ModelInfo(id=str(row.get("id", "")), loaded=True)
            for row in rows
            if isinstance(row, dict) and row.get("id")
        ]

    def context_length_for(self, model_id: str) -> int | None:
        """Always None: this API does not report it. Not an error."""
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
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        payload = self._request(
            "POST", "/chat/completions", self.chat_timeout, json=body
        )
        try:
            text = payload["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMError(f"unexpected response shape: {payload}") from exc
        usage.record_openai_response(
            self.provider_key,
            model,
            payload,
            system=system,
            user=user,
            reply=text,
            feature=self.feature,
        )
        return text

    def ping(self) -> list[ModelInfo]:
        models = self.list_models()
        if not models:
            raise LLMError(f"{self.base_url} returned no models.")
        return models


def _explain(exc: httpx.HTTPStatusError) -> str:
    """Turn a status code into the thing the user has to go and fix."""
    status = exc.response.status_code
    if status in (401, 403):
        return (
            "The API key was rejected. Check the key stored for this provider "
            "in the Connection & Model tab."
        )
    if status == 404:
        return (
            f"Not found at {exc.request.url}. Check the endpoint -- for Azure "
            "it ends with the deployment path, and the api-version must match "
            "the deployment."
        )
    if status == 429:
        return "Rate limit or quota reached. Wait a moment and try again."
    detail = ""
    try:
        body = exc.response.json()
        detail = str(body.get("error", {}).get("message", ""))[:200]
    except Exception:  # noqa: BLE001 - the body is not always JSON
        detail = exc.response.text[:200]
    return f"The provider returned HTTP {status}. {detail}".strip()
