"""Editable commit-message preview with Regenerate / Copy / Commit actions."""

from __future__ import annotations

import html

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFontDatabase, QGuiApplication
from PyQt6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from git_assistant import git_ops
from git_assistant.commit_generator import FileCoverage, GenerationResult
from git_assistant.config import Settings
from git_assistant.ui.workers import GeneratorWorker, run_worker

# Cap rendered lines per file so a huge diff can't freeze the view.
MAX_RENDER_LINES = 4000

_OMITTED_STYLE = "background-color:#5c1f1f; color:#ffb3b3;"
_SENT_STYLE = "color:#cfcfcf;"


class PreviewDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._thread = None
        self._worker: GeneratorWorker | None = None

        repo = settings.active_repo_entry()
        title = repo.display() if repo else "no repo"
        self.setWindowTitle(f"Commit message - {title}")
        self.setMinimumSize(1100, 560)
        self._coverage: list[FileCoverage] = []

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

        # ---- left pane: the commit message -------------------------------
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        left_box.addWidget(QLabel("Commit message"))
        left_box.addWidget(self.editor)

        # ---- right pane: staged files + what was omitted ------------------
        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        self.files_label = QLabel("Staged files")
        right_box.addWidget(self.files_label)

        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(150)
        self.file_list.currentRowChanged.connect(self._on_file_selected)
        right_box.addWidget(self.file_list)

        legend = QLabel(
            'Diff sent to the model - <span style="%s">&nbsp;red = omitted '
            "(never reached the model)&nbsp;</span>" % _OMITTED_STYLE
        )
        legend.setTextFormat(Qt.TextFormat.RichText)
        right_box.addWidget(legend)

        self.diff_view = QTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.diff_view.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        right_box.addWidget(self.diff_view, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)

        layout = QVBoxLayout(self)
        layout.addWidget(self.status)
        layout.addWidget(splitter, 1)
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
        self._populate_files(result.file_coverage)
        self.progress.setText("Done.")
        self._set_busy(False)

    # ---- omitted-content view ---------------------------------------------
    def _populate_files(self, coverage: list[FileCoverage]) -> None:
        # Files with omissions first, so problems are immediately visible.
        self._coverage = sorted(coverage, key=lambda c: -c.omitted_count)
        self.file_list.clear()

        total_omitted = sum(c.omitted_count for c in self._coverage)
        incomplete = sum(1 for c in self._coverage if not c.fully_sent)
        if total_omitted:
            self.files_label.setText(
                f"Staged files ({len(self._coverage)}) - {incomplete} with omitted "
                f"content, {total_omitted} line(s) not sent"
            )
        else:
            self.files_label.setText(
                f"Staged files ({len(self._coverage)}) - all content sent to the model"
            )

        for cov in self._coverage:
            if cov.reason == "filtered":
                suffix = "  [filtered as noise - fully omitted]"
            elif cov.omitted_count:
                suffix = f"  [{cov.omitted_count}/{len(cov.lines)} lines omitted]"
            else:
                suffix = "  [fully sent]"
            item = QListWidgetItem(f"{cov.path}{suffix}")
            item.setToolTip(cov.path)
            if cov.omitted_count:
                item.setForeground(Qt.GlobalColor.red)
            self.file_list.addItem(item)

        if self._coverage:
            self.file_list.setCurrentRow(0)
        else:
            self.diff_view.clear()

    def _on_file_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._coverage):
            self.diff_view.clear()
            return
        self.diff_view.setHtml(self._render_coverage(self._coverage[row]))

    def _render_coverage(self, cov: FileCoverage) -> str:
        parts = ["<pre style='margin:0; white-space:pre;'>"]
        truncated_note = ""
        lines = cov.lines
        if len(lines) > MAX_RENDER_LINES:
            lines = lines[:MAX_RENDER_LINES]
            truncated_note = (
                f"\n[view truncated: showing first {MAX_RENDER_LINES} of "
                f"{len(cov.lines)} lines]"
            )
        for i, raw in enumerate(lines):
            text = html.escape(raw.rstrip("\n")) or "&nbsp;"
            style = _OMITTED_STYLE if i in cov.omitted else _SENT_STYLE
            parts.append(f"<span style='{style}'>{text}</span>")
        if truncated_note:
            parts.append(f"<span style='color:#888;'>{html.escape(truncated_note)}</span>")
        parts.append("</pre>")
        return "<br>".join(parts)

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
