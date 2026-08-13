"""The provider clients, the credential store, and per-provider settings."""

import os
import sys

import httpx
import pytest

from conftest import user_tier
from git_assistant import repo_config
from git_assistant import credentials, usage
from git_assistant.claude_client import ClaudeClient, _text_of
from git_assistant.config import Settings
from git_assistant.llm import LLMError, ModelInfo
from git_assistant.openai_client import OpenAICompatibleClient


@pytest.fixture(autouse=True)
def _usage_store(tmp_path, monkeypatch):
    """Every completion is recorded, so the store must not be the real one.

    Patched where it is imported, as tests/test_identity.py does.
    """
    monkeypatch.setattr(usage, "user_config_dir", lambda *a, **k: str(tmp_path))


# ---- the credential store ---------------------------------------------------
@pytest.fixture
def key(request):
    """An account name no other test is using, cleaned up either way.

    These tests use the real Credential Manager, which is one store for the
    whole machine -- there is no tmp_path for it. A name shared between tests
    is therefore shared between `pytest -n auto` workers running them at the
    same moment, and one test's write became another's read: the failures read
    as "the key did not survive" when the key had simply been deleted by a
    test in another process.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "single")
    name = f"__git-assistant-test__{worker}-{request.node.name}"
    credentials.delete_secret(name)
    yield name
    credentials.delete_secret(name)


windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Credential Manager is Windows-only"
)


@windows_only
def test_a_key_round_trips(key):
    credentials.set_secret(key, "sk-secret-value")
    assert credentials.get_secret(key) == "sk-secret-value"
    assert credentials.has_secret(key)


@windows_only
def test_storing_again_replaces_rather_than_duplicates(key):
    credentials.set_secret(key, "first")
    credentials.set_secret(key, "second")
    assert credentials.get_secret(key) == "second"


@windows_only
def test_a_non_ascii_key_survives(key):
    """The blob is UTF-16; a naive encode would corrupt or truncate it."""
    secret = "kéy-ünicode-中文"
    credentials.set_secret(key, secret)
    assert credentials.get_secret(key) == secret


@windows_only
def test_an_empty_key_deletes_rather_than_storing_blank(key):
    """"No key" and "a key that is empty" are the same intent."""
    credentials.set_secret(key, "something")
    credentials.set_secret(key, "")
    assert credentials.get_secret(key) is None


@windows_only
def test_deleting_a_key_that_is_not_there_is_fine(key):
    credentials.delete_secret(key)  # must not raise
    assert credentials.get_secret(key) is None
    assert credentials.has_secret(key) is False


def test_the_entry_name_is_namespaced():
    """So the entry is identifiable in the control panel and cannot collide."""
    assert credentials.target_for("claude") == "git-assistant:claude"


def test_no_api_key_is_ever_written_to_settings():
    """The whole point of the credential store. A regression here is a leak.

    No exceptions, deliberately -- including Langfuse's *public* key, which
    would be defensible in a settings file. A rule with an exception in it is a
    rule nobody can check.
    """
    fields = set(Settings().to_dict())
    assert not [f for f in fields if "key" in f.lower() or "secret" in f.lower()]


# ---- per-provider settings ---------------------------------------------------
def test_model_and_endpoint_are_kept_per_provider():
    """Switching provider must not carry a model name into a backend that has
    never heard of it."""
    s = Settings()
    s.set_provider_model("claude", "claude-opus-5")
    s.set_provider_model("openai", "gpt-4o")
    user_tier(endpoints={"azure-ai-foundry": "https://r.openai.azure.com/openai/v1"})

    assert s.provider_model("claude") == "claude-opus-5"
    assert s.provider_model("openai") == "gpt-4o"
    # The address is the repository's setting now, so it is read through the
    # bound view -- which is what every consumer of it is handed.
    bound = repo_config.bind(s)
    assert bound.provider_endpoint("azure-ai-foundry").startswith("https://")
    assert bound.provider_endpoint("openai") == ""


def test_lm_studio_keeps_using_the_original_model_field():
    """Existing settings files must load with their model intact."""
    s = Settings.from_dict({"selected_model": "qwen2.5-coder"})
    assert s.provider_model("lmstudio") == "qwen2.5-coder"
    assert s.active_model() == "qwen2.5-coder"


def test_active_model_follows_the_selected_provider():
    s = Settings()
    s.set_provider_model("lmstudio", "local-model")
    s.set_provider_model("claude", "claude-opus-5")

    s.provider = "lmstudio"
    assert s.active_model() == "local-model"
    s.provider = "claude"
    assert s.active_model() == "claude-opus-5"


def test_the_model_map_round_trips_through_the_users_own_file():
    s = Settings()
    s.set_provider_model("openai", "gpt-4o")

    assert Settings.from_dict(s.to_dict()).provider_model("openai") == "gpt-4o"


def test_the_endpoints_round_trip_through_the_settings_a_repository_carries():
    user_tier(endpoints={"ollama": "http://localhost:11434/v1"})

    assert repo_config.defaults().model.endpoints == {
        "ollama": "http://localhost:11434/v1"
    }


def test_a_hand_edited_map_of_the_wrong_type_does_not_crash():
    """Loading must not fail far from the cause of a bad edit."""
    restored = Settings.from_dict({"provider_models": "not-a-dict"})
    assert restored.provider_models == {}


# ---- Claude: response shape --------------------------------------------------
class _Block:
    def __init__(self, type_, text=""):
        self.type = type_
        self.text = text


class _Message:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


def test_text_is_gathered_from_the_typed_blocks():
    """`content` is a list of blocks, not `choices[0].message.content`."""
    message = _Message([_Block("text", "feat: add thing")])
    assert _text_of(message) == "feat: add thing"


def test_thinking_blocks_are_not_treated_as_the_answer():
    """A thinking block alongside the answer must not land in the message."""
    message = _Message(
        [_Block("thinking", "considering..."), _Block("text", "fix: correct off-by-one")]
    )
    assert _text_of(message) == "fix: correct off-by-one"


def test_a_refusal_is_reported_rather_than_returned_as_empty():
    """A refusal is HTTP 200 with empty content; returning it would hand the
    user a blank commit message with no explanation."""
    with pytest.raises(LLMError) as caught:
        _text_of(_Message([], stop_reason="refusal"))
    assert "declined" in str(caught.value)


def test_an_empty_response_says_why():
    with pytest.raises(LLMError) as caught:
        _text_of(_Message([], stop_reason="max_tokens"))
    assert "max_tokens" in str(caught.value)


def test_claude_client_needs_no_endpoint():
    """Anthropic's address is fixed, so there is nothing for the user to set."""
    from git_assistant import providers

    assert providers.get("claude").needs_endpoint is False
    assert ClaudeClient(api_key="x") is not None


