"""HTTP client for LM Studio's local server.

LM Studio exposes an OpenAI-compatible API under ``/v1`` and a richer native API
under ``/api/v0``. We prefer the native ``/api/v0/models`` endpoint because it
reports each model's ``max_context_length`` (and load state), which lets us size
the token budget automatically. We fall back to ``/v1/models`` when the native
endpoint is unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class LMStudioError(RuntimeError):
    """Raised when the LM Studio server cannot be reached or returns an error."""


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


class LMStudioClient:
    def __init__(
        self,
        base_url: str,
        list_timeout: float = 8.0,
        chat_timeout: float = 600.0,
        connect_timeout: float = 3.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # Listing models must fail fast so the UI never appears to hang.
        # Chat completions may legitimately take a long time to generate.
        self.list_timeout = list_timeout
        self.chat_timeout = chat_timeout
        self.connect_timeout = connect_timeout

    def _list_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(self.list_timeout, connect=self.connect_timeout)
        )

    # ---- models ------------------------------------------------------------
    def list_models(self) -> list[ModelInfo]:
        """List models, preferring the native endpoint for context length.

        Both the native and OpenAI-compatible endpoints are attempted with a
        short timeout. If both fail, a single actionable error is raised.
        """
        try:
            return self._list_models_native()
        except httpx.TransportError as exc:
            # Cannot connect at all; the OpenAI endpoint is the same host:port,
            # so retrying would only burn another connect timeout. Fail now.
            raise self._unreachable_error(exc) from exc
        except Exception as native_exc:
            # Server was reachable but the native endpoint failed (e.g. an older
            # LM Studio without /api/v0). Fall back to the OpenAI-compatible one.
            try:
                return self._list_models_openai()
            except LMStudioError as openai_exc:
                raise self._unreachable_error(openai_exc) from openai_exc

    def _unreachable_error(self, exc: Exception) -> LMStudioError:
        return LMStudioError(
            f"Could not reach LM Studio at {self.base_url}. "
            "Make sure the local server is running "
            "(LM Studio -> Developer -> Start Server) and the IP/port match. "
            f"[{type(exc).__name__}: {exc}]"
        )

    def _list_models_native(self) -> list[ModelInfo]:
        with self._list_client() as client:
            resp = client.get(f"{self.base_url}/api/v0/models")
            resp.raise_for_status()
            payload = resp.json()
        models: list[ModelInfo] = []
        for item in payload.get("data", []):
            # Only language models are useful for chat completions.
            if item.get("type") not in (None, "llm", "vlm"):
                continue
            models.append(
                ModelInfo(
                    id=item.get("id", ""),
                    max_context_length=item.get("max_context_length")
                    or item.get("loaded_context_length"),
                    loaded=item.get("state") == "loaded",
                )
            )
        if not models:
            raise LMStudioError("no language models returned by /api/v0/models")
        return models

    def _list_models_openai(self) -> list[ModelInfo]:
        try:
            with self._list_client() as client:
                resp = client.get(f"{self.base_url}/v1/models")
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise LMStudioError(str(exc)) from exc
        return [ModelInfo(id=item.get("id", "")) for item in payload.get("data", [])]

    def context_length_for(self, model_id: str) -> int | None:
        """Return the reported context window for a model, if known."""
        for m in self.list_models():
            if m.id == model_id:
                return m.max_context_length
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
        """Run a single chat completion and return the assistant text."""
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
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.chat_timeout, connect=self.connect_timeout)
            ) as client:
                resp = client.post(f"{self.base_url}/v1/chat/completions", json=body)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise LMStudioError(f"chat completion failed: {exc}") from exc

        try:
            return payload["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as exc:
            raise LMStudioError(f"unexpected response shape: {payload}") from exc

    def ping(self) -> list[ModelInfo]:
        """Test connectivity by listing models; raises LMStudioError on failure."""
        models = self.list_models()
        if not models:
            raise LMStudioError("connected, but no models are available")
        return models
