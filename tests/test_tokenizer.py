from git_assistant.commit_generator import DEFAULT_CONTEXT_WINDOW, CommitGenerator
from conftest import settings_with
from git_assistant.config import Settings
from git_assistant.llm import ModelInfo
from git_assistant.tokenizer import (
    estimate_tokens,
    input_budget,
    reserved_output,
)


class _StubClient:
    def __init__(self, detected):
        self._detected = detected

    def context_length_for(self, model_id):
        return self._detected


def _gen(size, detected, margin=0.10):
    settings = settings_with(selected_model="m", context_window=size, safety_margin=margin)
    return CommitGenerator(settings, _StubClient(detected))


# ---- context window selection ------------------------------------------------
def test_context_auto_uses_detected():
    assert _gen(0, 4096)._context_window() == 4096


def test_context_auto_falls_back_to_default():
    assert _gen(0, None)._context_window() == DEFAULT_CONTEXT_WINDOW


def test_context_configured_used_when_within_detected():
    assert _gen(4000, 8192)._context_window() == 4000


def test_context_configured_clamped_to_detected():
    # A window larger than the model's max is clamped down.
    assert _gen(32000, 8192)._context_window() == 8192


def test_context_configured_used_when_detection_unavailable():
    assert _gen(12000, None)._context_window() == 12000


# ---- input/output split fits the window --------------------------------------
def test_output_reserved_from_margin():
    assert reserved_output(32768, 0.10) == int(32768 * 0.10)


def test_output_has_floor():
    assert reserved_output(1000, 0.0) == 256


def test_input_plus_output_never_exceeds_window():
    # This is the core invariant the redesign guarantees.
    window = 32768
    g = _gen(window, None, margin=0.10)
    out = g._reserved_output(window)
    diff = g._usable(window)
    assert out + diff <= window


def test_usable_matches_window_minus_output():
    g = _gen(32768, None, margin=0.10)
    out = reserved_output(32768, 0.10)
    assert g._usable(32768) == input_budget(32768, out)


# ---- estimation & budget primitives ------------------------------------------
def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_monotonic():
    small = estimate_tokens("hello")
    big = estimate_tokens("hello " * 500)
    assert 0 < small < big


def test_input_budget_basic():
    # window - output - overhead
    assert input_budget(8192, 819) == 8192 - 819


def test_input_budget_floor():
    assert input_budget(100, 1024) == 256


def test_input_budget_overhead_subtracted():
    a = input_budget(8192, 819, overhead_tokens=0)
    b = input_budget(8192, 819, overhead_tokens=500)
    assert b == a - 500


# ---- parallel execution ------------------------------------------------------
def _par_gen(parallel):
    settings = settings_with(selected_model="m", parallel_calls=parallel)
    g = CommitGenerator(settings, _StubClient(None))
    # generate() derives this from the context window; set it directly here.
    g._workers = parallel
    return g


def test_run_parallel_preserves_order():
    g = _par_gen(4)
    items = list(range(10))
    out = g._run_parallel(items, lambda x: x * 2, lambda _m: None, lambda: False, "t")
    assert out == [x * 2 for x in items]


def test_run_parallel_sequential_when_one():
    g = _par_gen(1)
    out = g._run_parallel(["a", "b"], str.upper, lambda _m: None, lambda: False, "t")
    assert out == ["A", "B"]


def test_run_parallel_actually_concurrent():
    import threading
    import time

    g = _par_gen(4)
    active = 0
    peak = 0
    lock = threading.Lock()

    def slow(_x):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return _x

    g._run_parallel(list(range(8)), slow, lambda _m: None, lambda: False, "t")
    assert peak > 1, "calls did not overlap"
    assert peak <= 4, f"exceeded configured concurrency: {peak}"


def test_run_parallel_propagates_errors():
    import pytest

    g = _par_gen(4)

    def boom(x):
        if x == 3:
            raise RuntimeError("chunk failed")
        return x

    with pytest.raises(RuntimeError, match="chunk failed"):
        g._run_parallel(list(range(8)), boom, lambda _m: None, lambda: False, "t")


def test_run_parallel_cancellation():
    from git_assistant.commit_generator import CancelledError
    import pytest

    g = _par_gen(4)
    with pytest.raises(CancelledError):
        g._run_parallel([1, 2, 3], lambda x: x, lambda _m: None, lambda: True, "t")


# ---- parallel slots share the context window ---------------------------------
def _ctx_gen(parallel, window):
    settings = settings_with(
        selected_model="m", parallel_calls=parallel, context_window=window
    )
    return CommitGenerator(settings, _StubClient(None))


def test_per_request_context_divides_window():
    g = _ctx_gen(4, 32768)
    assert g.effective_parallel(32768) == 4
    assert g.per_request_context(32768) == 32768 // 4


def test_sequential_uses_full_window():
    g = _ctx_gen(1, 8192)
    assert g.per_request_context(8192) == 8192


