"""What each provider has actually been asked to do, in tokens.

A commit message is one call or fifteen; a code review is one per file. That
adds up quietly, and on a metered provider it adds up in money. This records
every completion at the point where the answer comes back -- inside the provider
clients themselves, not in the UI -- so a run started from the tray, from a tab,
or from the MCP server all count the same.

    <config dir>/llm_usage.json

Two halves. ``totals`` is per provider and model and is never pruned, because
"how much has this cost me" must not change when the detail is trimmed.
``events`` is the newest few hundred calls, which is what a table of recent
activity needs and no more.

The numbers are the provider's own where it reports them (every
OpenAI-shaped API and Anthropic do) and this build's estimate otherwise, which
is marked as such rather than presented as measured.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from platformdirs import user_config_dir

from git_assistant.config import APP_NAME

USAGE_FILE = "llm_usage.json"
SCHEMA_VERSION = 1

#: Calls kept in the detail table. Twenty reviews of forty files each is 800
#: rows; past that the table is a log nobody reads, and the totals are the
#: answer anyway.
DEFAULT_LIMIT = 500

#: One writer at a time. A review fans out over several threads, and each of
#: them finishes here.
_LOCK = threading.Lock()


def usage_path() -> Path:
    """Path to the usage file (the directory may not yet exist)."""
    return Path(user_config_dir(APP_NAME, appauthor=False)) / USAGE_FILE


@dataclass
class Event:
    """One completion: who answered it, and what it cost."""

    when: str  # ISO-8601 UTC, sortable
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    #: True when the provider did not report usage and this build counted the
    #: tokens itself. Shown, because a bill is not settled against an estimate.
    estimated: bool = False

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def when_label(self) -> str:
        """Local time: ``5 Aug 13:12:44``."""
        try:
            moment = datetime.fromisoformat(self.when.replace("Z", "+00:00"))
        except ValueError:
            return self.when or "unknown"
        return moment.astimezone().strftime("%d %b %H:%M:%S")


@dataclass
class Total:
    """Everything one model of one provider has been asked to do."""

    provider: str
    model: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_calls: int = 0
    first: str = ""
    last: str = ""

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def key(self) -> str:
        return f"{self.provider}\x1f{self.model}"

    def last_label(self) -> str:
        return Event(self.last, self.provider, self.model).when_label() if self.last else ""


@dataclass
class Usage:
    """The whole file: lifetime totals, and the recent calls behind them."""

    totals: list[Total] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)  # newest first

    def for_provider(self, provider: str) -> list[Total]:
        return [t for t in self.totals if t.provider == provider]

    def providers(self) -> list[str]:
        """Providers that have been used, most-recently first."""
        seen: dict[str, str] = {}
        for total in self.totals:
            seen[total.provider] = max(seen.get(total.provider, ""), total.last)
        return [p for p, _ in sorted(seen.items(), key=lambda kv: kv[1], reverse=True)]

    def grand_total(self) -> tuple[int, int, int]:
        """``(calls, input, output)`` across every provider."""
        return (
            sum(t.calls for t in self.totals),
            sum(t.input_tokens for t in self.totals),
            sum(t.output_tokens for t in self.totals),
        )


# ---- reading ---------------------------------------------------------------------
def parse(data: object) -> Usage:
    """Read usage from decoded JSON, ignoring anything unusable."""
    if not isinstance(data, dict):
        return Usage()
    totals = [
        Total(
            provider=str(t.get("provider", "")),
            model=str(t.get("model", "")),
            calls=int(t.get("calls", 0) or 0),
            input_tokens=int(t.get("input_tokens", 0) or 0),
            output_tokens=int(t.get("output_tokens", 0) or 0),
            estimated_calls=int(t.get("estimated_calls", 0) or 0),
            first=str(t.get("first", "")),
            last=str(t.get("last", "")),
        )
        for t in data.get("totals", [])
        if isinstance(t, dict) and t.get("provider")
    ]
    events = [
        Event(
            when=str(e.get("when", "")),
            provider=str(e.get("provider", "")),
            model=str(e.get("model", "")),
            input_tokens=int(e.get("input_tokens", 0) or 0),
            output_tokens=int(e.get("output_tokens", 0) or 0),
            estimated=bool(e.get("estimated", False)),
        )
        for e in data.get("events", [])
        if isinstance(e, dict) and e.get("provider")
    ]
    events.sort(key=lambda e: e.when, reverse=True)
    return Usage(totals=totals, events=events)


def load() -> Usage:
    """Everything recorded so far. Never raises: a broken file reads as empty."""
    try:
        return parse(json.loads(usage_path().read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return Usage()


# ---- writing ----------------------------------------------------------------------
def _write(usage: Usage) -> None:
    """Replaced, never truncated: a torn write would lose the lifetime totals."""
    path = usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": SCHEMA_VERSION,
        "totals": [asdict(t) for t in usage.totals],
        "events": [asdict(e) for e in usage.events],
    }
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def record(
    provider: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    estimated: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> Event | None:
    """Add one completion. Returns the event, or ``None`` if it was not stored.

    Called from the provider clients, so it must never raise and never be the
    reason a generated message is lost: a failure here is silent by design, and
    the worst case is a missing row in a statistics table.
    """
    if not provider:
        return None
    event = Event(
        # To the millisecond, not the second: a review fans out four calls that
        # finish inside the same second, and the order they are listed in is
        # the order they happened.
        when=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
        provider=provider,
        model=model or "(unnamed)",
        input_tokens=max(0, int(input_tokens or 0)),
        output_tokens=max(0, int(output_tokens or 0)),
        estimated=estimated,
    )
    try:
        with _LOCK:
            usage = load()
            _add(usage, event)
            usage.events.insert(0, event)
            if limit > 0:
                del usage.events[limit:]
            _write(usage)
    except OSError:
        return None
    return event


def _add(usage: Usage, event: Event) -> None:
    for total in usage.totals:
        if total.provider == event.provider and total.model == event.model:
            break
    else:
        total = Total(provider=event.provider, model=event.model, first=event.when)
        usage.totals.append(total)
    total.calls += 1
    total.input_tokens += event.input_tokens
    total.output_tokens += event.output_tokens
    total.estimated_calls += 1 if event.estimated else 0
    total.first = total.first or event.when
    total.last = event.when


def clear() -> bool:
    """Forget everything recorded. Returns False if the file could not go."""
    try:
        with _LOCK:
            usage_path().unlink(missing_ok=True)
    except OSError:
        return False
    return True


# ---- what a client hands over ---------------------------------------------------------
def from_openai_payload(payload: object) -> tuple[int, int] | None:
    """``(input, output)`` from an OpenAI-shaped ``usage`` block, if it has one.

    Every OpenAI-compatible server is *supposed* to send this, and LM Studio
    does; a proxy in between may not, which is what the estimate is for.
    """
    if not isinstance(payload, dict):
        return None
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    if prompt is None and completion is None:
        return None
    try:
        return int(prompt or 0), int(completion or 0)
    except (TypeError, ValueError):
        return None


def estimate(*, system: str, user: str, reply: str) -> tuple[int, int]:
    """This build's own count, for a provider that reports none."""
    from git_assistant.tokenizer import estimate_tokens

    return estimate_tokens(system) + estimate_tokens(user), estimate_tokens(reply)


def record_openai_response(
    provider: str, model: str, payload: object, *, system: str, user: str, reply: str
) -> Event | None:
    """Record a completion from an OpenAI-shaped answer, however it counted.

    Shared by the LM Studio client and the OpenAI-compatible one: the response
    shape is the same, and so is the question of what to do when ``usage`` is
    missing.
    """
    counted = from_openai_payload(payload)
    if counted is None:
        counted = estimate(system=system, user=user, reply=reply)
        estimated = True
    else:
        estimated = False
    return record(provider, model, counted[0], counted[1], estimated=estimated)
