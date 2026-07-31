"""Choosing an AI provider: the list, its persistence, and refusing to fake it."""

import pytest

from git_assistant import providers
from git_assistant.config import RepoEntry, Settings
from git_assistant.providers import DEFAULT_PROVIDER, PROVIDERS


# ---- the list ---------------------------------------------------------------
def test_every_provider_is_offered_in_order():
    assert [p.label for p in PROVIDERS] == [
        "LM Studio",
        "Claude",
        "OpenAI",
        "Azure AI Foundry",
        "Litellm Proxy",
        "Ollama",
        "Lemonade Server",
    ]


def test_every_listed_provider_has_a_client():
    """Nothing is listed that cannot be used. If that changes, the label must
    say so -- see `display`, which is what the UI shows."""
    assert [p.label for p in PROVIDERS if not p.implemented] == []
    assert providers.get("ollama").display() == "Ollama"


def test_an_unimplemented_provider_would_be_labelled_as_such():
    """Guards the mechanism, now that no real provider exercises it."""
    from git_assistant.providers import Provider

    assert Provider("x", "Example").display() == "Example (not yet available)"


def test_every_implemented_provider_can_be_built(monkeypatch):
    """A provider marked implemented must have a client wired up for it.

    Guards the gap this pairing can develop: flipping `implemented` without
    adding a branch to `build_client` produces a provider that passes the
    UI's checks and then fails at generate time.
    """
    from git_assistant import credentials
    from git_assistant.llm import build_client

    monkeypatch.setattr(credentials, "get_secret", lambda key: "test-key")
    for provider in PROVIDERS:
        if not provider.implemented:
            continue
        s = Settings()
        s.provider = provider.key
        if not provider.base_url:
            s.set_provider_endpoint(provider.key, "https://x.example/v1")
        assert build_client(s) is not None, provider.key


def test_keys_are_unique():
    keys = [p.key for p in PROVIDERS]
    assert len(keys) == len(set(keys))


# ---- persistence ------------------------------------------------------------
def test_lm_studio_is_the_default_when_nothing_is_stored():
    assert Settings().provider == DEFAULT_PROVIDER
    assert providers.get(Settings().provider).label == "LM Studio"


def test_a_settings_file_predating_the_choice_still_loads():
    """Existing installs have no `provider` key and must not break."""
    assert Settings.from_dict({"lmstudio_port": 1234}).provider == DEFAULT_PROVIDER


def test_the_choice_round_trips():
    s = Settings()
    s.provider = "ollama"
    assert Settings.from_dict(s.to_dict()).provider == "ollama"


def test_an_unknown_provider_falls_back_rather_than_failing():
    """Hand-edited, or written by a newer build. It must still start."""
    restored = Settings.from_dict({"provider": "some-future-backend"})
    assert restored.provider == DEFAULT_PROVIDER


# ---- generating with one that does not exist yet ----------------------------
def test_generating_with_an_unimplemented_provider_refuses(monkeypatch):
    """It must not quietly generate through LM Studio under another name.

    Every provider ships with a client now, so this pins the guard itself by
    marking one unimplemented -- the check has to survive the case that
    prompted it.
    """
    from dataclasses import replace

    from git_assistant import providers as provider_module
    from git_assistant.ui import workers

    # `build_client` imports the module inside the function, so patching the
    # module attribute is enough -- there is no from-import to shadow.
    real_get = provider_module.get
    disabled = replace(real_get("ollama"), implemented=False)
    monkeypatch.setattr(
        provider_module,
        "get",
        lambda key: disabled if key == "ollama" else real_get(key),
    )

    settings = Settings()
    settings.provider = "ollama"
    worker = workers.GeneratorWorker(settings)

    errors: list[str] = []
    worker.error.connect(errors.append)
    worker.run()

    assert len(errors) == 1
    assert "Ollama is not available yet" in errors[0]