# ---- OpenAI-compatible: auth and shape ---------------------------------------
def _transport(handler):
    return httpx.MockTransport(handler)


def _client_with(monkeypatch, handler, **kwargs):
    """Route the client's httpx calls through a mock transport."""
    real_client = httpx.Client

    def fake_client(*args, **kw):
        kw["transport"] = _transport(handler)
        return real_client(*args, **kw)

    monkeypatch.setattr(httpx, "Client", fake_client)
    return OpenAICompatibleClient(base_url="https://api.example/v1", **kwargs)


def test_openai_sends_a_bearer_token(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

    client = _client_with(monkeypatch, handler, api_key="sk-test")
    assert client.list_models() == [ModelInfo(id="gpt-4o", loaded=True)]
    assert seen["auth"] == "Bearer sk-test"


def test_azure_sends_the_key_in_its_own_header(monkeypatch):
    """Azure uses `api-key`, not `Authorization: Bearer`."""
    seen = {}

    def handler(request):
        seen["api_key"] = request.headers.get("api-key")
        seen["auth"] = request.headers.get("authorization")
        seen["query"] = request.url.params.get("api-version")
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

    client = _client_with(
        monkeypatch,
        handler,
        api_key="azure-secret",
        auth_header="api-key",
        extra_query={"api-version": "2024-10-21"},
    )
    client.list_models()

    assert seen["api_key"] == "azure-secret"
    assert seen["auth"] is None
    assert seen["query"] == "2024-10-21"


def test_chat_reads_the_openai_response_shape(monkeypatch):
    def handler(request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "  chore: tidy  "}}]}
        )

    client = _client_with(monkeypatch, handler, api_key="k")
    assert client.chat("gpt-4o", "sys", "user", 100) == "chore: tidy"


def test_a_rejected_key_is_explained_not_dumped(monkeypatch):
    """401 should point at the key, not show an HTTP trace."""

    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    client = _client_with(monkeypatch, handler, api_key="wrong")
    with pytest.raises(LLMError) as caught:
        client.list_models()
    assert "API key was rejected" in str(caught.value)


