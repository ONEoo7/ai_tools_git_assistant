"""The shipped settings, their checksum, and what a damaged one is allowed to do."""

import json

import pytest

from git_assistant import jsonc
from git_assistant import settings_backup
from git_assistant.config import RepoEntry, Settings


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        settings_backup, "user_config_dir", lambda *a, **k: str(tmp_path / "config")
    )
    return tmp_path


def _corrupt(text="tampered"):
    settings_backup.defaults_path().write_text(text, encoding="utf-8")


# ---- writing them --------------------------------------------------------------------
def test_the_shipped_settings_are_written_with_a_checksum():
    assert settings_backup.ensure_defaults() is True

    assert settings_backup.defaults_path().is_file()
    assert settings_backup.checksum_path().is_file()
    assert settings_backup.check().ok


def test_they_are_written_once_and_not_again():
    settings_backup.ensure_defaults()
    settings_backup.defaults_path().write_text(
        settings_backup.defaults_path().read_text(encoding="utf-8"), encoding="utf-8"
    )

    assert settings_backup.ensure_defaults() is False


def test_a_damaged_file_is_not_quietly_replaced_on_the_next_launch():
    """Rewriting it would erase the only evidence that anything is wrong."""
    settings_backup.ensure_defaults()
    _corrupt()

    assert settings_backup.ensure_defaults() is False
    assert settings_backup.check().state is settings_backup.Integrity.DAMAGED


def test_the_factory_file_holds_nobodys_folders():
    """A shipped file carrying someone's repository list is not a shipped file."""
    settings_backup.write_defaults()

    data = jsonc.loads(settings_backup.defaults_path().read_text(encoding="utf-8"))

    for key in settings_backup.KEPT:
        assert key not in data
    assert data["provider"] == Settings().provider