def test_parallelism_capped_by_small_window():
    # 4192 context can only service 4 slots of >=1024 tokens.
    g = _ctx_gen(8, 4192)
    workers = g.effective_parallel(4192)
    assert workers == 4
    assert g.per_request_context(4192) >= 1024


def test_tiny_window_falls_back_to_sequential():
    g = _ctx_gen(8, 900)
    assert g.effective_parallel(900) == 1
    assert g.per_request_context(900) == 900


# ---- a model the server has not loaded yet -----------------------------------
# LM Studio loads on first use: the request that triggers the load is served and
# the ones racing beside it come back 500. So the first call must go alone.
class _ListingClient:
    """A client that reports one model, loaded or not."""

    def __init__(self, loaded, model_id="m"):
        self._info = ModelInfo(id=model_id, max_context_length=8192, loaded=loaded)

    def list_models(self):
        return [self._info]

    def context_length_for(self, model_id):
        return self._info.max_context_length


def _cold_gen(loaded, parallel=4):
    settings = settings_with(selected_model="m", parallel_calls=parallel)
    g = CommitGenerator(settings, _ListingClient(loaded))
    g._workers = parallel
    g._cold_start = g._model_is_cold()
    return g


def test_a_loaded_model_is_not_cold():
    assert _cold_gen(loaded=True)._model_is_cold() is False


def test_an_unloaded_model_is_cold():
    assert _cold_gen(loaded=False)._model_is_cold() is True


def test_a_provider_that_cannot_report_load_state_is_treated_as_ready():
    """A hosted model has nothing to load; a failed listing must not stall us."""
    g = CommitGenerator(settings_with(selected_model="m"), _StubClient(4096))
    assert g._model_is_cold() is False


def test_the_context_comes_from_the_listing_without_a_second_call():
    g = CommitGenerator(settings_with(selected_model="m"), _ListingClient(loaded=True))
    assert g._context_window() == 8192


#: The longest a probe waits for a sibling before deciding it has none. Never
#: reached when calls do overlap, which is every case that waits for one.
_SIBLING_TIMEOUT = 2.0

#: The window a call that should be alone is watched for company in.
_ALONE_SAMPLE = 0.05


def _first_call_concurrency(g, items=8, first_is_alone=False):
    """Run a probe through _run_parallel; report what overlapped the first call.

    A probe holds its slot until a sibling joins it rather than for a fixed
    sleep. What is being measured is whether calls overlap at all, and a probe
    that returns before the pool has started the next one measures the
    scheduler instead -- which is why a fixed 50ms sleep here passed on an idle
    machine and failed under ``pytest -n auto``, where every core is already
    busy with another worker.

    ``first_is_alone`` says this run is expected to send its first call by
    itself, because the model is cold. That call has no sibling to wait for, so
    it watches a short window instead of waiting out the timeout: it is proving
    a negative, and a short window can only weaken that proof, never turn it
    into a failure.
    """
    import threading
    import time

    state = {"active": 0, "peak": 0, "during_first": 0, "first_looked": False}
    lock = threading.Lock()

    def probe(x):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            # The most that were ever running alongside this one, taken as it is
            # seen rather than read back at the end: a probe that has left has
            # already decremented, and an overlap that happened would read as
            # one that did not.
            company = state["active"]

        if x == 0 and first_is_alone:
            time.sleep(_ALONE_SAMPLE)  # long enough for a sibling to show up
            with lock:
                company = max(company, state["active"])
        else:
            deadline = time.monotonic() + _SIBLING_TIMEOUT
            while time.monotonic() < deadline:
                with lock:
                    company = max(company, state["active"])
                    if x == 0:
                        if company > 1:
                            break  # call 0 reports its own company; nothing else will do
                    elif state["peak"] > 1 and state["first_looked"]:
                        # Two calls have overlapped *and* call 0 has had its
                        # look. Leaving before that would take away the very
                        # thing it is here to see -- and a sibling that enters
                        # and leaves between two polls is a sibling call 0
                        # never sees, which is what made this test fail.
                        break
                time.sleep(0.001)

        with lock:
            if x == 0:
                state["during_first"] = company
                state["first_looked"] = True
            state["active"] -= 1
        return x

    out = g._run_parallel(list(range(items)), probe, lambda _m: None, lambda: False, "t")
    assert out == list(range(items)), "results must stay in input order"
    return state


def test_a_cold_model_gets_the_first_call_to_itself():
    state = _first_call_concurrency(_cold_gen(loaded=False), first_is_alone=True)
    assert state["during_first"] == 1, "the load request must not race siblings"
    assert state["peak"] > 1, "the rest must still fan out once it is loaded"


def test_a_loaded_model_fans_out_immediately():
    state = _first_call_concurrency(_cold_gen(loaded=True))
    assert state["during_first"] > 1, "no reason to serialize a loaded model"


def test_the_model_is_only_warmed_once():
    """The reduce pass must not pay the serial call again."""
    g = _cold_gen(loaded=False)
    _first_call_concurrency(g, first_is_alone=True)
    assert g._cold_start is False
    assert _first_call_concurrency(g)["during_first"] > 1