def test_a_404_points_at_the_endpoint(monkeypatch):
    """The most common Azure misconfiguration."""

    def handler(request):
        return httpx.Response(404, text="not found")

    client = _client_with(monkeypatch, handler, api_key="k")
    with pytest.raises(LLMError) as caught:
        client.list_models()
    assert "endpoint" in str(caught.value)


def test_a_stalled_read_says_the_address_was_fine(monkeypatch):
    """The opposite of unreachable, and it has to read as the opposite.

    httpx's own words for this are "The read operation timed out", which say
    neither how long it waited nor that the connection itself succeeded -- so
    it reads like a wrong address, which is the one thing it is not.
    """

    def handler(request):
        raise httpx.ReadTimeout("The read operation timed out")

    client = _client_with(monkeypatch, handler, api_key="k", list_timeout=15.0)
    with pytest.raises(LLMError) as caught:
        client.list_models()

    message = str(caught.value)
    assert "accepted the connection" in message
    assert "15s" in message  # how long it waited, which the user cannot guess
    assert "try again" in message


def test_a_connection_that_never_answers_points_at_the_address(monkeypatch):
    def handler(request):
        raise httpx.ConnectTimeout("timed out")

    client = _client_with(monkeypatch, handler, api_key="k")
    with pytest.raises(LLMError) as caught:
        client.list_models()

    message = str(caught.value)
    assert "https://api.example/v1" in message
    assert "proxy or firewall" in message


def test_a_stalled_chat_is_not_quietly_retried(monkeypatch):
    """A completion that stalled on the read may still have been produced.

    Listing models twice is listing models; asking for a completion twice is a
    second one, paid for. So neither is retried here, and the message says to
    try again rather than doing it invisibly.
    """
    calls = []

    def handler(request):
        calls.append(request.url.path)
        raise httpx.ReadTimeout("x")

    client = _client_with(monkeypatch, handler, api_key="k")
    with pytest.raises(LLMError):
        client.chat("gpt-4o", "sys", "user", 100)

    assert len(calls) == 1


def test_an_unexpected_body_is_reported_rather_than_indexed(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"unexpected": True})

    client = _client_with(monkeypatch, handler, api_key="k")
    with pytest.raises(LLMError):
        client.list_models()


def test_context_length_is_unknown_not_wrong(monkeypatch):
    """This API does not report it; guessing would mis-size the token budget."""

    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}]})

    client = _client_with(monkeypatch, handler, api_key="k")
    assert client.context_length_for("gpt-4o") is None


# ---- the self-hosted, OpenAI-compatible providers ----------------------------
# Ollama, Lemonade Server and a Litellm proxy speak the same chat-completions
# shape as LM Studio, so they share one client and differ only in configuration.


def test_self_hosted_providers_default_to_localhost():
    """The usual setup should need no typing at all."""
    from git_assistant import providers

    assert providers.get("ollama").base_url == "http://localhost:11434/v1"
    assert providers.get("litellm-proxy").base_url == "http://localhost:4000/v1"
    assert providers.get("lemonade").base_url == "http://localhost:13305/api/v1"


def test_a_litellm_proxy_without_a_key_still_connects():
    """A proxy you run yourself may have no auth; requiring one would block it."""
    from git_assistant import providers
    from git_assistant.llm import build_client

    credentials.delete_secret("litellm-proxy")
    settings = Settings()
    settings.provider = "litellm-proxy"

    client = build_client(settings)  # must not raise
    assert client._headers() == {}


@windows_only
def test_a_litellm_key_is_sent_when_one_is_stored():
    from git_assistant.llm import build_client

    credentials.set_secret("litellm-proxy", "sk-virtual-key")
    try:
        settings = Settings()
        settings.provider = "litellm-proxy"
        client = build_client(settings)
        assert client._headers() == {"Authorization": "Bearer sk-virtual-key"}
    finally:
        credentials.delete_secret("litellm-proxy")


def test_ollama_offers_no_key_field():
    """Ollama has no authentication of its own; a key box would be noise."""
    from git_assistant import providers

    assert providers.get("ollama").needs_api_key is False


def test_lemonade_offers_no_key_field():
    """Same as Ollama: nothing to authenticate against."""
    from git_assistant import providers

    assert providers.get("lemonade").needs_api_key is False


