"""Several audits over one repository at once.

The question here is not whether it is faster. It is whether three audits
running side by side stay inside the same concurrency every other fan-out in
the application respects -- a local server divides its context between the
requests in flight, and one more request than it was loaded for is an aborted
run rather than a slow one -- and what becomes of the other two when one of
them cannot be taken.
"""

import threading
import time

from git_assistant import agents
from git_assistant.agents.base import AgentInfo, Fact, Report, Section
from git_assistant.config import Settings
from git_assistant.llm import ModelInfo
from git_assistant.model_runtime import ModelRuntime
from git_assistant.parallel import effective_parallel

CONTEXT = 32768


class _Stub:
    """An audit that measures nothing and leaves one paragraph to write."""

    info = AgentInfo("stub", "Stub", "collects nothing", "free")

    def collect(self, ctx):
        return Report(
            # A real agent id, so the narrator has an outline to write to.
            agent_id="size-audit",
            title="Stub",
            subtitle="demo",
            generated_at="07 August 2026 12:00",
            repo_path=ctx.repo,
            sections=[
                Section(
                    number="1",
                    title="Executive summary",
                    slot="exec_summary",
                    facts=[Fact("total", "Total", "1.0 GiB")],
                )
            ],
        )


class _Broken:
    """An audit git cannot answer about."""

    info = AgentInfo("broken", "Broken", "always fails", "free")

    def collect(self, ctx):
        raise RuntimeError("not a git repository")


class _CountingClient:
    """A client that records how many chats were in flight at once."""

    def __init__(self, delay: float = 0.02) -> None:
        self._delay = delay
        self._lock = threading.Lock()
        self._now = 0
        self.peak = 0
        self.calls = 0

    def chat(self, model, system, user, max_tokens, temperature=None):
        with self._lock:
            self._now += 1
            self.calls += 1
            self.peak = max(self.peak, self._now)
        time.sleep(self._delay)  # long enough for a sibling to overlap it
        with self._lock:
            self._now -= 1
        return "The repository holds 1.0 GiB."

    def list_models(self):
        return [ModelInfo(id="m", max_context_length=CONTEXT, loaded=True)]

    def context_length_for(self, model_id):
        return CONTEXT


def _settings(**kw) -> Settings:
    settings = Settings(selected_model="m", context_window=CONTEXT, **kw)
    settings.save = lambda: None
    return settings


def _install(monkeypatch, client=None, **by_id):
    """Register these audits, and whichever client the narration should get."""
    monkeypatch.setattr(agents, "get", lambda agent_id: by_id[agent_id])
    monkeypatch.setattr(agents, "_sample_point", lambda repo: ("abc", "main", False))
    if client is not None:
        monkeypatch.setattr(agents, "build_client", lambda s, feature="": client)
    return by_id


def _three(monkeypatch, client):
    return _install(monkeypatch, client, a=_Stub(), b=_Stub(), c=_Stub())


# ---- how many may run at once -------------------------------------------------
def test_how_many_run_at_once_is_what_the_provider_allows():
    settings = _settings(parallel_calls=2)
    assert agents.audit_workers(settings, 3, narrate=True, context=CONTEXT) == 2
    assert effective_parallel(settings, CONTEXT) == 2


def test_a_backend_that_takes_one_call_at_a_time_gets_one_audit_at_a_time():
    """An agent CLI is a whole process per call; four is four start-ups."""
    settings = _settings(parallel_calls=8, provider="claude-cli")
    assert agents.audit_workers(settings, 3, narrate=True, context=CONTEXT) == 1


def test_audits_that_send_nothing_are_bounded_by_nothing():
    """No prose, no requests -- the provider's limit is about requests."""
    settings = _settings(parallel_calls=1)
    assert agents.audit_workers(settings, 3, narrate=False, context=CONTEXT) == 3


def test_one_audit_is_one_worker_however_generous_the_setting():
    settings = _settings(parallel_calls=8)
    assert agents.audit_workers(settings, 1, narrate=True, context=CONTEXT) == 1


# ---- the requests that actually go out ----------------------------------------
def test_the_calls_in_flight_never_exceed_the_threshold(monkeypatch):
    client = _CountingClient()
    _three(monkeypatch, client)

    agents.run_many(["a", "b", "c"], _settings(parallel_calls=2), repo="/x/demo")

    # Two, exactly: fewer would mean the fan-out is not one, and more would be
    # a request the window was not divided for.
    assert client.peak == 2
    assert client.calls >= 3  # every audit did narrate


def test_a_one_at_a_time_backend_never_has_two_in_flight(monkeypatch):
    client = _CountingClient()
    _three(monkeypatch, client)

    settings = _settings(parallel_calls=8, provider="claude-cli")

    agents.run_many(["a", "b", "c"], settings, repo="/x/demo")

    assert client.peak == 1


def test_the_window_is_divided_between_the_audits_in_flight(monkeypatch):
    """Three requests sharing a window get a third of it each, and are sized for it."""
    client = _CountingClient()
    _three(monkeypatch, client)
    built = []
    real = agents.ModelRuntime

    class Spy(real):
        def __init__(self, settings, client, **kw):
            super().__init__(settings, client, **kw)
            built.append(self)

    monkeypatch.setattr(agents, "ModelRuntime", Spy)

    agents.run_many(["a", "b", "c"], _settings(parallel_calls=3), repo="/x/demo")

    assert [runtime.slots for runtime in built] == [3]
    assert built[0].share() == CONTEXT // 3


