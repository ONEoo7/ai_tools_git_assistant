from git_assistant.commit_generator import DEFAULT_CONTEXT_WINDOW, CommitGenerator
from git_assistant.config import Settings
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
    settings = Settings(selected_model="m", context_window=size, safety_margin=margin)
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