def test_lemonade_keeps_its_api_v1_prefix():
    """Its OpenAI-compatible routes are under /api/v1, not /v1; dropping the
    prefix would post to a path the server does not serve."""
    from git_assistant.llm import build_client

    settings = Settings()
    settings.provider = "lemonade"

    assert build_client(settings).base_url == "http://localhost:13305/api/v1"


def test_an_overridden_endpoint_beats_the_default():
    """A remote Ollama, or a proxy on another host."""
    from git_assistant.llm import build_client

    settings = Settings()
    settings.provider = "ollama"
    user_tier(endpoints={"ollama": "http://gpu-box.lan:11434/v1"})

    assert (
        build_client(repo_config.bind(settings)).base_url
        == "http://gpu-box.lan:11434/v1"
    )


def test_a_trailing_slash_does_not_double_up_the_path():
    """Pasted endpoints usually carry one; `//chat/completions` is a 404."""
    from git_assistant.llm import build_client

    settings = Settings()
    settings.provider = "ollama"
    user_tier(endpoints={"ollama": "http://localhost:11434/v1/"})

    assert (
        build_client(repo_config.bind(settings)).base_url
        == "http://localhost:11434/v1"
    )


# ---- LM Studio: what the server said, and how big its context really is ------
def _lmstudio_with(monkeypatch, handler):
    from git_assistant.lmstudio_client import LMStudioClient

    real_client = httpx.Client

    def fake_client(*args, **kw):
        kw["transport"] = _transport(handler)
        return real_client(*args, **kw)

    monkeypatch.setattr(httpx, "Client", fake_client)
    return LMStudioClient(base_url="http://127.0.0.1:1234")


def _models_response(**overrides):
    model = {
        "id": "qwen3.5-4b",
        "type": "llm",
        "state": "loaded",
        "max_context_length": 262144,
        "loaded_context_length": 32768,
    }
    model.update(overrides)
    return httpx.Response(200, json={"data": [model]})


def test_loaded_context_wins_over_the_models_maximum(monkeypatch):
    """Planning against the weights' maximum overflows what is actually loaded."""
    client = _lmstudio_with(monkeypatch, lambda request: _models_response())
    assert client.context_length_for("qwen3.5-4b") == 32768


def test_an_unloaded_model_reports_the_maximum_it_supports(monkeypatch):
    """Nothing is loaded yet, so the ceiling is all there is to go on."""
    client = _lmstudio_with(
        monkeypatch,
        lambda request: _models_response(state="not-loaded", loaded_context_length=None),
    )
    assert client.context_length_for("qwen3.5-4b") == 262144


def test_the_load_state_is_reported(monkeypatch):
    client = _lmstudio_with(
        monkeypatch, lambda request: _models_response(state="not-loaded")
    )
    assert client.list_models()[0].loaded is False


def test_a_failed_completion_quotes_the_server(monkeypatch):
    """Without the body the user gets a status code and a link to its definition."""

    def handler(request):
        return httpx.Response(
            400,
            json={
                "error": "request (40022 tokens) exceeds the available context "
                "size (32768 tokens), try increasing it"
            },
        )

    client = _lmstudio_with(monkeypatch, handler)
    with pytest.raises(LLMError) as caught:
        client.chat("qwen3.5-4b", "sys", "user", 100)
    assert "exceeds the available context size" in str(caught.value)


def test_a_nested_error_message_is_unwrapped(monkeypatch):
    def handler(request):
        return httpx.Response(500, json={"error": {"message": "engine crashed"}})

    client = _lmstudio_with(monkeypatch, handler)
    with pytest.raises(LLMError) as caught:
        client.chat("qwen3.5-4b", "sys", "user", 100)
    assert "engine crashed" in str(caught.value)


def test_a_silent_500_names_the_likely_cause(monkeypatch):
    """LM Studio answers a request sent mid-load with an empty 500."""

    def handler(request):
        return httpx.Response(500, text="")

    client = _lmstudio_with(monkeypatch, handler)
    with pytest.raises(LLMError) as caught:
        client.chat("qwen3.5-4b", "sys", "user", 100)
    assert "still loading" in str(caught.value)


# ---- 429: two problems wearing one status code, and the waiting -------------
#
# A rate limit passes on its own and an exhausted balance never does, so the
# first is worth waiting out and the second is worth refusing to.


