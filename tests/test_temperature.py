"""Temperature, remembered per provider and per model, and reaching the wire."""

import pytest

from git_assistant import llm
from git_assistant.config import (
    DEFAULT_TEMPERATURE,
    MAX_TEMPERATURE,
    Settings,
)
from git_assistant.lmstudio_client import LMStudioClient
from git_assistant.openai_client import OpenAICompatibleClient


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None
    return s


# ---- remembering it ---------------------------------------------------------------
def test_a_model_nobody_has_set_uses_the_default(settings):
    assert settings.temperature_for("lmstudio", "qwen3.5-4b") == DEFAULT_TEMPERATURE
    assert not settings.has_temperature("lmstudio", "qwen3.5-4b")


def test_the_default_is_low_because_a_commit_message_describes(settings):
    assert DEFAULT_TEMPERATURE <= 0.3


def test_it_is_kept_per_model_not_just_per_provider(settings):
    """What is careful for one set of weights is mute for another."""
    settings.set_temperature("lmstudio", "qwen3.5-4b", 0.1)
    settings.set_temperature("lmstudio", "llama-3.3-70b", 0.7)

    assert settings.temperature_for("lmstudio", "qwen3.5-4b") == 0.1
    assert settings.temperature_for("lmstudio", "llama-3.3-70b") == 0.7


def test_two_providers_can_hold_the_same_model_name_apart(settings):
    settings.set_temperature("lmstudio", "shared-name", 0.1)
    settings.set_temperature("openai", "shared-name", 0.9)
    assert settings.temperature_for("lmstudio", "shared-name") == 0.1


def test_a_model_id_with_slashes_and_colons_survives(settings):
    """The reason this is nested rather than under a compound key."""
    name = "meta-llama/Llama-3.3-70B-Instruct:free"
    settings.set_temperature("openrouter", name, 0.4)
    assert settings.temperature_for("openrouter", name) == 0.4


def test_clearing_one_puts_the_default_back(settings):
    settings.set_temperature("lmstudio", "m", 0.9)
    settings.set_temperature("lmstudio", "m", None)

    assert settings.temperature_for("lmstudio", "m") == DEFAULT_TEMPERATURE
    assert not settings.has_temperature("lmstudio", "m")
    assert "lmstudio" not in settings.provider_temperatures, "no empty leftovers"


def test_it_survives_a_round_trip(settings):
    settings.set_temperature("lmstudio", "qwen3.5-4b", 0.35)
    assert (
        Settings.from_dict(settings.to_dict()).temperature_for("lmstudio", "qwen3.5-4b")
        == 0.35
    )


# ---- a file nobody should have to trust ---------------------------------------------
def test_a_value_a_provider_would_reject_is_clamped(settings):
    """A rejected completion is a lost commit message."""
    settings.set_temperature("lmstudio", "m", 40)
    assert settings.temperature_for("lmstudio", "m") == MAX_TEMPERATURE


def test_a_hand_edited_word_reads_as_the_default(settings):
    settings.provider_temperatures = {"lmstudio": {"m": "warm"}}
    assert settings.temperature_for("lmstudio", "m") == DEFAULT_TEMPERATURE


def test_a_hand_edited_shape_is_dropped_at_load():
    loaded = Settings.from_dict(
        {"provider_temperatures": {"lmstudio": "hot", "openai": {"m": 0.5, "n": "x"}}}
    )
    assert loaded.provider_temperatures == {"openai": {"m": 0.5}}


def test_nothing_at_all_reads_as_nothing():
    assert Settings.from_dict({"provider_temperatures": 7}).provider_temperatures == {}


# ---- reaching the request -----------------------------------------------------------
def test_a_client_uses_what_it_was_built_with(monkeypatch):
    sent = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            sent.update(json)
            return Response()

    monkeypatch.setattr("httpx.Client", lambda **kw: Client())
    LMStudioClient("http://x", temperature=0.75).chat("m", "s", "u", 10)

    assert sent["temperature"] == 0.75


def test_a_call_that_names_one_overrules_the_client(monkeypatch):
    sent = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            sent.update(json)
            return Response()

    monkeypatch.setattr("httpx.Client", lambda **kw: Client())
    LMStudioClient("http://x", temperature=0.75).chat("m", "s", "u", 10, temperature=0.0)

    assert sent["temperature"] == 0.0


def test_a_client_built_for_a_provider_carries_that_model_s_temperature(
    settings, monkeypatch
):
    settings.selected_model = "qwen3.5-4b"
    settings.set_temperature("lmstudio", "qwen3.5-4b", 0.42)

    built = llm.build_client(settings, feature="Commit message")

    assert built.temperature == 0.42


def test_the_openai_client_takes_one_too(settings):
    made = OpenAICompatibleClient("http://x", temperature=0.9)
    assert made.temperature == 0.9
