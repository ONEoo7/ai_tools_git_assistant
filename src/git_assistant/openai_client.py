"""OpenAI and anything that speaks its chat-completions shape.

One module for both OpenAI and Azure AI Foundry because the wire format is the
same one LM Studio serves: `POST {base}/chat/completions` with `messages`, and
the answer at `choices[0].message.content`. What differs between them is
configuration, not code:

  * **the address** -- OpenAI has one; Azure's is per-resource, so the user
    supplies it;
  * **the header the key goes in** -- `Authorization: Bearer` for OpenAI,
    `api-key` for Azure;
  * **query parameters** -- Azure pins the contract with `api-version`.

Written on httpx rather than the `openai` package, matching the LM Studio
client: the surface used here is two endpoints, and every provider added to
this build is one more thing to declare as a PyInstaller hidden import.
"""

from __future__ import annotations

import re

import httpx

from git_assistant import net, ratelimit, usage
from git_assistant.config import DEFAULT_TEMPERATURE
from git_assistant.llm import LLMError, ModelInfo

CHAT_TIMEOUT = 600.0
LIST_TIMEOUT = 15.0
CONNECT_TIMEOUT = 5.0

#: Time, not tries, is what bounds the retrying: a token-per-minute limit is
#: often refused with "try again in 644ms", and four attempts spends seven
#: seconds of a ninety-second budget before giving up on a wait that would have
#: cost under a second each time. The attempt cap is only a backstop against a
#: server that answers instantly and forever.
MAX_ATTEMPTS = 30
#: Total seconds one request may spend waiting to be allowed through. Past
#: this, failing with an explanation beats a progress bar that never moves.
RETRY_BUDGET = 90.0


