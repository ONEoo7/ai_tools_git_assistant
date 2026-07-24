"""Token estimation and input-budget calculation.

Local models served by LM Studio use many different tokenizers, so exact token
counts are impossible to know client-side. We use tiktoken's ``o200k_base`` as a
*proxy* and lean on a configurable safety margin in the budget to absorb the
mismatch. If tiktoken is unavailable we fall back to a chars/4 heuristic.
"""

from __future__ import annotations

from functools import lru_cache

_CHARS_PER_TOKEN_FALLBACK = 4
# Local tokenizers often split code more finely than o200k_base; nudge estimates
# up slightly so we err toward staying under the real context window.
_ESTIMATE_FUDGE = 1.10


@lru_cache(maxsize=1)
def _encoder():
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except Exception:  # pragma: no cover - depends on environment
        return None


def estimate_tokens(text: str) -> int:
    """Return a conservative token estimate for ``text``."""
    if not text:
        return 0
    enc = _encoder()
    if enc is None:
        base = len(text) / _CHARS_PER_TOKEN_FALLBACK
    else:
        base = len(enc.encode(text))
    return int(base * _ESTIMATE_FUDGE) + 1


MIN_OUTPUT_TOKENS = 256
MIN_INPUT_TOKENS = 256


def reserved_output(context_window: int, safety_margin: float) -> int:
    """Tokens reserved for the model's reply, as a fraction of the window.

    The safety margin doubles as the output reservation: a bigger margin leaves
    more room for the reply (and less for the diff).
    """
    return max(MIN_OUTPUT_TOKENS, int(context_window * max(0.0, safety_margin)))


def input_budget(
    context_window: int, output_tokens: int, overhead_tokens: int = 0
) -> int:
    """Tokens available for input content = window - output - overhead.

    Because output is carved out of the same window, input + output never
    exceeds ``context_window``.
    """
    budget = context_window - output_tokens - overhead_tokens
    return max(MIN_INPUT_TOKENS, budget)