def _serving(monkeypatch, responses):
    """A client whose next call gets the next response in the list."""
    remaining = list(responses)
    seen = []

    def handler(request):
        seen.append(request)
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return _client_with(monkeypatch, handler, api_key="k"), seen


def _limited(message="Rate limit reached for gpt-4o-mini.", code="rate_limit_exceeded",
             headers=None):
    return httpx.Response(
        429, json={"error": {"message": message, "code": code}}, headers=headers or {}
    )


def _ok():
    return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})


def test_a_rate_limit_is_waited_out_rather_than_reported(monkeypatch, slept):
    client, seen = _serving(monkeypatch, [_limited(), _ok()])

    assert client.list_models()  # no exception: it went through on the retry

    assert len(seen) == 2
    assert len(slept) == 1


def test_the_servers_own_wait_is_honoured(monkeypatch, slept):
    """It knows when the window turns over; a guess does not."""
    client, _seen = _serving(
        monkeypatch, [_limited(headers={"retry-after": "12"}), _ok()]
    )

    client.list_models()

    # At least what it asked for, and a little more so twenty threads refused
    # in the same instant do not all resume in the same instant.
    assert 12 <= slept[0] <= 12 * 1.5


def test_milliseconds_are_preferred_where_the_server_sends_them(monkeypatch, slept):
    client, _seen = _serving(
        monkeypatch, [_limited(headers={"retry-after-ms": "8500"}), _ok()]
    )

    client.list_models()

    assert 8.5 <= slept[0] <= 8.5 * 1.5


def test_backoff_grows_and_is_never_the_same_twice(monkeypatch, slept):
    """Identical waits are the burst that caused the refusal, on a timer."""
    client, seen = _serving(monkeypatch, [_limited(), _limited(), _limited(), _ok()])

    client.list_models()

    assert len(seen) == 4
    assert len(slept) == 3
    assert slept[-1] > slept[0]


def test_it_gives_up_rather_than_waiting_for_ever(monkeypatch, slept):
    client, seen = _serving(monkeypatch, [_limited()])

    with pytest.raises(LLMError) as caught:
        client.list_models()

    from git_assistant.openai_client import MAX_ATTEMPTS, RETRY_BUDGET

    # Time is the bound, not the attempt count -- the cap is only a backstop.
    assert sum(slept) <= RETRY_BUDGET
    assert 1 < len(seen) < MAX_ATTEMPTS


def test_giving_up_says_the_limit_is_lower_than_the_run_needs(monkeypatch, slept):
    """By then "try again" is not advice; it is what has just been tried."""
    client, _seen = _serving(monkeypatch, [_limited()])

    with pytest.raises(LLMError) as caught:
        client.list_models()

    said = str(caught.value)
    assert "Parallel requests" in said
    assert "backing off" in said


def test_a_wait_longer_than_the_budget_is_not_taken(monkeypatch, slept):
    """Five minutes of a frozen window is a hang, however politely asked for."""
    client, seen = _serving(monkeypatch, [_limited(headers={"retry-after": "600"})])

    with pytest.raises(LLMError) as caught:
        client.list_models()

    assert len(seen) == 1
    assert slept == []
    assert "Try again in 600s" in str(caught.value)


def test_no_credit_is_never_retried(monkeypatch, slept):
    """Waiting does not add credit, so waiting is the wrong thing to do."""
    client, seen = _serving(
        monkeypatch,
        [_limited(message="You exceeded your current quota.", code="insufficient_quota")],
    )

    with pytest.raises(LLMError) as caught:
        client.list_models()

    assert len(seen) == 1
    assert slept == []
    said = str(caught.value)
    assert "no credit left" in said
    assert "waiting will not help" in said


def test_the_providers_own_words_are_passed_on(monkeypatch, slept):
    """The message named the model and the limit; we used to discard it."""
    client, _seen = _serving(
        monkeypatch, [_limited(message="Limit 3 RPM for gpt-4o-mini", code="x")]
    )

    with pytest.raises(LLMError) as caught:
        client.list_models()

    assert "Limit 3 RPM for gpt-4o-mini" in str(caught.value)


def test_an_unparseable_body_still_explains_the_status(monkeypatch, slept):
    def handler(request):
        return httpx.Response(500, text="<html>gateway blew up</html>")

    client = _client_with(monkeypatch, handler, api_key="k")
    with pytest.raises(LLMError) as caught:
        client.list_models()
    assert "HTTP 500" in str(caught.value)
    assert "gateway blew up" in str(caught.value)