class OpenAICompatibleClient:
    """Chat completions against any OpenAI-shaped endpoint."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        auth_header: str = "Authorization",
        extra_query: dict[str, str] | None = None,
        chat_timeout: float = CHAT_TIMEOUT,
        list_timeout: float = LIST_TIMEOUT,
        provider_key: str = "openai",
        feature: str = "",
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        # What a call gets when it does not ask for one; see
        # Settings.temperature_for, which is where this comes from.
        self.temperature = temperature
        # This client serves several providers, so it has to be told which one
        # its usage is filed under -- and what it is being spent on; see
        # git_assistant.usage.
        self.provider_key = provider_key
        self.feature = feature
        self._api_key = api_key
        self._auth_header = auth_header
        # Azure rejects an empty api-version rather than defaulting, so an
        # empty value is dropped instead of being sent as "".
        self._extra_query = {k: v for k, v in (extra_query or {}).items() if v}
        self.chat_timeout = chat_timeout
        self.list_timeout = list_timeout
        # Shared per account, not per client: the reviewer and the judge are
        # two clients spending one allowance. See git_assistant.ratelimit.
        self.limiter = ratelimit.for_account(provider_key, self.base_url)

    def _pause_for(self, response, attempt: int, waited: float) -> float | None:
        """How long to wait before trying this request again, or None to stop.

        Stops for the one 429 that never passes -- an exhausted balance -- and
        when waiting any longer would be worse than saying so: a run that hangs
        for five minutes has failed, it just has not admitted it yet.
        """
        if attempt >= MAX_ATTEMPTS - 1:
            return None
        message, code = _error_body_of(response)
        if _is_out_of_credit(message, code):
            return None  # no amount of waiting adds credit
        asked = ratelimit.retry_delay(response.headers)
        # The server's figure wins outright where it sent one: it knows when the
        # window turns over, and backing off exponentially past a documented
        # 644ms would be idling on purpose. Only guess when it said nothing.
        delay = (
            ratelimit.jittered(asked) if asked > 0 else ratelimit.backoff(attempt)
        )
        if waited + delay > RETRY_BUDGET:
            return None
        return delay

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            return {}
        if self._auth_header.lower() == "authorization":
            return {"Authorization": f"Bearer {self._api_key}"}
        return {self._auth_header: self._api_key}

    def _send(self, method: str, url: str, timeout: float, **kwargs):
        with net.http_client(
            timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT),
            headers=self._headers(),
            params=self._extra_query or None,
        ) as client:
            return client.request(method, url, **kwargs)

    def _request(self, method: str, path: str, timeout: float, **kwargs):
        url = f"{self.base_url}{path}"
        waited = 0.0
        attempt = 0
        # `while True` rather than a bounded loop so the only ways out are a
        # return and a raise. Bounding it here would make "does this always
        # answer?" depend on MAX_ATTEMPTS agreeing with a guard in _pause_for,
        # and a disagreement would return None to a caller expecting a payload.
        try:
            while True:
                waited += self.limiter.wait_turn()
                response = self._send(method, url, timeout, **kwargs)
                # Every response carries the allowance, not just the refusals:
                # pacing off the successes is what stops the refusals.
                self.limiter.observe(response.headers)
                if response.status_code == 429:
                    delay = self._pause_for(response, attempt, waited)
                    if delay is not None:
                        # Only recorded here; the wait itself is taken by the
                        # next wait_turn. One place sleeps, and the penalty
                        # reaches the other threads rather than each of them
                        # having to learn about the limit by being refused.
                        self.limiter.penalise(delay)
                        attempt += 1
                        continue
                response.raise_for_status()
                return response.json()
        except httpx.HTTPStatusError as exc:
            raise LLMError(_explain(exc, waited)) from exc
        except httpx.ConnectTimeout as exc:
            raise LLMError(
                f"Could not reach {self.base_url}: nothing answered within "
                f"{CONNECT_TIMEOUT:.0f}s. Check the address, and whether a "
                "proxy or firewall is in the way."
            ) from exc
        except httpx.ReadTimeout as exc:
            # Worth telling apart from the above, because it means the opposite:
            # the address is right and something is listening. httpx's own text
            # for this is "The read operation timed out", which says neither how
            # long it waited nor that the connection itself was fine.
            raise LLMError(
                f"{self.base_url} accepted the connection but sent nothing back "
                f"within {timeout:.0f}s. Usually a stalled connection - try again."
            ) from exc
        except httpx.HTTPError as exc:
            # A certificate that did not verify is worth telling apart: the raw
            # message names a line of C and no action, and on a corporate
            # network the cause is nearly always TLS inspection.
            if net.is_certificate_error(exc):
                raise LLMError(net.certificate_help(self.base_url)) from exc
            raise LLMError(f"Could not reach {self.base_url}: {exc}") from exc

    # ---- models ------------------------------------------------------------
    def list_models(self) -> list[ModelInfo]:
        """List models. Context length is not part of this API's response.

        `/v1/models` reports ids and nothing about capacity, so the context
        window stays unknown and the user sets it themselves -- which is why
        the Context window size field offers "Auto-detect" rather than assuming
        it.
        """
        payload = self._request("GET", "/models", self.list_timeout)
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise LLMError(f"unexpected response listing models: {payload}")
        return [
            ModelInfo(id=str(row.get("id", "")), loaded=True)
            for row in rows
            if isinstance(row, dict) and row.get("id")
        ]

    def context_length_for(self, model_id: str) -> int | None:
        """Always None: this API does not report it. Not an error."""
        return None

    def _temperature(self, asked: float | None) -> float:
        """What this call should use: what it asked for, or this client's own."""
        return self.temperature if asked is None else asked

    # ---- chat --------------------------------------------------------------
    def chat(
        self,
        model: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float | None = None,
    ) -> str:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": self._temperature(temperature),
            "stream": False,
        }
        payload = self._request(
            "POST", "/chat/completions", self.chat_timeout, json=body
        )
        try:
            text = payload["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMError(f"unexpected response shape: {payload}") from exc
        usage.record_openai_response(
            self.provider_key,
            model,
            payload,
            system=system,
            user=user,
            reply=text,
            feature=self.feature,
        )
        return text

    def ping(self) -> list[ModelInfo]:
        models = self.list_models()
        if not models:
            raise LLMError(f"{self.base_url} returned no models.")
        return models


def _error_body_of(response) -> tuple[str, str]:
    """``(message, code)`` from the provider's error body, best effort.

    Read once, up front, so every branch of :func:`_explain` can use it -- and
    so the retry can tell the two kinds of 429 apart before deciding to wait.
    The 429 branch used to return before this ran, which threw away the one
    field that says which kind it was.
    """
    try:
        body = response.json()
        error = body.get("error")
    except Exception:  # noqa: BLE001 - the body is not always JSON
        return _trim(response.text), ""
    if not isinstance(error, dict):
        return _trim(str(error or "")), ""
    code = str(error.get("code") or error.get("type") or "")
    return _trim(str(error.get("message", ""))), code


#: A closing "Visit <url> to learn more." OpenAI ends its rate-limit messages
#: with. Dropped before the length cap, because otherwise the cap lands inside
#: the URL and the message ends "Visit https://platform.op".
_BOILERPLATE = re.compile(r"\s*Visit https?://\S+.*$", re.IGNORECASE | re.DOTALL)


def _trim(message: str, limit: int = 220) -> str:
    """The useful part of a provider's message, short enough to read."""
    text = _BOILERPLATE.sub("", message or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _explain(exc: httpx.HTTPStatusError, waited: float = 0.0) -> str:
    """Turn a status code into the thing the user has to go and fix."""
    status = exc.response.status_code
    message, code = _error_body_of(exc.response)
    if status in (401, 403):
        return (
            "The API key was rejected. Check the key stored for this provider "
            "in the Connection & Model tab."
        )
    if status == 404:
        return (
            f"Not found at {exc.request.url}. Check the endpoint -- for Azure "
            "it ends with the deployment path, and the api-version must match "
            "the deployment."
        )
    if status == 429:
        # Two unrelated things share this status, and the advice for one is
        # wrong for the other: a rate limit passes on its own, and an exhausted
        # balance never does. Telling somebody to wait for that is telling them
        # to wait forever.
        if _is_out_of_credit(message, code):
            return (
                "This account has no credit left, so waiting will not help: "
                "add credit or check the billing plan for this provider. "
                f"{message}"
            ).strip()
        if waited > 0:
            # It has already been waited out, repeatedly, and the limit is
            # still there -- so "try again" is not the advice any more.
            return (
                f"{message or 'Rate limit reached.'} Still limited after "
                f"{waited:.0f}s of backing off; the account's rate limit is "
                "lower than this run needs. Reduce Parallel requests in "
                "Connection & Model, or review fewer files at a time."
            )
        after = exc.response.headers.get("retry-after", "").strip()
        advice = f"Try again in {after}s." if after else "Wait a moment and try again."
        # The provider's own sentence first: it names the model and the limit
        # that was hit, and leading with ours would only say "rate limit" twice.
        return f"{message or 'Rate limit reached.'} {advice}".strip()
    return f"The provider returned HTTP {status}. {message}".strip()


def _is_out_of_credit(message: str, code: str) -> bool:
    """Whether a 429 is about the balance rather than the pace.

    The code is the reliable answer; the wording is the fallback for a provider
    that speaks OpenAI's shape without sending one.
    """
    if code:
        return code == "insufficient_quota"
    return "exceeded your current quota" in message.lower()
