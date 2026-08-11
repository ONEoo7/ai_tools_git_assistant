"""A record of what was actually sent to the model, and what came back.

Generation is several calls -- one per chunk of a large diff, sometimes a round
of condensing, then one that writes the message -- and when the result is
disappointing the question is always which of them to blame. Guessing from the
final answer cannot tell a model that summarised badly from a prompt that
arrived truncated.

The recorder wraps whichever provider client is configured and notes every
call as it happens. It changes nothing about the request: it is the same client
underneath, so what is recorded is what was sent.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from git_assistant.tokenizer import estimate_tokens

#: What a call was for. Phases are named, not numbered, because a run's shape
#: depends on the diff: one call, or fifteen.
SINGLE = "single-shot"
MAP = "summarizing a chunk"
REDUCE = "condensing notes"
FINAL = "writing the message"
REVIEW = "reviewing a file"
JUDGE = "scoring a review"
NARRATE = "writing a section"


@dataclass
class LlmCall:
    """One request and its answer, exactly as they went over the wire."""

    index: int
    phase: str
    model: str
    system: str
    user: str
    max_tokens: int
    response: str = ""
    error: str = ""
    seconds: float = 0.0
    started_at: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.error

    def prompt_tokens(self) -> int:
        return estimate_tokens(self.system) + estimate_tokens(self.user)

    def response_tokens(self) -> int:
        return estimate_tokens(self.response)

    def summary(self) -> str:
        if self.error:
            return f"{self.phase} — failed: {self.error[:80]}"
        return (
            f"{self.phase} — {self.prompt_tokens():,} in / "
            f"{self.response_tokens():,} out, {self.seconds:.1f}s"
        )

    def transcript(self) -> str:
        """The whole exchange as text, for reading, copying or exporting."""
        lines = [
            f"### Call {self.index}: {self.phase}",
            f"model: {self.model}    max_tokens: {self.max_tokens:,}    "
            f"took: {self.seconds:.1f}s",
            f"prompt: ~{self.prompt_tokens():,} tokens    "
            f"response: ~{self.response_tokens():,} tokens",
            "",
            "--- SYSTEM ---",
            self.system,
            "",
            f"--- USER ({len(self.user):,} characters) ---",
            self.user,
            "",
            "--- RESPONSE ---",
            self.error or self.response,
        ]
        return "\n".join(lines)


class RecordingClient:
    """A chat client that keeps a copy of every exchange.

    Implements the same four methods as any provider client and forwards all of
    them, so nothing downstream can tell the difference -- which is the point:
    a recorder that changed the request would be recording something else.
    """

    def __init__(self, inner, on_call: Callable[[LlmCall], None] | None = None) -> None:
        self._inner = inner
        self._on_call = on_call
        self._lock = threading.Lock()
        self.calls: list[LlmCall] = []
        self.phase = SINGLE  # through the setter, so the client below hears it too

    @property
    def phase(self) -> str:
        """What the next call is for.

        Set by the generator around each phase; map chunks run on several
        threads at once, so it is read under the same lock that hands out call
        numbers.

        Written through to the client underneath when that client keeps a phase
        of its own -- the tracer does, and a Langfuse trace of fifteen spans all
        called "completion" is a list rather than a story.
        """
        return self._phase

    @phase.setter
    def phase(self, value: str) -> None:
        self._phase = value
        if hasattr(self._inner, "phase"):
            try:
                self._inner.phase = value
            except Exception:
                pass

    # ---- the recording part ------------------------------------------------
    def chat(self, model, system, user, max_tokens, temperature=None):
        with self._lock:
            call = LlmCall(
                index=len(self.calls) + 1,
                phase=self.phase,
                model=model,
                system=system,
                user=user,
                max_tokens=max_tokens,
                started_at=time.time(),
            )
            self.calls.append(call)
        started = time.monotonic()
        try:
            call.response = self._inner.chat(
                model=model,
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return call.response
        except Exception as exc:
            call.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            call.seconds = time.monotonic() - started
            if self._on_call is not None:
                self._on_call(call)

    # ---- everything else is the client underneath ---------------------------
    def list_models(self):
        return self._inner.list_models()

    def context_length_for(self, model_id):
        return self._inner.context_length_for(model_id)

    def ping(self):
        return self._inner.ping()

    def __getattr__(self, name):  # anything a provider adds of its own
        return getattr(self._inner, name)


@dataclass
class CallLog:
    """The calls of one generation, for the panel to show."""

    calls: list[LlmCall] = field(default_factory=list)

    def transcript(self) -> str:
        header = f"{len(self.calls)} call(s) to the model\n"
        return header + "\n\n".join(call.transcript() for call in self.calls)