def test_a_disk_that_refuses_reports_rather_than_raises(monkeypatch):
    monkeypatch.setattr(
        settings_backup.Path,
        "mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    assert "read-only" in settings_backup.write_defaults()


# ---- checking them -------------------------------------------------------------------
def test_a_file_nobody_has_written_is_missing_rather_than_damaged():
    """The first run has not happened; that is not damage and needs no reinstall."""
    result = settings_backup.check()

    assert result.state is settings_backup.Integrity.MISSING
    assert not result.needs_reinstall
    assert not result.ok


def test_an_edited_file_fails_its_checksum():
    settings_backup.ensure_defaults()
    _corrupt('{"provider": "something-else"}')

    result = settings_backup.check()

    assert result.state is settings_backup.Integrity.DAMAGED
    assert result.needs_reinstall
    assert "checksum" in result.detail


def test_a_missing_checksum_is_damage_too():
    """Half of the evidence is not enough to call the other half intact."""
    settings_backup.ensure_defaults()
    settings_backup.checksum_path().unlink()

    result = settings_backup.check()

    assert result.state is settings_backup.Integrity.DAMAGED
    assert "missing" in result.detail


def test_an_edited_checksum_is_damage(monkeypatch):
    settings_backup.ensure_defaults()
    settings_backup.checksum_path().write_text("0" * 64, encoding="utf-8")

    assert settings_backup.check().state is settings_backup.Integrity.DAMAGED


def test_a_byte_for_byte_rewrite_still_passes():
    """The checksum is over the file, not over the moment it was written."""
    settings_backup.ensure_defaults()
    text = settings_backup.defaults_path().read_text(encoding="utf-8")

    settings_backup.defaults_path().write_text(text, encoding="utf-8")

    assert settings_backup.check().ok


def test_the_summary_says_which_of_the_three_it_is():
    assert "not been written" in settings_backup.check().summary()
    settings_backup.ensure_defaults()
    assert "intact" in settings_backup.check().summary()
    _corrupt()
    assert "altered" in settings_backup.check().summary()


# ---- restoring from them -------------------------------------------------------------
def _lived_in(**changes) -> Settings:
    """A settings object somebody has been using."""
    current = Settings(
        provider="claude",
        selected_model="opus",
        theme="pony",
        repos=[RepoEntry("/x/one"), RepoEntry("/x/two")],
        active_repo="/x/two",
        scan_roots=["/x"],
        **changes,
    )
    current.save = lambda: None
    return current


def test_restoring_puts_the_settings_back_and_keeps_the_repositories():
    """A factory reset that threw the repository list away is one nobody presses."""
    settings_backup.ensure_defaults()
    current = _lived_in()

    assert settings_backup.apply_over(current) == ""

    assert current.provider == Settings().provider
    assert current.theme == Settings().theme
    assert [r.path for r in current.repos] == ["/x/one", "/x/two"]
    assert current.active_repo == "/x/two"
    assert current.scan_roots == ["/x"]


def test_the_app_level_settings_are_the_ones_that_come_back():
    """Theme, Langfuse, MCP and the provider endpoints live in user settings."""
    settings_backup.ensure_defaults()
    current = _lived_in()
    current.provider_models = {"openai": "gpt-4o"}
    current.mcp_allow_writes = True
    current.azure_api_version = "1999-01-01"

    settings_backup.apply_over(current)

    assert current.provider_models == {}
    assert current.mcp_allow_writes is False
    assert current.azure_api_version == Settings().azure_api_version


def test_nothing_is_restored_from_a_file_that_cannot_be_trusted():
    settings_backup.ensure_defaults()
    _corrupt('{"provider": "claude"}')
    current = _lived_in()

    problem = settings_backup.apply_over(current)

    assert "altered" in problem
    assert current.provider == "claude"  # left exactly as it was


def test_the_live_settings_object_is_the_one_that_changes():
    """Every panel, the tray and every worker hold this object, not a way to
    find it: handing back a new one would restore one window and leave the rest
    running on the settings that were just thrown away."""
    settings_backup.ensure_defaults()
    current = _lived_in()
    also_held_by_a_panel = current

    settings_backup.apply_over(current)

    assert also_held_by_a_panel.provider == Settings().provider


def test_restoring_saves_nothing_of_its_own():
    """The caller knows whether this is what the user agreed to."""
    settings_backup.ensure_defaults()
    saves = []
    current = _lived_in()
    current.save = lambda: saves.append(True)

    settings_backup.apply_over(current)

    assert saves == []


def test_the_reinstall_command_names_this_package():
    from git_assistant.updating import PACKAGE_ID

    assert PACKAGE_ID in settings_backup.reinstall_command()
    assert settings_backup.reinstall_command().startswith("winget install")


# ---- what the window does with all of that ------------------------------------------
pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from git_assistant.ui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def dialog(qapp, monkeypatch):
    settings = _lived_in()
    monkeypatch.setattr(Settings, "load", staticmethod(lambda: settings))
    return SettingsDialog(settings)


def test_an_intact_backup_offers_to_restore_from_itself(dialog):
    settings_backup.write_defaults()
    dialog._refresh_shipped_status()

    assert dialog.shipped_restore_btn.isEnabled()
    assert "intact" in dialog.shipped_status.text()


def test_a_damaged_backup_proposes_a_reinstall_and_offers_no_restore(dialog):
    """There is nothing left that knows what the file should have said."""
    settings_backup.write_defaults()
    _corrupt('{"provider": "tampered"}')

    dialog._refresh_shipped_status()

    assert not dialog.shipped_restore_btn.isEnabled()
    text = dialog.shipped_status.text()
    assert "damaged" in text
    assert settings_backup.reinstall_command() in text


def test_restoring_puts_the_live_settings_back_and_saves_them(dialog, monkeypatch):
    settings_backup.write_defaults()
    saves = []
    dialog.settings.save = lambda: saves.append(True)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)

    dialog._on_restore_shipped()

    assert dialog.settings.provider == Settings().provider
    assert [r.path for r in dialog.settings.repos] == ["/x/one", "/x/two"]
    assert saves == [True]


def test_declining_restores_nothing(dialog, monkeypatch):
    settings_backup.write_defaults()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel
    )

    dialog._on_restore_shipped()

    assert dialog.settings.provider == "claude"


def test_the_window_shows_the_restored_settings(dialog, monkeypatch):
    """Otherwise the window keeps offering the settings it has just replaced."""
    settings_backup.write_defaults()
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    dialog.settings.provider = "claude"
    dialog._load_into_widgets()
    assert dialog.provider_list.currentItem().data(Qt.ItemDataRole.UserRole) == "claude"

    dialog._on_restore_shipped()

    assert dialog.provider_list.currentItem().data(Qt.ItemDataRole.UserRole) == (
        Settings().provider
    )
