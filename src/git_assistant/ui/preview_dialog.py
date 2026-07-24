"""Editable commit-message preview with Regenerate / Copy / Commit actions."""

from __future__ import annotations

from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from git_assistant import git_ops
from git_assistant.commit_generator import GenerationResult
from git_assistant.config import Settings
from git_assistant.ui.workers import GeneratorWorker, run_worker


class PreviewDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._thread = None
        self._worker: GeneratorWorker | None = None

        repo = settings.active_repo_entry()
        title = repo.display() if repo else "no repo"
        self.setWindowTitle(f"Commit message - {title}")
        self.setMinimumSize(620, 460)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.progress = QLabel("")
        self.progress.setStyleSheet("color: #888;")

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("The generated commit message will appear here...")

        self.regen_btn = QPushButton("Regenerate")
        self.copy_btn = QPushButton("Copy")
        self.commit_btn = QPushButton("Commit")
        self.close_btn = QPushButton("Close")
        self.regen_btn.clicked.connect(self._start)
        self.copy_btn.clicked.connect(self._on_copy)
        self.commit_btn.clicked.connect(self._on_commit)
        self.close_btn.clicked.connect(self.reject)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self.regen_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(self.copy_btn)
        btn_row.addWidget(self.commit_btn)
        btn_row.addWidget(self.close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(self.editor)
        layout.addWidget(self.progress)
        layout.addLayout(btn_row)

        self._start()

    # ---- generation --------------------------------------------------------
    def _start(self) -> None:
        self._set_busy(True)
        self.progress.setText("Starting...")
        self.status.setText("")
        worker = GeneratorWorker(self.settings)
        worker.progress.connect(self.progress.setText)
        worker.finished.connect(self._on_finished)
        worker.error.connect(self._on_error)
        self._worker = worker
        self._thread = run_worker(worker)

    def _on_finished(self, result: GenerationResult) -> None:
        self.editor.setPlainText(result.message)
        dropped = (
            f" - {len(result.dropped_files)} noise file(s) skipped"
            if result.dropped_files
            else ""
        )
        chunks = (
            f", {result.num_chunks} chunk(s)"
            if result.strategy == "map-reduce"
            else ""
        )
        self.status.setText(
            f"Strategy: {result.strategy}{chunks} - "
            f"~{result.input_tokens} input tokens / {result.input_budget} budget "
            f"(context {result.context_window}){dropped}"
        )
        self.progress.setText("Done.")
        self._set_busy(False)

    def _on_error(self, message: str) -> None:
        self.progress.setText("")
        if message != "Cancelled.":
            self.status.setText(f"Error: {message}")
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self.regen_btn.setEnabled(not busy)
        self.copy_btn.setEnabled(not busy)
        self.commit_btn.setEnabled(not busy)

    # ---- actions -----------------------------------------------------------
    def _on_copy(self) -> None:
        text = self.editor.toPlainText().strip()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self.progress.setText("Copied to clipboard.")

    def _on_commit(self) -> None:
        message = self.editor.toPlainText().strip()
        if not message:
            QMessageBox.information(self, "Nothing to commit", "The message is empty.")
            return
        repo = self.settings.active_repo_entry()
        if not repo:
            QMessageBox.warning(self, "No repo", "No active repository is selected.")
            return

        confirm = QMessageBox.question(
            self,
            "Confirm commit",
            f"Create a commit in:\n{repo.path}\n\nwith this message?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        result = git_ops.commit(repo.path, message)
        if result.ok:
            QMessageBox.information(
                self, "Committed", result.stdout.strip() or "Commit created."
            )
            self.accept()
        else:
            QMessageBox.critical(
                self,
                "Commit failed",
                result.stderr.strip() or result.stdout.strip() or "git commit failed.",
            )
