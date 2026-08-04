"""MCP Server tab: what the server offers, and how to hand it to a client.

The tool list is built from the same catalogue the server serves, so this tab
cannot describe something the server does not do.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant.config import Settings
from git_assistant.mcp import SERVER_NAME, clients, launch, tools
from git_assistant.ui.preview_dialog import SECTION_GAP
from git_assistant.ui.workers import FunctionWorker, run_worker

INFO_COLOUR = "color: #8ab;"
WARN_COLOUR = "color: #b36b00;"
MUTED_COLOUR = "color: #888;"

WRITE_WARNING = (
    "With this on, a client can commit, push, tag and switch branches in your "
    "repositories. Changing it here does nothing on its own — register again "
    "below to apply it."
)
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
#: Long enough for a cold start behind a virus scanner, short enough that a
#: broken command does not look like a hang.
TEST_TIMEOUT = 30


class McpPanel(QWidget):
    """Explain the server, test it, and register it with a client."""

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._thread = None
        self._worker = None

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Serve this application's repositories, audits and commit-message "
                "generation to an MCP client over stdio."
            )
        )

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(INFO_COLOUR)
        layout.addWidget(self.status)

        # ---- the command ---------------------------------------------------
        command_box = QGroupBox("Server command")
        command_layout = QVBoxLayout(command_box)
        self.command_edit = QLineEdit()
        self.command_edit.setReadOnly(True)
        command_layout.addWidget(self.command_edit)

        self.writes_check = QCheckBox(
            "Allow write operations (commit, push, tag, switch branch)"
        )
        self.writes_check.setToolTip(WRITE_WARNING)
        self.writes_check.setChecked(settings.mcp_allow_writes)
        self.writes_check.toggled.connect(self._on_writes_toggled)
        command_layout.addWidget(self.writes_check)

        self.writes_note = QLabel(WRITE_WARNING)
        self.writes_note.setWordWrap(True)
        self.writes_note.setStyleSheet(MUTED_COLOUR)
        command_layout.addWidget(self.writes_note)

        row = QHBoxLayout()
        self.test_btn = QPushButton("Test server")
        self.test_btn.setToolTip(
            "Start the server, ask it what it can do, and stop it again."
        )
        self.test_btn.clicked.connect(self._on_test)
        self.copy_cmd_btn = QPushButton("Copy command")
        self.copy_cmd_btn.clicked.connect(self._on_copy_command)
        self.copy_json_btn = QPushButton("Copy JSON")
        self.copy_json_btn.setToolTip(
            "The mcpServers entry, for any other client that reads one."
        )
        self.copy_json_btn.clicked.connect(self._on_copy_json)
        row.addWidget(self.test_btn)
        row.addWidget(self.copy_cmd_btn)
        row.addWidget(self.copy_json_btn)
        row.addStretch(1)
        command_layout.addLayout(row)
        layout.addWidget(command_box)

        # ---- integrations ---------------------------------------------------
        integrations = QGroupBox("Integrations")
        grid = QVBoxLayout(integrations)

        self.desktop_status = QLabel("")
        self.desktop_status.setWordWrap(True)
        self.desktop_status.setStyleSheet(MUTED_COLOUR)
        desktop_row = QHBoxLayout()
        desktop_row.addWidget(QLabel("Claude Desktop:"))
        self.desktop_add_btn = QPushButton("Register")
        self.desktop_add_btn.clicked.connect(self._on_register_desktop)
        self.desktop_remove_btn = QPushButton("Remove")
        self.desktop_remove_btn.clicked.connect(self._on_unregister_desktop)
        desktop_row.addWidget(self.desktop_add_btn)
        desktop_row.addWidget(self.desktop_remove_btn)
        desktop_row.addStretch(1)
        grid.addLayout(desktop_row)
        grid.addWidget(self.desktop_status)

        self.code_status = QLabel("")
        self.code_status.setWordWrap(True)
        self.code_status.setStyleSheet(MUTED_COLOUR)
        code_row = QHBoxLayout()
        code_row.addWidget(QLabel("Claude Code:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItems(clients.SCOPES)
        self.scope_combo.setCurrentText(settings.mcp_scope or clients.DEFAULT_SCOPE)
        self.scope_combo.setToolTip(
            "user: every project on this machine. local: this project only. "
            "project: written into the repository's .mcp.json and committed."
        )
        self.scope_combo.currentTextChanged.connect(self._on_scope_changed)
        self.code_add_btn = QPushButton("Register")
        self.code_add_btn.clicked.connect(self._on_register_code)
        self.code_remove_btn = QPushButton("Remove")
        self.code_remove_btn.clicked.connect(self._on_unregister_code)
        code_row.addWidget(self.scope_combo)
        code_row.addWidget(self.code_add_btn)
        code_row.addWidget(self.code_remove_btn)
        code_row.addStretch(1)
        grid.addLayout(code_row)
        grid.addWidget(self.code_status)
        layout.addWidget(integrations)

        # ---- the tools -------------------------------------------------------
        layout.addSpacing(SECTION_GAP)
        layout.addWidget(QLabel("Tools offered:"))
        self.tool_tree = QTreeWidget()
        self.tool_tree.setHeaderLabels(["Tool", "Access", "What it does"])
        self.tool_tree.setRootIsDecorated(False)
        self.tool_tree.setColumnWidth(0, 200)
        self.tool_tree.setColumnWidth(1, 90)
        layout.addWidget(self.tool_tree, 1)

        self.refresh()

    # ---- state ---------------------------------------------------------------
    def _command(self) -> list[str] | None:
        return launch.server_command(allow_writes=self.writes_check.isChecked())

    def refresh(self) -> None:
        """Re-read everything this tab reports. Cheap: no subprocess."""
        command = self._command()
        self.command_edit.setText(launch.display(command))
        available = command is not None
        for button in (
            self.test_btn,
            self.copy_cmd_btn,
            self.copy_json_btn,
            self.desktop_add_btn,
            self.code_add_btn,
        ):
            button.setEnabled(available)
        if not available:
            self.status.setText(launch.NO_SERVER)
            self.status.setStyleSheet(WARN_COLOUR)
        elif self.status.text() in ("", launch.NO_SERVER):
            self.status.setText(
                "Ready. Register it below, then ask the client for this "
                "server's tools."
            )
            self.status.setStyleSheet(INFO_COLOUR)
        self._fill_tools()
        self._refresh_registrations()

    def _fill_tools(self) -> None:
        self.tool_tree.clear()
        allow = self.writes_check.isChecked()
        for tool in tools.TOOLS:
            access = "writes" if tool.writes else "read-only"
            item = QTreeWidgetItem([tool.name, access, tool.description])
            item.setToolTip(2, tool.description)
            if tool.writes and not allow:
                item.setText(1, "needs write access")
                item.setDisabled(True)
            self.tool_tree.addTopLevelItem(item)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Asking Claude Code costs a subprocess, so it waits until this tab is
        # actually looked at rather than running whenever the window is built.
        super().showEvent(event)
        self._refresh_code_status()

    def _refresh_registrations(self) -> None:
        """The cheap half: reading Claude Desktop's config is one file read."""
        command = self._command()
        desktop = clients.desktop_status(command)
        self.desktop_status.setText(f"{desktop.describe(command)}  {desktop.detail}")
        self.desktop_remove_btn.setEnabled(desktop.present)

    def _refresh_code_status(self) -> None:
        command = self._command()
        code = clients.code_status(command)
        self.code_status.setText(f"{code.describe(command)}  {code.detail}".strip())
        self.code_remove_btn.setEnabled(code.present)

    def _on_writes_toggled(self, checked: bool) -> None:
        self.settings.mcp_allow_writes = checked
        self.settings.save()
        self.writes_note.setStyleSheet(WARN_COLOUR if checked else MUTED_COLOUR)
        self.refresh()

    def _on_scope_changed(self, scope: str) -> None:
        self.settings.mcp_scope = scope
        self.settings.save()
        self._refresh_code_status()

    # ---- actions --------------------------------------------------------------
    def _on_copy_command(self) -> None:
        QGuiApplication.clipboard().setText(self.command_edit.text())
        self._say("Command copied to the clipboard.")

    def _on_copy_json(self) -> None:
        command = self._command()
        if command:
            QGuiApplication.clipboard().setText(clients.snippet(command))
            self._say("mcpServers entry copied to the clipboard.")

    def _on_test(self) -> None:
        command = self._command()
        if not command:
            return
        self.test_btn.setEnabled(False)
        self._say("Starting the server...")
        worker = FunctionWorker(lambda c=list(command): probe(c))
        worker.finished.connect(self._on_tested)
        worker.error.connect(self._on_test_failed)
        self._worker = worker
        self._thread = run_worker(worker)

    def _on_tested(self, outcome) -> None:
        self.test_btn.setEnabled(True)
        self._worker = None
        ok, message = outcome
        self._say(message, warn=not ok)

    def _on_test_failed(self, message: str) -> None:
        self.test_btn.setEnabled(True)
        self._worker = None
        self._say(message, warn=True)

    def _on_register_desktop(self) -> None:
        command = self._command()
        if not command or not self._confirm("Claude Desktop"):
            return
        try:
            note = clients.register_desktop(command)
        except clients.ClientError as exc:
            QMessageBox.warning(self, "Could not register", str(exc))
            return
        QMessageBox.information(self, "Registered", note)
        self._refresh_registrations()

    def _on_unregister_desktop(self) -> None:
        try:
            note = clients.unregister_desktop()
        except clients.ClientError as exc:
            QMessageBox.warning(self, "Could not remove", str(exc))
            return
        self._say(note)
        self._refresh_registrations()

    def _on_register_code(self) -> None:
        command = self._command()
        if not command or not self._confirm("Claude Code"):
            return
        try:
            note = clients.register_code(command, self.scope_combo.currentText())
        except clients.ClientError as exc:
            QMessageBox.warning(self, "Could not register", str(exc))
            return
        self._say(note)
        self._refresh_code_status()

    def _on_unregister_code(self) -> None:
        try:
            self._say(clients.unregister_code(self.scope_combo.currentText()))
        except clients.ClientError as exc:
            QMessageBox.warning(self, "Could not remove", str(exc))
            return
        self._refresh_code_status()

    def _confirm(self, client: str) -> bool:
        """Say what the client will be able to do before handing it the keys."""
        writes = self.writes_check.isChecked()
        what = (
            "read your repositories, generate commit messages and run audits"
            if not writes
            else "read your repositories, generate commit messages, run audits, "
            "and COMMIT, PUSH, TAG and SWITCH BRANCHES in them"
        )
        return (
            QMessageBox.question(
                self,
                f"Register with {client}",
                f"{client} will be able to start this server and use it to {what}.\n\n"
                f"Command:\n{self.command_edit.text()}\n\nRegister it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            == QMessageBox.StandardButton.Yes
        )

    def _say(self, message: str, warn: bool = False) -> None:
        self.status.setText(message)
        self.status.setStyleSheet(WARN_COLOUR if warn else INFO_COLOUR)


def probe(command: list[str]) -> tuple[bool, str]:
    """Start the server, ask what it offers, stop it. Returns (ok, message).

    The most useful thing on this tab: a registration that does not work fails
    silently inside the client, where the only symptom is a server that never
    appears.
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_NO_WINDOW,
        )
    except OSError as exc:
        return False, f"Could not start the server: {exc}"

    ask = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
    }
    try:
        out, err = proc.communicate(
            (json.dumps(ask) + "\n").encode("utf-8"), timeout=TEST_TIMEOUT
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, f"The server did not answer within {TEST_TIMEOUT} seconds."

    line = next((ln for ln in out.splitlines() if ln.strip()), b"")
    try:
        answer = json.loads(line.decode("utf-8", "replace"))
        names = [t["name"] for t in answer["result"]["tools"]]
    except (json.JSONDecodeError, KeyError, TypeError, UnicodeDecodeError):
        tail = err.decode("utf-8", "replace").strip().splitlines()[-3:]
        return False, "The server did not answer with a tool list. " + " ".join(tail)
    elapsed = int((time.monotonic() - started) * 1000)
    return True, f"Answered in {elapsed} ms — {len(names)} tools: {', '.join(names[:6])}…"