# ---- pacing off the allowance the server reports ----------------------------
def _with_allowance(limit, remaining, resets_in):
    return httpx.Response(
        200,
        json={"data": [{"id": "m"}]},
        headers={
            "x-ratelimit-limit-requests": str(limit),
            "x-ratelimit-remaining-requests": str(remaining),
            "x-ratelimit-reset-requests": resets_in,
        },
    )


def test_a_comfortable_allowance_is_not_paced(monkeypatch, slept):
    """The usual run must not pay for the unusual one."""
    client, _seen = _serving(monkeypatch, [_with_allowance(500, 480, "60s")])

    client.list_models()
    client.list_models()

    assert slept == []


def test_the_last_of_an_allowance_is_spread_over_its_window(monkeypatch, slept):
    """Ten requests left and a minute to refill is six seconds apart.

    The call that learns this still goes at once -- there is allowance for it,
    and spacing a request against a limit nobody had yet met would be paying
    for information rather than using it. The spreading starts after that.
    """
    client, _seen = _serving(monkeypatch, [_with_allowance(500, 10, "60s")])

    client.list_models()  # learns the allowance
    client.list_models()  # goes on the strength of it
    client.list_models()  # and this one is spaced

    assert slept and slept[0] == pytest.approx(6, abs=1)


def test_a_spent_allowance_waits_for_the_refill(monkeypatch, slept):
    client, _seen = _serving(monkeypatch, [_with_allowance(500, 0, "20s")])

    client.list_models()
    client.list_models()

    assert slept[0] == pytest.approx(20, abs=0.1)


def test_a_server_that_reports_nothing_is_never_paced(monkeypatch, slept):
    """LM Studio and Ollama send no such headers and have no such limits."""
    client, _seen = _serving(monkeypatch, [_ok()])

    for _ in range(5):
        client.list_models()

    assert slept == []


def test_a_sub_second_wait_is_retried_many_times_over(monkeypatch, slept):
    """The failure this was written for.

    A tokens-per-minute limit is refused with "try again in 644ms". Four
    attempts spent seven seconds of a ninety-second budget and gave up on a
    wait that cost under a second each time.
    """
    client, seen = _serving(
        monkeypatch,
        [*[_limited(headers={"retry-after-ms": "644"})] * 12, _ok()],
    )

    client.list_models()

    assert len(seen) == 13
    assert sum(slept) < 15  # thirteen tries for the price of a few seconds


def test_a_documented_wait_is_not_backed_off_past(monkeypatch, slept):
    """Idling exponentially past a figure the server gave is idling on purpose."""
    client, _seen = _serving(
        monkeypatch,
        [*[_limited(headers={"retry-after-ms": "500"})] * 5, _ok()],
    )

    client.list_models()

    assert max(slept) < 2.0  # never grew away from the 0.5s it asked for


def test_a_token_limit_paces_the_next_call(monkeypatch, slept):
    """Requests to spare and no tokens left is still a reason to wait."""
    spent = httpx.Response(
        200,
        json={"data": [{"id": "m"}]},
        headers={
            "x-ratelimit-limit-requests": "500",
            "x-ratelimit-remaining-requests": "480",
            "x-ratelimit-limit-tokens": "200000",
            "x-ratelimit-remaining-tokens": "0",
            "x-ratelimit-reset-tokens": "644ms",
        },
    )
    client, _seen = _serving(monkeypatch, [spent])

    client.list_models()
    client.list_models()

    assert slept[0] == pytest.approx(0.644, abs=0.05)


def test_the_boilerplate_link_is_not_cut_in_half(monkeypatch, slept):
    """It read "Visit https://platform.op" -- the cap landed inside the URL."""
    long_one = (
        "Rate limit reached for gpt-4o-mini in organization org-k3VWlgt1jJxL12v87N2 "
        "on tokens per min (TPM): Limit 200000, Used 200000, Requested 2148. "
        "Please try again in 644ms. "
        "Visit https://platform.openai.com/account/rate-limits to learn more."
    )
    client, _seen = _serving(
        monkeypatch, [_limited(message=long_one, headers={"retry-after": "600"})]
    )

    with pytest.raises(LLMError) as caught:
        client.list_models()

    said = str(caught.value)
    assert "Requested 2148" in said
    assert "Please try again in 644ms." in said
    assert "Visit" not in said
    assert "platform.op" not in said
