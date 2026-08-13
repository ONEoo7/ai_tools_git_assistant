"""Pacing requests to the allowance the provider says you have.

Every OpenAI-shaped response carries what is left of your account's budget --
``x-ratelimit-remaining-requests`` and how long until it refills -- so the
limits are read off the wire rather than written down here. A table of tiers
would be wrong per model, wrong per account, and out of date by the next
pricing change; these headers are none of those things, and a server that sends
none of them (LM Studio, Ollama) simply never causes a wait.

Two mechanisms, and they answer different questions:

- **pacing** spreads the *last* of an allowance over the window it refills in,
  so a fan-out of twenty files does not spend a minute's worth of requests in
  the first second. Nothing is held back while the allowance is comfortable:
  the usual run must not pay for the unusual one.
- **penalties** are what a 429 leaves behind. The server has said how long to
  wait, and every other thread wants to know -- learning it one refusal at a
  time is exactly the thundering herd the retry is trying to avoid.

Shared per account rather than per client: the reviewer and the judge are two
clients spending one budget, and a limiter that did not know that would pace
each of them against the whole.
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass

#: Sleep, as a module attribute so tests can watch it instead of living it.
sleep = time.sleep
monotonic = time.monotonic

#: Below this share of the allowance, start spreading what is left over the
#: window rather than sending it as fast as the threads can go.
LOW_WATER = 0.10

#: Never hold a request back longer than this on our own initiative. A limit
#: that resets in six minutes is real, but a UI that goes away for six minutes
#: is a hang; past this we let the request through and let a 429 -- which comes
#: with the server's own Retry-After -- be the thing that waits.
MAX_PACE_WAIT = 30.0

_UNIT = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")


def duration(text: str) -> float:
    """Seconds from OpenAI's duration notation: ``6m0s``, ``1s``, ``500ms``.

    A bare number is read as seconds, which is what ``Retry-After`` sends.
    Anything unparseable is 0.0: a wait nobody can measure is not a wait worth
    imposing.
    """
    raw = (text or "").strip().lower()
    if not raw:
        return 0.0
    found = _DURATION.findall(raw)
    if found:
        return sum(float(amount) * _UNIT[unit] for amount, unit in found)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class Budget:
    """One dimension of an allowance: requests, or tokens."""

    limit: int = 0
    remaining: int = -1  # -1 for "the server did not say"
    resets_in: float = 0.0

    @property
    def known(self) -> bool:
        return self.limit > 0 and self.remaining >= 0

    @property
    def spent(self) -> bool:
        return self.known and self.remaining <= 0

    @property
    def low(self) -> bool:
        """Into the last tenth, where sending as fast as possible stops paying."""
        return self.known and self.remaining <= max(1, int(self.limit * LOW_WATER))


@dataclass(frozen=True)
class Allowance:
    """What one response said was left, in both dimensions.

    Both matter and they run out independently: a run of large diffs exhausts
    tokens per minute with requests to spare, and a run of tiny ones does the
    opposite. Watching only requests -- which this did at first -- means being
    blind to whichever of the two a given account actually hits.
    """

    requests: Budget = Budget()
    tokens: Budget = Budget()

    @property
    def known(self) -> bool:
        return self.requests.known or self.tokens.known

    def both(self) -> tuple[Budget, Budget]:
        return (self.requests, self.tokens)


def _int(headers, name: str, fallback: int) -> int:
    try:
        return int(str(headers.get(name, "")).strip())
    except (TypeError, ValueError):
        return fallback


def _budget(headers, unit: str) -> Budget:
    return Budget(
        limit=_int(headers, f"x-ratelimit-limit-{unit}", 0),
        remaining=_int(headers, f"x-ratelimit-remaining-{unit}", -1),
        resets_in=duration(str(headers.get(f"x-ratelimit-reset-{unit}", ""))),
    )


def read_allowance(headers) -> Allowance:
    """The allowance a response reported, or an empty one."""
    return Allowance(
        requests=_budget(headers, "requests"),
        tokens=_budget(headers, "tokens"),
    )


def retry_delay(headers) -> float:
    """How long the server asked us to wait, in seconds. 0.0 if it did not.

    ``retry-after-ms`` is preferred where it appears: it is the same answer
    without a second's worth of rounding.
    """
    millis = str(headers.get("retry-after-ms", "")).strip()
    if millis:
        try:
            return max(0.0, float(millis) / 1000.0)
        except ValueError:
            pass
    return duration(str(headers.get("retry-after", "")))


class Limiter:
    """One account's pacing. Safe to call from every thread of a fan-out."""

    def __init__(self, key: str = "") -> None:
        self.key = key
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._allowance = Allowance()

    # ---- what the caller does ----------------------------------------------
    def wait_turn(self) -> float:
        """Block until this request may go, and return the seconds waited.

        The slot is *reserved* under the lock and only then slept off, so a
        hundred threads take a hundred separate slots rather than all waking at
        the same instant and re-colliding.
        """
        with self._lock:
            now = monotonic()
            start = max(now, self._next_at)
            if start - now > MAX_PACE_WAIT:
                start = now + MAX_PACE_WAIT
            self._next_at = start + self._gap()
        delay = start - monotonic()
        if delay > 0:
            sleep(delay)
            return delay
        return 0.0

    def observe(self, headers) -> None:
        """Record what a response said about the allowance.

        An allowance reported as spent stops everybody immediately rather than
        through the spacing below, which only takes effect from the *next*
        reservation onwards -- one call too late to be any use when the answer
        is that there are no requests left at all.
        """
        allowance = read_allowance(headers)
        if not allowance.known:
            return  # a server that reports nothing is never paced
        with self._lock:
            self._allowance = allowance
            for budget in allowance.both():
                if budget.spent and budget.resets_in > 0:
                    self._next_at = max(
                        self._next_at, monotonic() + budget.resets_in
                    )

    def penalise(self, seconds: float) -> None:
        """Hold every caller back for ``seconds``, after a refusal.

        Never brought forward: two threads refused at once must not let the
        second one's shorter wait undo the first one's.
        """
        if seconds <= 0:
            return
        with self._lock:
            self._next_at = max(self._next_at, monotonic() + seconds)

    def allowance(self) -> Allowance:
        with self._lock:
            return self._allowance

    # ---- how long the next caller is spaced by ------------------------------
    def _gap(self) -> float:
        """Seconds to leave after this request. Zero while there is room.

        Whichever dimension is scarcer decides, since either one refusing is a
        refusal. Called under the lock.
        """
        return min(
            max(
                self._gap_for(self._allowance.requests, countable=True),
                self._gap_for(self._allowance.tokens, countable=False),
            ),
            MAX_PACE_WAIT,
        )

    @staticmethod
    def _gap_for(budget: Budget, *, countable: bool) -> float:
        """How far apart to space requests on the strength of one dimension.

        Requests are countable: what is left divides into the window, and the
        answer is how far apart to send them. Tokens are not -- how many
        requests the remaining tokens buy depends on how big the next prompt
        is, which nothing here knows -- so a depleted token budget simply waits
        out its window, which for a rolling per-minute limit is usually under a
        second anyway.
        """
        if not budget.low or budget.resets_in <= 0:
            return 0.0
        if countable:
            return budget.resets_in / max(1, budget.remaining)
        return budget.resets_in


