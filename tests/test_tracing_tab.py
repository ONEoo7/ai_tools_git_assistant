"""The Langfuse controls in Advanced: what they store, and what they never store."""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant import repo_config
from git_assistant import credentials, tracing  # noqa: E402
from git_assistant.config import Settings  # noqa: E402
from git_assistant.tracing import settings as trace_settings  # noqa: E402
from git_assistant.tracing import tracer  # noqa: E402
from git_assistant.ui.settings_dialog import SettingsDialog  # noqa: E402

SECRET = "sk-lf-not-a-real-key"


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def store(monkeypatch):
    """A credential store in memory: a test may not touch the real one."""
    kept: dict[str, str] = {}
    monkeypatch.setattr(credentials, "available", lambda: True)
    monkeypatch.setattr(credentials, "get_secret", lambda key: kept.get(key))
    monkeypatch.setattr(
        credentials, "set_secret", lambda key, value, **kw: kept.__setitem__(key, value)
    )
    monkeypatch.setattr(credentials, "delete_secret", lambda key: kept.pop(key, None))
    monkeypatch.setattr(credentials, "has_secret", lambda key: bool(kept.get(key)))
    yield kept
    tracer.shutdown()


@pytest.fixture
def settings():
    s = Settings()
    s.save = lambda: None
    return s


@pytest.fixture
def dialog(qapp, settings, monkeypatch):
    # Nothing may open a modal box in a test run.
    monkeypatch.setattr(
        "git_assistant.ui.settings_dialog.QMessageBox.information", lambda *a, **k: None
    )
    return SettingsDialog(settings)


def _fill(dialog, *, secret=SECRET):
    dialog.langfuse_host_edit.setText("https://langfuse.example")
    dialog.langfuse_public_edit.setText("pk-lf-public")
    dialog._on_langfuse_public()  # what leaving the field does
    dialog.langfuse_secret_edit.setText(secret)
    dialog._on_langfuse_secret()
    dialog.langfuse_check.setChecked(True)


# ---- what is stored, and where ---------------------------------------------------
def test_tracing_is_off_until_it_is_turned_on(dialog):
    assert not dialog.langfuse_check.isChecked()
    assert not tracing.from_settings(dialog.settings).configured()


def test_the_fields_are_saved(dialog, settings):
    _fill(dialog)
    dialog._apply_to_settings()

    # Where a trace goes is a setting a repository can carry, so the tab
    # writes the User tier -- the answer every repository without one gets.
    traced = repo_config.defaults().tracing
    assert traced.enabled
    assert traced.host == "https://langfuse.example"


def test_both_keys_go_to_the_credential_manager_and_neither_to_settings(
    dialog, settings, store
):
    _fill(dialog)
    dialog._apply_to_settings()

    assert store[tracing.CREDENTIAL_KEY] == SECRET
    assert store[tracing.PUBLIC_CREDENTIAL_KEY] == "pk-lf-public"
    written = str(settings.to_dict())
    assert SECRET not in written and "pk-lf-public" not in written


def test_the_public_key_is_read_back_when_the_window_reopens(qapp, settings, store):
    """Shown, unlike the secret: nobody should have to remember which key it is."""
    store[tracing.PUBLIC_CREDENTIAL_KEY] = "pk-lf-from-last-time"
    assert SettingsDialog(settings).langfuse_public_edit.text() == "pk-lf-from-last-time"


def test_the_public_key_is_stored_on_leaving_the_field_not_per_keystroke(dialog, store):
    """A credential store is not a place to write a dozen half-typed keys."""
    dialog.langfuse_public_edit.setText("pk-lf-half")
    assert tracing.PUBLIC_CREDENTIAL_KEY not in store

    dialog._on_langfuse_public()

    assert store[tracing.PUBLIC_CREDENTIAL_KEY] == "pk-lf-half"


def test_the_box_is_cleared_the_moment_the_key_is_stored(dialog):
    """It is an input for a new key, never a display of the stored one."""
    _fill(dialog)
    assert dialog.langfuse_secret_edit.text() == ""


def test_the_stored_key_is_never_shown_anywhere(dialog):
    _fill(dialog)
    assert SECRET not in dialog.langfuse_secret_edit.text()
    assert SECRET not in dialog.langfuse_secret_edit.placeholderText()
    assert SECRET not in dialog.langfuse_status.text()


