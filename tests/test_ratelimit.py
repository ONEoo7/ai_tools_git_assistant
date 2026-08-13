"""Reading the allowance off the wire, and pacing against it.

The limits are never written down here: they arrive on every response, they
differ per model and per account, and a table of them would be wrong by the
next pricing change. What is written down is how to read them.
"""

import threading

import pytest

from git_assistant import ratelimit
from git_assistant.ratelimit import Limiter, backoff, duration, read_allowance


# ---- the duration notation the reset headers use ----------------------------
@pytest.mark.parametrize(
    "text, seconds",
    [
        ("1s", 1.0),
        ("6m0s", 360.0),
        ("500ms", 0.5),
        ("1h2m3s", 3723.0),
        ("1m30s", 90.0),
        ("20", 20.0),  # Retry-After sends a bare number of seconds
        ("0s", 0.0),
    ],
)
def test_the_reset_headers_are_read(text, seconds):
    assert duration(text) == pytest.approx(seconds)


@pytest.mark.parametrize("text", ["", "   ", "soon", None, "-"])
def test_a_wait_nobody_can_measure_is_not_imposed(text):
    """Guessing at a duration would be worse than not waiting at all."""
    assert duration(text) == 0.0


# ---- what a response says ---------------------------------------------------
def test_both_dimensions_are_read_from_the_headers():
    """Requests and tokens run out independently, so both are read."""
    left = read_allowance(
        {
            "x-ratelimit-limit-requests": "500",
            "x-ratelimit-remaining-requests": "12",
            "x-ratelimit-reset-requests": "6m0s",
            "x-ratelimit-limit-tokens": "200000",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "644ms",
        }
    )
    assert (left.requests.limit, left.requests.remaining) == (500, 12)
    assert left.requests.resets_in == 360.0
    assert (left.tokens.limit, left.tokens.remaining) == (200000, 0)
    assert left.tokens.resets_in == pytest.approx(0.644)
    assert left.known
    assert left.tokens.spent and not left.requests.spent


def test_one_dimension_is_enough_to_be_worth_pacing_against():
    """A provider that reports tokens and not requests still gets paced."""
    left = read_allowance(
        {
            "x-ratelimit-limit-tokens": "200000",
            "x-ratelimit-remaining-tokens": "500",
            "x-ratelimit-reset-tokens": "1s",
        }
    )
    assert left.known
    assert not left.requests.known


def test_a_server_that_says_nothing_reports_an_unknown_allowance():
    left = read_allowance({})
    assert not left.known
    assert left.requests.remaining == -1
    assert left.tokens.remaining == -1


def test_a_nonsense_header_is_not_mistaken_for_an_answer():
    left = read_allowance({"x-ratelimit-remaining-requests": "many"})
    assert not left.known


def test_the_millisecond_form_is_preferred_where_it_is_sent():
    """Same answer, without a second's worth of rounding."""
    headers = {"retry-after": "2", "retry-after-ms": "1200"}
    assert ratelimit.retry_delay(headers) == pytest.approx(1.2)


def test_seconds_are_read_when_that_is_all_there_is():
    assert ratelimit.retry_delay({"retry-after": "2"}) == pytest.approx(2.0)


def test_no_answer_is_no_wait():
    assert ratelimit.retry_delay({}) == 0.0


# ---- pacing -----------------------------------------------------------------
@pytest.fixture
def waits(monkeypatch):
    """Record what a limiter would have slept, with time standing still.

    Deliberately frozen, unlike the ``slept`` fixture in conftest: these tests
    are about which moment each caller is *allocated*, and a clock that moved
    underneath them would answer a different question. Nothing here measures a
    total, which is the one thing a frozen clock gets wrong.
    """
    taken: list[float] = []
    monkeypatch.setattr(ratelimit, "sleep", taken.append)
    return taken


def _allowance(limit, remaining, resets_in):
    return {
        "x-ratelimit-limit-requests": str(limit),
        "x-ratelimit-remaining-requests": str(remaining),
        "x-ratelimit-reset-requests": resets_in,
    }


def test_nothing_is_paced_before_anything_is_known(waits):
    limiter = Limiter()
    for _ in range(10):
        limiter.wait_turn()
    assert waits == []


def test_a_comfortable_allowance_is_not_paced(waits):
    limiter = Limiter()
    limiter.observe(_allowance(500, 400, "60s"))
    for _ in range(5):
        limiter.wait_turn()
    assert waits == []


def test_a_spent_allowance_stops_everybody_at_once(waits):
    """Not through the spacing: that only bites from the next reservation, one
    call too late when the answer is that there are no requests left."""
    limiter = Limiter()
    limiter.observe(_allowance(500, 0, "20s"))

    limiter.wait_turn()

    assert waits[0] == pytest.approx(20, abs=0.1)


def test_the_last_of_an_allowance_is_spread_over_the_window(waits):
    limiter = Limiter()
    limiter.observe(_allowance(100, 5, "60s"))

    limiter.wait_turn()  # there is allowance for this one
    limiter.wait_turn()  # and this one is spaced by what is left

    assert waits[0] == pytest.approx(12, abs=0.1)  # 60s over 5 requests


