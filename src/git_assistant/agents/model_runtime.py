"""Sizing one chat request for whichever provider is configured.

The same arithmetic ``CommitGenerator`` does -- clamp the configured context to
what the model actually has, reserve room for the answer, subtract the prompt
scaffolding -- with none of its map-reduce machinery, which narration does not
need: an audit is a handful of short sections written one after another.

There is deliberately no cold-model warm-up here. That exists in the commit
generator because it fans several requests out at once and a model still being
loaded serves one and refuses the rest; these calls are serial, so the first one
simply waits for the load like any other.
"""

from __future__ import annotations

from git_assistant.llm import ChatClient, ModelInfo
from git_assistant.tokenizer import input_budget, reserved_output

DEFAULT_CONTEXT_WINDOW = 8192


class ModelRuntime:
    def __init__(self, settings, client: ChatClient) -> None:
        self.settings = settings
        self.client = client
        self._model: ModelInfo | None = None
        self._looked_up = False

    def _report(self) -> ModelInfo | None:
        if not self._looked_up:
            self._looked_up = True
            wanted = self.settings.active_model()
            try:
                self._model = next(
                    (m for m in self.client.list_models() if m.id == wanted), None
                )
            except Exception:
                self._model = None
        return self._model

    def context_window(self) -> int:
        """The window to plan against: never larger than the model really has."""
        model = self._report()
        detected = model.max_context_length if model else None
        if detected is None:
            try:
                detected = self.client.context_length_for(self.settings.active_model())
            except Exception:
                detected = None
        configured = self.settings.context_window
        if configured and configured > 0:
            return min(configured, detected) if detected else configured
        return detected or DEFAULT_CONTEXT_WINDOW

    def reserved_output(self) -> int:
        return reserved_output(self.context_window(), self.settings.safety_margin)

    def budget(self, overhead: int = 0) -> int:
        """Tokens left for the facts once the prompt scaffolding is paid for."""
        return input_budget(self.context_window(), self.reserved_output(), overhead)

    def chat(self, *, system: str, user: str, max_tokens: int) -> str:
        return self.client.chat(
            model=self.settings.active_model(),
            system=system,
            user=user,
            max_tokens=min(max_tokens, self.reserved_output()),
        )