def test_removing_the_keys_deletes_both(dialog, store):
    """Half a credential pair is not a configuration."""
    _fill(dialog)
    dialog._on_clear_langfuse_secret()

    assert tracing.CREDENTIAL_KEY not in store
    assert tracing.PUBLIC_CREDENTIAL_KEY not in store
    assert dialog.langfuse_public_edit.text() == ""
    assert not dialog.langfuse_clear_btn.isEnabled()


def test_a_trailing_slash_on_the_host_is_dropped(dialog, settings):
    dialog.langfuse_host_edit.setText("https://langfuse.example/")
    dialog._apply_to_settings()
    assert repo_config.defaults().tracing.host == "https://langfuse.example"


def test_an_empty_environment_falls_back_rather_than_being_blank(dialog, settings):
    dialog.langfuse_env_edit.setText("   ")
    dialog._apply_to_settings()
    assert repo_config.defaults().tracing.environment == "development"


# ---- what the status line says ----------------------------------------------------
def test_the_missing_field_is_named_as_the_form_is_filled(dialog):
    dialog.langfuse_check.setChecked(True)
    assert "no host" in dialog.langfuse_status.text()

    dialog.langfuse_host_edit.setText("https://langfuse.example")
    assert "no public key" in dialog.langfuse_status.text()


def test_a_ready_configuration_says_where_the_key_lives(dialog):
    _fill(dialog)
    assert "Credential Manager" in dialog.langfuse_status.text()
    assert "not in settings.json" in dialog.langfuse_status.text()


def test_the_test_button_reports_what_came_back(dialog, monkeypatch):
    _fill(dialog)
    monkeypatch.setattr(tracing, "check", lambda config: (False, "Could not reach it."))

    dialog._on_test_langfuse()

    assert dialog.langfuse_status.text() == "Could not reach it."


def test_the_test_button_uses_what_is_on_screen_not_what_was_saved(dialog, monkeypatch):
    """The whole point of pressing it is to check what has just been typed."""
    seen = []
    monkeypatch.setattr(tracing, "check", lambda config: (seen.append(config), (True, "ok"))[1])
    _fill(dialog)
    dialog.langfuse_host_edit.setText("https://typed-just-now.example")

    dialog._on_test_langfuse()

    assert seen[-1].host == "https://typed-just-now.example"


# ---- turning it off ---------------------------------------------------------------
def test_unticking_the_box_stops_the_exporter(dialog, monkeypatch):
    _fill(dialog)
    stopped = []
    monkeypatch.setattr(tracing, "shutdown", lambda: stopped.append(True))

    dialog.langfuse_check.setChecked(False)

    assert stopped == [True]


def test_turning_it_on_says_what_will_be_sent(qapp, settings, monkeypatch):
    """Every prompt here is made of the user's diff; that is worth one sentence."""
    said = []
    monkeypatch.setattr(
        "git_assistant.ui.settings_dialog.QMessageBox.information",
        lambda parent, title, text, *a, **k: said.append(text),
    )
    dialog = SettingsDialog(settings)

    dialog.langfuse_check.setChecked(True)

    assert said and "source code" in said[0]
    assert tracing.who() in said[0], "the user it will be filed under, by name"


def test_loading_a_dialog_with_tracing_on_does_not_ask_again(qapp, settings, monkeypatch):
    """The warning belongs to the act of enabling it, not to opening the window."""
    settings.langfuse_enabled = True
    said = []
    monkeypatch.setattr(
        "git_assistant.ui.settings_dialog.QMessageBox.information",
        lambda *a, **k: said.append(True),
    )

    SettingsDialog(settings)

    assert said == []


# ---- withholding the prompts ------------------------------------------------------
def test_prompts_are_sent_by_default_and_can_be_withheld(dialog, settings):
    assert dialog.langfuse_prompts_check.isChecked()

    dialog.langfuse_prompts_check.setChecked(False)
    dialog._apply_to_settings()

    assert not repo_config.defaults().tracing.send_prompts
    assert not trace_settings.from_settings(repo_config.bind(settings)).send_prompts
