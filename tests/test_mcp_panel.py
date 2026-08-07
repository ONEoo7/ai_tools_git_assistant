"""The MCP Server tab, and the property that the server path never loads Qt."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_the_server_never_imports_qt():
    """A convenience import here would put PyQt6 in every client's subprocess.

    It also has to keep working in a build where PyQt6 is excluded entirely --
    which is exactly how the portable MCP executable is built.
    """
    probe = (
        "import sys; import git_assistant.mcp.server, git_assistant.mcp.tools; "
        "print('PyQt6' in sys.modules or any(m.startswith('PyQt6.') for m in sys.modules))"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=60,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False"


pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.config import Settings  # noqa: E402
from git_assistant.mcp import clients, tools  # noqa: E402
from git_assistant.ui.mcp_panel import McpPanel  # noqa: E402
from git_assistant.ui.settings_dialog import SettingsDialog  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(tmp_path, monkeypatch):
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    # Never a real client's config, either: the tab reads all of them.
    monkeypatch.setattr(
        clients, "desktop_config_path", lambda: tmp_path / "claude_desktop_config.json"
    )
    monkeypatch.setattr(clients, "antigravity_config_path", lambda: tmp_path / "mcp_config.json")
    monkeypatch.setattr(clients, "vscode_config_path", lambda: tmp_path / "mcp.json")
    return s


def test_the_window_has_an_mcp_tab(qapp, settings):
    dlg = SettingsDialog(settings)
    labels = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
    assert "MCP Server" in labels


def test_the_command_to_register_is_shown(qapp, settings):
    panel = McpPanel(settings)
    assert "--mcp" in panel.command_edit.text()
    assert panel.command_edit.isReadOnly()


def test_writes_are_off_until_asked_for(qapp, settings):
    panel = McpPanel(settings)
    assert panel.writes_check.isChecked() is False
    assert "--allow-writes" not in panel.command_edit.text()


def test_turning_writes_on_changes_the_registered_command(qapp, settings):
    panel = McpPanel(settings)
    panel.writes_check.setChecked(True)
    assert "--allow-writes" in panel.command_edit.text()
    assert settings.mcp_allow_writes is True


def test_every_tool_is_listed(qapp, settings):
    panel = McpPanel(settings)
    assert panel.tool_tree.topLevelItemCount() == len(tools.TOOLS)


def test_write_tools_are_shown_as_unavailable_until_enabled(qapp, settings):
    panel = McpPanel(settings)
    rows = {
        panel.tool_tree.topLevelItem(i).text(0): panel.tool_tree.topLevelItem(i)
        for i in range(panel.tool_tree.topLevelItemCount())
    }
    assert rows["commit"].text(1) == "needs write access"
    assert rows["list_repos"].text(1) == "read-only"

    panel.writes_check.setChecked(True)
    rows = {
        panel.tool_tree.topLevelItem(i).text(0): panel.tool_tree.topLevelItem(i)
        for i in range(panel.tool_tree.topLevelItemCount())
    }
    assert rows["commit"].text(1) == "writes"


def test_the_scope_defaults_to_every_project(qapp, settings):
    panel = McpPanel(settings)
    assert panel.scope_combo.currentText() == clients.DEFAULT_SCOPE


def test_every_file_backed_client_gets_a_row(qapp, settings):
    panel = McpPanel(settings)
    labels = [client.label for client, *_ in panel._json_rows]
    assert labels == [c.label for c in clients.JSON_CLIENTS]
    assert "Antigravity" in labels
    assert "VS Code (GitHub Copilot)" in labels


def test_a_client_with_nothing_registered_cannot_be_removed(qapp, settings):
    panel = McpPanel(settings)
    assert all(not remove.isEnabled() for *_, remove in panel._json_rows)


def test_registering_a_client_from_its_own_row(qapp, settings, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: None)
    panel = McpPanel(settings)
    row = next(r for r in panel._json_rows if r[0] is clients.VSCODE)

    row[2].click()

    written = json.loads(clients.vscode_config_path().read_text(encoding="utf-8"))
    assert written["servers"]["git-assistant"]["type"] == "stdio"
    assert row[3].isEnabled()  # and Remove is now the thing to do


def test_building_the_tab_asks_claude_code_nothing(qapp, settings, monkeypatch):
    """Constructing the settings window must not spawn a subprocess."""
    called: list[str] = []
    monkeypatch.setattr(clients, "code_status", lambda *a, **k: called.append("asked"))

    McpPanel(settings)

    assert called == []


def test_declining_the_confirmation_registers_nothing(qapp, settings, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    registered: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Cancel
    )
    monkeypatch.setattr(clients, "register_desktop", lambda c: registered.append("did"))

    panel = McpPanel(settings)
    panel._on_register_desktop()

    assert registered == []


def test_the_confirmation_spells_out_what_the_client_could_do(qapp, settings, monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    shown: list[str] = []

    def capture(_parent, _title, text, *a, **k):
        shown.append(text)
        return QMessageBox.StandardButton.Cancel

    monkeypatch.setattr(QMessageBox, "question", capture)
    panel = McpPanel(settings)

    panel._on_register_desktop()
    assert "generate commit messages" in shown[0]
    assert "COMMIT" not in shown[0]

    panel.writes_check.setChecked(True)
    panel._on_register_desktop()
    assert "COMMIT, PUSH" in shown[1]


# ---- the Test server button ------------------------------------------------------
def test_testing_a_working_server_reports_its_tools():
    from git_assistant.ui.mcp_panel import probe

    ok, message = probe([sys.executable, "-m", "git_assistant", "--mcp"])

    assert ok is True
    assert f"{len(tools.catalogue())} tools" in message
    assert "list_repos" in message


def test_testing_a_command_that_cannot_start_says_why():
    """A registration that does not work fails silently inside the client."""
    from git_assistant.ui.mcp_panel import probe

    ok, message = probe([r"C:\nope\missing.exe", "--mcp"])

    assert ok is False
    assert "Could not start" in message


def test_testing_a_server_that_dies_reports_its_own_error():
    from git_assistant.ui.mcp_panel import probe

    ok, message = probe([sys.executable, "-m", "not_a_real_module", "--mcp"])

    assert ok is False
    assert "not_a_real_module" in message
