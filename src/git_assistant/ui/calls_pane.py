"""Every request and answer, so a poor result can be traced to one call.

A run is one call, or forty. When the result disappoints, the answer is in
which call went wrong -- a chunk summarised badly, a prompt that arrived with
its notes cut off, a file whose reply was prose -- and none of that can be read
off the final text.

Shared by the commit tab and the code review tab. Both fan out over the same
recorder (``git_assistant.llm_log``), and duplicating sixty lines of list, view
and buttons would leave two panes free to drift in what they show.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QFontDatabase, QGuiApplication
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

TITLE = "View LLM Calls"

PLACEHOLDER = (
    "Every call made during a run appears here: the exact system and user "
    "prompt sent, and exactly what came back."
)


def transcript(calls: list) -> str:
    header = f"{len(calls)} call(s) to the model\n\n"
    return header + "\n\n".join(call.transcript() for call in calls)


class CallsPane(QWidget):
    """The list of calls, the selected one in full, and ways to keep it."""

    #: Something worth saying in the host's status line ("Call copied...").
    noted = pyqtSignal(str)
    #: How many calls the pane now holds, for whoever draws its title.
    countChanged = pyqtSignal(int)  # noqa: N815 - Qt signal naming

    def __init__(
        self,
        *,
        repo_name: Callable[[], str] = lambda: "repo",
        margins: tuple[int, int, int, int] = (0, 0, 0, 0),
        show_title: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._repo_name = repo_name
        self._calls: list = []

        box = QVBoxLayout(self)
        box.setContentsMargins(*margins)

        # Hidden when the pane sits behind a tab that already names it: two
        # copies of "View LLM Calls (3)", one above the other, is one too many.
        self.calls_label = QLabel(TITLE)
        self.calls_label.setVisible(show_title)
        box.addWidget(self.calls_label)

        self.calls_list = QListWidget()
        self.calls_list.setMaximumHeight(150)
        self.calls_list.currentRowChanged.connect(self._on_selected)
        box.addWidget(self.calls_list)

        self.call_view = QTextEdit()
        self.call_view.setReadOnly(True)
        self.call_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.call_view.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.call_view.setPlaceholderText(PLACEHOLDER)
        box.addWidget(self.call_view, 1)

        row = QHBoxLayout()
        self.copy_call_btn = QPushButton("Copy call")
        self.copy_call_btn.clicked.connect(self._on_copy_call)
        self.copy_all_calls_btn = QPushButton("Copy all")
        self.copy_all_calls_btn.clicked.connect(self._on_copy_all)
        self.save_calls_btn = QPushButton("Save...")
        self.save_calls_btn.clicked.connect(self._on_save)
        for button in (self.copy_call_btn, self.copy_all_calls_btn, self.save_calls_btn):
            button.setEnabled(False)
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)

    # ---- what it holds -----------------------------------------------------
    @property
    def calls(self) -> list:
        return self._calls

    def reset(self) -> None:
        self._calls = []
        self.calls_list.clear()
        self.call_view.clear()
        self.calls_label.setText(TITLE)
        for button in (self.copy_call_btn, self.copy_all_calls_btn, self.save_calls_btn):
            button.setEnabled(False)
        self.countChanged.emit(0)

    def add_call(self, call) -> None:
        """One exchange finished. Shown as it happens, not only at the end."""
        self._calls.append(call)
        marker = "" if call.ok else "  [failed]"
        self.calls_list.addItem(f"{call.index}. {call.summary()}{marker}")
        self.calls_label.setText(f"{TITLE} ({len(self._calls)})")
        for button in (self.copy_all_calls_btn, self.save_calls_btn):
            button.setEnabled(True)
        if self.calls_list.currentRow() < 0:
            self.calls_list.setCurrentRow(0)
        self.countChanged.emit(len(self._calls))

    def say(self, message: str) -> None:
        """Put a line where the transcript goes, for a run with no calls of its own."""
        self.reset()
        self.call_view.setPlainText(message)

    # ---- interaction -------------------------------------------------------
    def _on_selected(self, row: int) -> None:
        if 0 <= row < len(self._calls):
            self.call_view.setPlainText(self._calls[row].transcript())
            self.copy_call_btn.setEnabled(True)

    def _on_copy_call(self) -> None:
        row = self.calls_list.currentRow()
        if 0 <= row < len(self._calls):
            QGuiApplication.clipboard().setText(self._calls[row].transcript())
            self.noted.emit("Call copied to the clipboard.")

    def _on_copy_all(self) -> None:
        if self._calls:
            QGuiApplication.clipboard().setText(transcript(self._calls))
            self.noted.emit(f"{len(self._calls)} call(s) copied to the clipboard.")

    def _on_save(self) -> None:
        if not self._calls:
            return
        path, _chosen = QFileDialog.getSaveFileName(
            self,
            "Save the calls",
            str(Path.home() / f"{self._repo_name() or 'repo'}-llm-calls.txt"),
            "Text (*.txt);;All files (*)",
        )
        if not path:
            return
        try:
            Path(path).write_text(transcript(self._calls), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Could not save", str(exc))
            return
        self.noted.emit(f"Saved to {path}")