_LIMITERS: dict[str, Limiter] = {}
_REGISTRY_LOCK = threading.Lock()


def for_account(provider_key: str, base_url: str) -> Limiter:
    """The limiter shared by everything spending one account's allowance."""
    key = f"{provider_key}@{base_url}"
    with _REGISTRY_LOCK:
        found = _LIMITERS.get(key)
        if found is None:
            found = _LIMITERS[key] = Limiter(key)
        return found


def forget_all() -> None:
    """Drop every limiter. For tests, and for a settings change mid-session."""
    with _REGISTRY_LOCK:
        _LIMITERS.clear()


def jittered(seconds: float, spread: float = 0.25) -> float:
    """The server's figure treated as a minimum, plus a little.

    Honouring it to the millisecond would have every thread refused in the same
    instant resume in the same instant, which is the burst that caused the
    refusal. The spread is small because the figure is not a guess.
    """
    if seconds <= 0:
        return 0.0
    return seconds + random.uniform(0.0, max(0.05, seconds * spread))


def backoff(attempt: int, base: float = 1.0, cap: float = 20.0) -> float:
    """Exponential backoff with jitter, as the provider's own guidance asks.

    The jitter is the point: without it, every thread refused in the same
    second retries in the same second, and the burst that caused the refusal
    happens again on schedule.
    """
    ceiling = min(cap, base * (2**max(0, attempt)))
    return random.uniform(ceiling / 2, ceiling)