def test_a_run_of_one_still_gets_the_whole_window(monkeypatch):
    client = _CountingClient()
    _install(monkeypatch, client, a=_Stub())
    built = []
    real = agents.ModelRuntime

    class Spy(real):
        def __init__(self, settings, client, **kw):
            super().__init__(settings, client, **kw)
            built.append(self)

    monkeypatch.setattr(agents, "ModelRuntime", Spy)

    agents.run_many(["a"], _settings(parallel_calls=4), repo="/x/demo")

    assert built[0].share() == CONTEXT


def test_one_client_for_the_whole_run_not_one_each(monkeypatch):
    """The calls pane numbers them in the order they happened, once."""
    built = []
    _three(monkeypatch, None)
    monkeypatch.setattr(
        agents,
        "build_client",
        lambda s, feature="": built.append(_CountingClient()) or built[-1],
    )

    agents.run_many(["a", "b", "c"], _settings(), repo="/x/demo")

    assert len(built) == 1


# ---- what comes back ----------------------------------------------------------
def test_every_audit_comes_back_in_the_order_it_was_asked_for(monkeypatch):
    _three(monkeypatch, _CountingClient())

    runs = agents.run_many(["c", "a", "b"], _settings(), repo="/x/demo")

    assert [run.agent_id for run in runs] == ["c", "a", "b"]
    assert all(run.ok for run in runs)


def test_the_same_audit_twice_is_the_same_audit_once(monkeypatch):
    _three(monkeypatch, _CountingClient())

    runs = agents.run_many(["a", "a", "b"], _settings(), repo="/x/demo")

    assert [run.agent_id for run in runs] == ["a", "b"]


def test_an_audit_that_cannot_be_taken_does_not_lose_the_others(monkeypatch):
    """Three audits are three readings; one that failed is not the other two."""
    _install(monkeypatch, _CountingClient(), a=_Stub(), bad=_Broken(), b=_Stub())

    runs = agents.run_many(["a", "bad", "b"], _settings(), repo="/x/demo")

    assert [run.ok for run in runs] == [True, False, True]
    assert "not a git repository" in runs[1].problem
    assert runs[1].report is None


def test_no_audit_at_all_is_refused_rather_than_run(monkeypatch):
    _three(monkeypatch, _CountingClient())
    try:
        agents.run_many([], _settings(), repo="/x/demo")
    except ValueError as exc:
        assert "No audit" in str(exc)
    else:  # pragma: no cover - the assertion above is the point
        raise AssertionError("an empty run should be refused")


# ---- the provider is not the run --------------------------------------------------
def test_a_provider_that_cannot_be_reached_still_measures_every_audit(monkeypatch):
    """Collection takes minutes and cannot be redone cheaply. It is kept."""

    def refuse(settings, feature=""):
        raise RuntimeError("no model is selected")

    _three(monkeypatch, None)
    monkeypatch.setattr(agents, "build_client", refuse)

    runs = agents.run_many(["a", "b"], _settings(), repo="/x/demo")

    assert all(run.ok for run in runs)
    assert all("no model is selected" in " ".join(r.report.warnings) for r in runs)


def test_nothing_is_built_when_no_prose_was_asked_for(monkeypatch):
    def refuse(settings, feature=""):  # pragma: no cover - failing is the point
        raise AssertionError("a run that writes no prose must contact nobody")

    _three(monkeypatch, None)
    monkeypatch.setattr(agents, "build_client", refuse)

    runs = agents.run_many(["a", "b"], _settings(), repo="/x/demo", narrate=False)

    assert all(run.ok for run in runs)


# ---- what the user is told while it runs -----------------------------------------
def test_the_bar_is_the_mean_of_the_audits_not_the_last_to_speak(monkeypatch):
    seen: list[int] = []
    shared = agents._SharedProgress(lambda _m: None, seen.append, {"a": "A", "b": "B"})

    shared.pct_for("a")(100)
    shared.pct_for("b")(50)

    assert seen == [50, 75]  # 100 of two audits is half the work


def test_the_line_says_which_audit_is_speaking_when_several_are(monkeypatch):
    seen: list[str] = []
    many = agents._SharedProgress(seen.append, lambda _p: None, {"a": "A", "b": "B"})
    one = agents._SharedProgress(seen.append, lambda _p: None, {"a": "A"})

    many.progress_for("a")("counting objects")
    one.progress_for("a")("counting objects")

    assert seen == ["A: counting objects", "counting objects"]


# ---- the arithmetic one request is sized against -----------------------------------
def test_a_shared_window_is_a_smaller_budget():
    settings = _settings()
    client = _CountingClient()
    alone = ModelRuntime(settings, client)
    shared = ModelRuntime(settings, client, slots=4)

    assert shared.share() == alone.share() // 4
    assert shared.budget() < alone.budget()
    assert shared.reserved_output() < alone.reserved_output()
