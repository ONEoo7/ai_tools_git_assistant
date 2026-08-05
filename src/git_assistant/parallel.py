"""Running many model calls at once, and how many "at once" may be.

Two callers fan out: commit-message generation summarizing chunks of a diff,
and a code review taking a file at a time. Both meet the same two facts about a
local server, which is why this lives in one place:

- LM Studio (llama.cpp) divides the loaded context across parallel slots, so N
  in-flight requests each get roughly ``context / N`` tokens.
- A model the server has not loaded yet serves only the request that triggered
  the load and refuses its siblings with a 500.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from git_assistant.config import Settings

#: Never shrink a parallel request's share of the context below this.
MIN_PARALLEL_CONTEXT = 1024

ProgressFn = Callable[[str], None]
CancelFn = Callable[[], bool]


class CancelledError(RuntimeError):
    """Raised when work is cancelled cooperatively."""


def _noop(_: str) -> None:
    pass


def _never() -> bool:
    return False


def effective_parallel(settings: Settings, context: int) -> int:
    """How many requests may safely run at once for this context size.

    Running more than the context can service makes the server abort with
    "Context size has been exceeded", so concurrency is capped by the window.
    """
    requested = max(1, int(settings.parallel_calls or 1))
    affordable = max(1, context // MIN_PARALLEL_CONTEXT)
    return min(requested, affordable)


def per_request_context(settings: Settings, context: int) -> int:
    """Context a single request may use when running concurrently."""
    return context // effective_parallel(settings, context)


def run_parallel(
    items: list,
    fn: Callable,
    *,
    workers: int,
    cold_start: bool = False,
    progress: ProgressFn = _noop,
    is_cancelled: CancelFn = _never,
    label: str = "working",
    prefix: str = "",
) -> list:
    """Apply ``fn`` to every item, up to ``workers`` at a time.

    Results keep the input order. Network I/O releases the GIL, so threads give
    a near-linear speed-up on independent calls.

    ``cold_start`` sends the first item on its own so the fan-out meets a loaded
    model. It costs nothing when the model is already up, which is the usual
    case. The caller owns the flag and should clear it once this returns: by
    then a call has come back, whatever branch was taken.
    """
    total = len(items)
    # Never exceed the concurrency the context window can service - the work was
    # sized for exactly this many slots.
    workers = max(1, min(workers, total))
    _check(is_cancelled)

    if workers == 1 or total == 1:
        results = []
        for i, item in enumerate(items, start=1):
            _check(is_cancelled)
            progress(f"{prefix}{label} {i}/{total}...")
            results.append(fn(item))
        return results

    results: list = [None] * total
    lock = threading.Lock()

    first = 0
    if cold_start:
        progress(f"{prefix}{label} 1/{total} (loading the model)...")
        results[0] = fn(items[0])
        first = 1
        _check(is_cancelled)

    done = first
    progress(f"{prefix}{label} {done}/{total} ({workers} in parallel)...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(fn, item): i for i, item in enumerate(items[first:], start=first)
        }
        try:
            for fut in as_completed(futures):
                if is_cancelled():
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise CancelledError("Cancelled.")
                results[futures[fut]] = fut.result()
                with lock:
                    done += 1
                    progress(
                        f"{prefix}{label} {done}/{total} ({workers} in parallel)..."
                    )
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
    return results


def _check(is_cancelled: CancelFn) -> None:
    if is_cancelled():
        raise CancelledError("Cancelled.")