def test_a_provider_without_a_key_refuses_before_the_network():
    """Missing credentials are a configuration answer, not a failed request."""
    from git_assistant import credentials
    from git_assistant.llm import LLMError, build_client

    settings = Settings()
    settings.provider = "claude"
    credentials.delete_secret("claude")

    with pytest.raises(LLMError) as caught:
        build_client(settings)
    assert "No API key is stored" in str(caught.value)
    assert "Credential Manager" in str(caught.value)


# ---- the UI -----------------------------------------------------------------
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.ui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    s.repos = [RepoEntry("/x/a")]
    s.active_repo = "/x/a"
    return s


def test_the_providers_list_shows_them_all(qapp, settings):
    dlg = SettingsDialog(settings)
    shown = [dlg.provider_list.item(i).text() for i in range(dlg.provider_list.count())]
    assert shown == [p.display() for p in PROVIDERS]


def test_the_stored_provider_is_selected_on_open(qapp, settings):
    settings.provider = "ollama"
    dlg = SettingsDialog(settings)

    selected = dlg.provider_list.currentItem().data(Qt.ItemDataRole.UserRole)
    assert selected == "ollama"
    assert dlg.commit_panel.provider_combo.currentData() == "ollama"


def test_choosing_in_the_connection_tab_reaches_the_generate_tab(qapp, settings):
    """Two views of one setting; they must not disagree."""
    dlg = SettingsDialog(settings)
    dlg.provider_list.setCurrentRow(1)  # Claude

    assert settings.provider == "claude"
    assert dlg.commit_panel.provider_combo.currentData() == "claude"


def test_choosing_in_the_generate_tab_persists(qapp, settings):
    dlg = SettingsDialog(settings)
    index = dlg.commit_panel.provider_combo.findData("openai")
    dlg.commit_panel.provider_combo.setCurrentIndex(index)

    assert settings.provider == "openai"


def test_no_provider_is_flagged_as_unavailable(qapp, settings):
    """All of them work now, so the warning note should stay empty for each."""
    dlg = SettingsDialog(settings)
    for row in range(dlg.provider_list.count()):
        dlg.provider_list.setCurrentRow(row)
        assert dlg.provider_note.text() == "", dlg.provider_list.item(row).text()


# ---- the endpoint field -----------------------------------------------------
def test_a_default_endpoint_is_editable_text_not_a_greyed_hint(qapp, settings):
    """A placeholder reads as an example to type out; for a local server the
    default address is the answer, so it goes in as real text."""
    dlg = SettingsDialog(settings)
    for provider in PROVIDERS:
        if not provider.base_url:
            continue
        settings.provider = provider.key
        dlg._apply_provider_fields(provider)
        assert dlg.endpoint_edit.text() == provider.base_url, provider.key


def test_azure_has_no_default_to_prefill(qapp, settings):
    """Its address is per-resource, so the hint is all there is to show."""
    dlg = SettingsDialog(settings)
    dlg._apply_provider_fields(providers.get("azure-ai-foundry"))

    assert dlg.endpoint_edit.text() == ""
    assert dlg.endpoint_edit.placeholderText()


def test_a_stored_endpoint_beats_the_prefilled_default(qapp, settings):
    settings.provider = "lemonade"
    settings.set_provider_endpoint("lemonade", "http://gpu-box.lan:13305/api/v1")
    dlg = SettingsDialog(settings)

    assert dlg.endpoint_edit.text() == "http://gpu-box.lan:13305/api/v1"


def test_an_untouched_default_is_not_written_into_settings(qapp, settings):
    """Storing it would pin today's value; the provider default must keep
    applying to anyone who never edited the field."""
    settings.provider = "lemonade"
    dlg = SettingsDialog(settings)
    dlg._apply_to_settings()

    assert "lemonade" not in settings.provider_endpoints


def test_an_edited_endpoint_is_written_into_settings(qapp, settings):
    settings.provider = "lemonade"
    dlg = SettingsDialog(settings)
    dlg.endpoint_edit.setText("http://gpu-box.lan:13305/api/v1")
    dlg._apply_to_settings()

    assert settings.provider_endpoint("lemonade") == "http://gpu-box.lan:13305/api/v1"
