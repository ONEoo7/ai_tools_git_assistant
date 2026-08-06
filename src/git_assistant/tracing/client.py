"""A chat client that also files every exchange with Langfuse.

The same shape as ``llm_log.RecordingClient``: it implements the four methods
of a provider client, forwards all of them, and changes nothing about the
request. A tracer that altered what was sent would be tracing something else.

One instance is one run. ``llm.build_client`` is called once per commit
generation, once per review, once per audit and once per MCP tool call, so a
client and a run are already the same thing -- which is why the trace boundary
needed no new plumbing.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager

from git_assistant import usage
from git_assistant.tracing import tracer
from git_assistant.tracing.settings import TraceSettings

#: What a span is called when nothing better is known. The phases in
#: ``llm_log`` are better, and reach here through ``phase``.
DEFAULT_PHASE = "completion"

#: Tagged on every trace. Langfuse cannot filter on ``service.name`` -- resource
#: attributes land under ``metadata.resourceAttributes`` and are documented as
#: not queryable -- and tags are the field that *is* filterable, so the
#: application says which application it is in both places.
TAG = "git-assistant"


class TracingClient:
    """Wraps a provider client; every ``chat`` becomes a generation."""

    def __init__(
        self,
        inner,
        config: TraceSettings,
        *,
        name: str = "",
        metadata: dict | None = None,
        user: str = "",
    ) -> None:
        self._inner = inner
        self._config = config
        self._name = name or "LLM run"
        self._metadata = dict(metadata or {})
        self._user = user
        self._lock = threading.Lock()
        self._root = None
        self._closed = False
        #: What the next call is for. Written through from the recorder above
        #: this client, when there is one; see llm_log.RecordingClient.phase.
        self.phase = DEFAULT_PHASE

    # ---- the tracing part --------------------------------------------------
    def chat(self, model, system, user, max_tokens, temperature=None):
        span = self._begin(model, system, user, max_tokens, temperature)
        # Cleared rather than merely read afterwards: this thread may have
        # recorded a completion for an earlier call, and a stale token count
        # filed against this one would be worse than none.
        usage.forget()
        try:
            reply = self._inner.chat(
                model=model,
                system=system,
                user=user,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as exc:
            self._finish(span, error=exc)
            raise
        self._finish(span, reply=reply)
        return reply

    def _root_span(self):
        """The run's span, made on the first completion and not before.

        Lazily, because ``build_client`` is also how the Connection tab tests a
        connection: a client that never chats should leave no empty trace.
        """
        with self._lock:
            if self._root is not None or self._closed:
                return self._root
            client = tracer.client(self._config)
            if client is None:
                return None
            try:
                # The root span's name is the trace's name in Langfuse, which
                # is why this is the feature ("Code review"), not "chat".
                with self._trace_scope():
                    self._root = client.start_observation(
                        name=self._name, metadata=self._metadata or None
                    )
            except Exception:
                self._root = None
            return self._root

    def _trace_scope(self):
        """Who ran this and what it was, as trace-level attributes.

        ``propagate_attributes`` puts them on spans created inside it, and it
        carries them in the OpenTelemetry context -- which does *not* cross a
        thread pool. A review fans out, so this is entered again around each
        call rather than once around the run: the attributes have to be on
        every span, not only on whichever one happened to be made first.
        """
        try:
            from langfuse import propagate_attributes

            return propagate_attributes(
                user_id=self._user or None,
                tags=[TAG],
                metadata=self._metadata or None,
            )
        except Exception:
            return _nothing()

    def _begin(self, model, system, user, max_tokens, temperature):
        try:
            root = self._root_span()
            if root is None:
                return None
            with self._trace_scope():
                return root.start_observation(
                    name=self.phase or DEFAULT_PHASE,
                    as_type="generation",
                    model=model,
                    input=self._prompt(system, user),
                    model_parameters={
                        "max_tokens": max_tokens,
                        # The one the call will actually run at, not the `None`
                        # that means "whatever the client was configured with".
                        "temperature": (
                            temperature
                            if temperature is not None
                            else getattr(self._inner, "temperature", None)
                        ),
                    },
                )
        except Exception:
            return None

    def _finish(self, span, *, reply: str = "", error: Exception | None = None):
        if span is None:
            return
        try:
            fields: dict = {"usage_details": _counts()}
            if error is not None:
                fields["level"] = "ERROR"
                fields["status_message"] = f"{type(error).__name__}: {error}"
            elif self._config.send_prompts:
                fields["output"] = reply
            span.update(**{k: v for k, v in fields.items() if v is not None})
            span.end()
        except Exception:
            pass

    def _prompt(self, system: str, user: str):
        """The exchange as messages, or nothing at all.

        Absent rather than blank when prompts are withheld: an empty string in
        Langfuse reads as a call made with an empty prompt, which is a
        different -- and alarming -- thing from a call whose prompt was kept
        at home.
        """
        if not self._config.send_prompts:
            return None
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def close(self) -> None:
        """End the run. Called in a ``finally`` wherever a run is started.

        A run whose ``close`` is missed still reaches Langfuse -- the
        generations end on their own and the trace exists -- it just has no
        span around them. A worse trace, never a wrong one.
        """
        with self._lock:
            root, self._root, self._closed = self._root, None, True
        if root is None:
            return
        try:
            root.end()
        except Exception:
            pass

    # ---- everything else is the client underneath ---------------------------
    def list_models(self):
        return self._inner.list_models()

    def context_length_for(self, model_id):
        return self._inner.context_length_for(model_id)

    def ping(self):
        return self._inner.ping()

    def __getattr__(self, name):  # anything a provider adds of its own
        return getattr(self._inner, name)


@contextmanager
def _nothing():
    """A scope that does nothing, for a build whose SDK could not supply one."""
    yield


def _counts() -> dict | None:
    """The token counts the provider itself reported, or nothing.

    Never this build's estimate. ``usage`` marks an estimate as one because a
    bill is not settled against a guess, and a number that arrives in Langfuse
    unmarked is read as measured.
    """
    event = usage.last_event()
    if event is None or event.estimated:
        return None
    return {"input": event.input_tokens, "output": event.output_tokens}