def test_a_penalty_holds_every_caller_back(waits):
    limiter = Limiter()
    limiter.penalise(30)

    limiter.wait_turn()

    assert waits[0] == pytest.approx(30, abs=0.1)


def test_a_shorter_penalty_never_undoes_a_longer_one(waits):
    """Two threads refused at once must not let the second one shorten the wait."""
    limiter = Limiter()
    limiter.penalise(30)
    limiter.penalise(2)

    limiter.wait_turn()

    assert waits[0] == pytest.approx(30, abs=0.1)


def test_a_penalty_of_nothing_is_not_a_penalty(waits):
    limiter = Limiter()
    limiter.penalise(0)
    limiter.penalise(-5)

    limiter.wait_turn()

    assert waits == []


def test_a_window_too_far_off_is_not_waited_out_here(waits):
    """A six-minute hold is a hang. Let it through and let the 429 -- which
    carries the server's own Retry-After -- be the thing that waits."""
    limiter = Limiter()
    limiter.observe(_allowance(500, 0, "6m0s"))

    limiter.wait_turn()

    assert waits[0] == pytest.approx(ratelimit.MAX_PACE_WAIT, abs=0.1)


def test_a_fan_out_is_staggered_rather_than_released_as_one(waits):
    """Reserving under the lock is what stops eight threads waking together.

    Only up to the cap, and deliberately: past it they do pile up, and what
    re-staggers them from there is the jittered backoff on the 429 that
    follows. Waiting thirty seconds is pacing; waiting two minutes is a hang.
    """
    limiter = Limiter()
    limiter.observe(_allowance(100, 4, "60s"))  # 15s apart
    barrier = threading.Barrier(8)

    def go():
        barrier.wait()
        limiter.wait_turn()

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    taken = sorted(waits)
    assert len(taken) == 7  # the first thread had allowance and went at once
    assert taken[0] == pytest.approx(15, abs=0.1)
    assert max(taken) <= ratelimit.MAX_PACE_WAIT + 0.1


# ---- backoff ----------------------------------------------------------------
def test_backoff_grows_with_each_attempt():
    early = [backoff(0) for _ in range(50)]
    later = [backoff(3) for _ in range(50)]
    assert max(early) < min(later)


def test_backoff_is_capped():
    assert all(backoff(20, cap=20.0) <= 20.0 for _ in range(50))


def test_backoff_is_jittered():
    """Identical waits are the burst that caused the refusal, on a timer."""
    assert len({backoff(2) for _ in range(50)}) > 1


# ---- the shared registry ----------------------------------------------------
def test_one_account_is_one_limiter():
    """The reviewer and the judge are two clients spending one allowance."""
    a = ratelimit.for_account("openai", "https://api.openai.com/v1")
    b = ratelimit.for_account("openai", "https://api.openai.com/v1")
    assert a is b


def test_two_accounts_are_not():
    a = ratelimit.for_account("openai", "https://api.openai.com/v1")
    b = ratelimit.for_account("lmstudio", "http://localhost:1234/v1")
    assert a is not b


# ---- tokens per minute, which is the limit a diff run actually meets --------
def _tokens(limit, remaining, resets_in):
    return {
        "x-ratelimit-limit-tokens": str(limit),
        "x-ratelimit-remaining-tokens": str(remaining),
        "x-ratelimit-reset-tokens": resets_in,
    }


def test_a_spent_token_budget_waits_even_with_requests_to_spare(waits):
    """The failure this was written for: 200k TPM used, requests untouched."""
    limiter = Limiter()
    limiter.observe(
        {**_allowance(500, 480, "60s"), **_tokens(200_000, 0, "644ms")}
    )

    limiter.wait_turn()

    assert waits[0] == pytest.approx(0.644, abs=0.05)


def test_a_comfortable_token_budget_is_not_paced(waits):
    limiter = Limiter()
    limiter.observe(_tokens(200_000, 150_000, "60s"))

    for _ in range(5):
        limiter.wait_turn()

    assert waits == []


def test_the_scarcer_of_the_two_decides(waits):
    """Either one refusing is a refusal, so the tighter one sets the pace."""
    limiter = Limiter()
    # Requests would space by 60/5 = 12s; tokens want the whole 20s window.
    limiter.observe({**_allowance(100, 5, "60s"), **_tokens(200_000, 100, "20s")})

    limiter.wait_turn()
    limiter.wait_turn()

    assert waits[0] == pytest.approx(20, abs=0.1)


# ---- honouring a figure without resuming as one -----------------------------
def test_the_servers_figure_is_a_minimum_not_an_appointment():
    """644ms honoured to the millisecond by twenty threads is one burst."""
    asked = 0.644
    spread = {ratelimit.jittered(asked) for _ in range(50)}
    assert len(spread) > 1
    assert min(spread) >= asked
    assert max(spread) <= asked * 1.5


def test_jittering_nothing_is_nothing():
    assert ratelimit.jittered(0) == 0.0
    assert ratelimit.jittered(-1) == 0.0
