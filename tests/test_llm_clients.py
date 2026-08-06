"""The provider clients, the credential store, and per-provider settings."""

import sys

import httpx
import pytest

from git_assistant import credentials, usage
from git_assistant.claude_client import ClaudeClient, _text_of
from git_assistant.config import Settings
from git_assistant.llm import LLMError, ModelInfo
from git_assistant.openai_client import OpenAICompatibleClient

TEST_KEY = "__git-assistant-test__"


@pytest.fixture(autouse=True)
def _usage_store(tmp_path, monkeypatch):
    """Every completion is recorded, so the store must not be the real one.

    Patched where it is imported, as tests/test_identity.py does.
    """
    monkeypatch.setattr(usage, "user_config_dir", lambda *a, **k: str(tmp_path))


# ---- the credential store ---------------------------------------------------
@pytest.fixture(autouse=True)
def _clean_credential():
    credentials.delete_secret(TEST_KEY)
    yield
    credentials.delete_secret(TEST_KEY)


windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="Credential Manager is Windows-only"
)


@windows_only
def test_a_key_round_trips():
    credentials.set_secret(TEST_KEY, "sk-secret-value")
    assert credentials.get_secret(TEST_KEY) == "sk-secret-value"
    assert credentials.has_secret(TEST_KEY)


@windows_only
def test_storing_again_replaces_rather_than_duplicates():
    credentials.set_secret(TEST_KEY, "first")
    credentials.set_secret(TEST_KEY, "second")
    assert credentials.get_secret(TEST_KEY) == "second"


@windows_only
def test_a_non_ascii_key_survives():
    """The blob is UTF-16; a naive encode would corrupt or truncate it."""
    secret = "kéy-ünicode-中文"
    credentials.set_secret(TEST_KEY, secret)
    assert credentials.get_secret(TEST_KEY) == secret


@windows_only
def test_an_empty_key_deletes_rather_than_storing_blank():
    """"No key" and "a key that is empty" are the same intent."""
    credentials.set_secret(TEST_KEY, "something")
    credentials.set_secret(TEST_KEY, "")
    assert credentials.get_secret(TEST_KEY) is None


@windows_only
def test_deleting_a_key_that_is_not_there_is_fine():
    credentials.delete_secret(TEST_KEY)  # must not raise
    assert credentials.get_secret(TEST_KEY) is None
    assert credentials.has_secret(TEST_KEY) is False


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
    s.set_provider_endpoint("azure-ai-foundry", "https://r.openai.azure.com/openai/v1")

    assert s.provider_model("claude") == "claude-opus-5"
    assert s.provider_model("openai") == "gpt-4o"
    assert s.provider_endpoint("azure-ai-foundry").startswith("https://")
    assert s.provider_endpoint("openai") == ""


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


def test_per_provider_maps_round_trip():
    s = Settings()
    s.set_provider_model("openai", "gpt-4o")
    s.set_provider_endpoint("ollama", "http://localhost:11434/v1")
    restored = Settings.from_dict(s.to_dict())
    assert restored.provider_model("openai") == "gpt-4o"
    assert restored.provider_endpoint("ollama") == "http://localhost:11434/v1"


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
    settings.set_provider_endpoint("ollama", "http://gpu-box.lan:11434/v1")

    assert build_client(settings).base_url == "http://gpu-box.lan:11434/v1"


def test_a_trailing_slash_does_not_double_up_the_path():
    """Pasted endpoints usually carry one; `//chat/completions` is a 404."""
    from git_assistant.llm import build_client

    settings = Settings()
    settings.provider = "ollama"
    settings.set_provider_endpoint("ollama", "http://localhost:11434/v1/")

    assert build_client(settings).base_url == "http://localhost:11434/v1"


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
