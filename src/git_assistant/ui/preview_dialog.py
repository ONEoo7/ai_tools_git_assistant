"""Commit-message generation UI.

``CommitPanel`` is the reusable widget (message editor + per-file view of what
was omitted from the prompt). It is embedded both in the main window's first tab
and in ``PreviewDialog``, the standalone window the tray's quick action opens.
"""

from __future__ import annotations

import html

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase, QGuiApplication
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
from git_assistant.config import DEFAULT_TEMPLATE_NAME, Settings
from git_assistant.diff_strategy import filter_files, split_diff
from git_assistant.providers import PROVIDERS
from git_assistant.ui.repo_picker import RepoPicker
from git_assistant.ui.workers import FunctionWorker, GeneratorWorker, run_worker

# Cap rendered lines per file so a huge diff can't freeze the view.
MAX_RENDER_LINES = 4000

_OMITTED_STYLE = "background-color:#5c1f1f; color:#ffb3b3;"
_SENT_STYLE = "color:#cfcfcf;"

# Vertical gap between distinct groups within a pane (the default ~6px spacing
# is for related widgets, not for separating sections).
SECTION_GAP = 12

# Compared against the status text to clear it once repositories exist, so a
# generation result shown in the same label is not wiped by a refresh.
NO_REPOS_MESSAGE = "No repositories configured - add one in Repositories."


def _read_staged(repo: str, mode: str, ignore_globs: list[str]) -> list[FileCoverage]:
    """Current diff as coverage entries, before anything has been sent.

    ``omitted`` is empty and the reason is "staged": nothing has reached the
    model yet, so nothing is marked red. Noise-filtered files are still shown as
    filtered, since that decision is already made.
    """
    raw = git_ops.get_diff(repo, mode)
    if not raw.strip():
        return []
    all_files = split_diff(raw)
    kept, dropped = filter_files(all_files, ignore_globs)
    dropped_set = set(dropped)
    coverage: list[FileCoverage] = []
    for f in all_files:
        lines = f.text.splitlines(keepends=True)
        is_dropped = f.path in dropped_set
        coverage.append(
            FileCoverage(
                path=f.path,
                lines=lines,
                omitted=set(range(len(lines))) if is_dropped else set(),
                reason="filtered" if is_dropped else "staged",
            )
        )
    return coverage


class CommitPanel(QWidget):
    """Generate, review and commit a message for the active repository.

    ``auto_start`` immediately kicks off a generation (the tray quick action);
    the tabbed view leaves it False so opening the window costs nothing.
    ``before_generate`` lets the host apply pending edits (e.g. unsaved settings)
    just before a run starts.
    """

    committed = pyqtSignal()  # a commit was created successfully

    def __init__(
        self,
        settings: Settings,
        auto_start: bool = True,
        before_generate=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self._before_generate = before_generate
        self._thread = None
        self._worker: GeneratorWorker | None = None
        self._push_thread = None
        self._push_worker = None
        self._coverage: list[FileCoverage] = []
        self._branches: list[str] = []  # local branches of the selected repo

        self.repo_picker = RepoPicker(settings)
        self.repo_picker.repoChanged.connect(self._on_repo_selected)

        # The branch the commit will land on. Picking one here checks it out,
        # because that is the only way the choice could mean anything: the diff
        # being described is the work tree's, and so is the commit.
        self.branch_combo = QComboBox()
        self.branch_combo.setToolTip(
            "Branch checked out in this repository.\n"
            "Choosing another one runs 'git switch'; uncommitted changes come "
            "along with you, and git refuses the switch if they would be lost."
        )
        self.branch_combo.currentIndexChanged.connect(self._on_branch_changed)

        # Each project can use its own prompt template; picking one here is what
        # assigns it to the selected repository.
        self.template_combo = QComboBox()
        self.template_combo.setToolTip(
            "Prompt template used for this repository. Manage templates in the "
            "Template tab."
        )
        self.template_combo.currentIndexChanged.connect(self._on_template_changed)

        # Which backend generates the message. Application-wide, not per repo:
        # it is an account and a connection, not a property of a project.
        self.provider_combo = QComboBox()
        self.provider_combo.setToolTip(
            "Which inference provider generates the message. Configure it "
            "under Inference Providers in the Connection & Model tab."
        )
        for provider in PROVIDERS:
            self.provider_combo.addItem(provider.display(), provider.key)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.progress = QLabel("")
        self.progress.setStyleSheet("color: #888;")

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("The generated commit message will appear here...")

        self.regen_btn = QPushButton("Regenerate" if auto_start else "Generate")
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip(
            "Re-read the staged files and clear the generated message.\n"
            "Use after staging or unstaging something outside this window."
        )
        self.copy_btn = QPushButton("Copy")
        self.commit_btn = QPushButton("Commit")
        self.push_btn = QPushButton("Push")
        self.push_btn.setToolTip(
            "Push the current branch to its remote (asks for confirmation first)."
        )
        self.regen_btn.clicked.connect(self._start)
        self.refresh_btn.clicked.connect(self._on_refresh)
        self.copy_btn.clicked.connect(self._on_copy)
        self.commit_btn.clicked.connect(self._on_commit)
        self.push_btn.clicked.connect(self._on_push)

        self.btn_row = QHBoxLayout()
        self.btn_row.addWidget(self.regen_btn)
        self.btn_row.addWidget(self.refresh_btn)
        self.btn_row.addStretch(1)
        self.btn_row.addWidget(self.copy_btn)
        self.btn_row.addWidget(self.commit_btn)
        self.btn_row.addWidget(self.push_btn)

        # ---- far-left pane: pick the repository ---------------------------
        repos_pane = QWidget()
        repos_box = QVBoxLayout(repos_pane)
        repos_box.setContentsMargins(0, 0, SECTION_GAP, 0)
        repos_box.addWidget(self.repo_picker, 1)
        repos_box.addSpacing(SECTION_GAP)
        repos_box.addWidget(QLabel("Branch:"))
        repos_box.addWidget(self.branch_combo)
        repos_box.addWidget(QLabel("Template:"))
        repos_box.addWidget(self.template_combo)
        repos_box.addWidget(QLabel("Inference Providers:"))
        repos_box.addWidget(self.provider_combo)

        # ---- left pane: the commit message -------------------------------
        left = QWidget()
        left_box = QVBoxLayout(left)
        # Keep the panes off the splitter handle; without this the labels and
        # boxes sit flush against the divider. This pane has a handle on BOTH
        # sides, so it needs the gap on both.
        left_box.setContentsMargins(SECTION_GAP, 0, SECTION_GAP, 0)
        left_box.addWidget(QLabel("Commit message"))
        left_box.addWidget(self.editor)

        # ---- right pane: staged files + what was omitted ------------------
        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(SECTION_GAP, 0, 0, 0)
        self.files_label = QLabel("Staged files")
        right_box.addWidget(self.files_label)

        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(150)
        self.file_list.currentRowChanged.connect(self._on_file_selected)
        right_box.addWidget(self.file_list)

        # The file list and the diff below it are two separate things; without a
        # gap the legend reads as a caption of the list rather than a heading
        # for the diff.
        right_box.addSpacing(SECTION_GAP)

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
        splitter.addWidget(repos_pane)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)  # repo picker stays narrow
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 4)
        splitter.setSizes([220, 420, 520])

        # Default margins, matching the other tabs. PreviewDialog zeroes its own
        # layout instead, so the standalone window keeps a single set of margins.
        layout = QVBoxLayout(self)
        # Status sits below the panes, not above them: keeping it at the top
        # pushed the content down and made this tab's spacing differ from the
        # others, which start their content at the tab margin.
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.progress)
        layout.addLayout(self.btn_row)

        self.refresh_repos()

        self.refresh_provider()

        if auto_start:
            self._start()

    # ---- repository selection ----------------------------------------------
    def refresh_repos(self) -> None:
        """Reload the repo list (call after repositories are added/removed)."""
        self.repo_picker.refresh()
        self._refresh_branches()
        self._refresh_templates()
        # The Connection & Model tab can change this while we are not looking.
        self.refresh_provider()
        self._load_staged_files()
        self._set_busy(False)
        if self.repo_picker.count() == 0:
            self.status.setText(NO_REPOS_MESSAGE)
            self._set_busy(True)  # nothing to generate against
        elif self.status.text() == NO_REPOS_MESSAGE:
            # Repositories have since been added; drop the stale warning without
            # clobbering a generation result that may be shown here.
            self.status.setText("")

    def _current_repo_path(self) -> str:
        return self.repo_picker.current_path()

    # ---- staged files, shown before anything is generated -------------------
    def _load_staged_files(self) -> None:
        """Show what is staged now, without waiting for a generation run.

        Done synchronously: it is one local ``git diff``, and running it on a
        worker meant a thread could outlive the panel that owns it - which
        aborts the process rather than merely failing.
        """
        repo = self._current_repo_path()
        if not repo:
            self._show_staged([])
            return
        try:
            coverage = _read_staged(
                repo, self.settings.diff_mode, self.settings.ignore_globs
            )
        except Exception:
            # A repo git cannot read (e.g. blocked by safe.directory) simply
            # shows nothing here; generating surfaces the real error.
            coverage = []
        self._show_staged(coverage)

    def _show_staged(self, coverage: list[FileCoverage]) -> None:
        self._populate_files(coverage)
        if not coverage:
            self.files_label.setText("Staged files - nothing staged")

    # ---- branch ------------------------------------------------------------
    def _refresh_branches(self) -> None:
        """List the repository's local branches, with the checked-out one shown.

        Populated with signals blocked: filling the box is not the user choosing
        a branch, and treating it as one would check out whatever landed first.
        """
        repo = self._current_repo_path()
        self._branches = git_ops.list_branches(repo) if repo else []
        current = git_ops.current_branch(repo) if repo else ""

        self.branch_combo.blockSignals(True)
        self.branch_combo.clear()
        for name in self._branches:
            # The branch to switch to is the item's data, so the entry below --
            # which is a state, not a branch -- cannot be checked out by name.
            self.branch_combo.addItem(name, name)
        if current and current not in self._branches:
            # Detached HEAD, or a repo git cannot read: show the state rather
            # than silently selecting a branch that is not checked out.
            label = (
                "(detached HEAD)" if current == git_ops.DETACHED_HEAD else current
            )
            self.branch_combo.insertItem(0, label, None)
            self.branch_combo.setCurrentIndex(0)
        else:
            self.branch_combo.setCurrentIndex(self.branch_combo.findData(current))
        self.branch_combo.setEnabled(bool(self._branches))
        self.branch_combo.blockSignals(False)

    def _on_branch_changed(self, _index: int) -> None:
        repo = self._current_repo_path()
        target = self.branch_combo.currentData()
        current = git_ops.current_branch(repo) if repo else ""
        if not repo or not target or target == current:
            return

        if git_ops.has_uncommitted_changes(repo) and (
            QMessageBox.question(
                self,
                "Switch branch",
                f"Switch from '{current}' to '{target}'?\n\n"
                f"Repository: {repo}\n\n"
                "This repository has uncommitted changes. They stay in the "
                f"work tree and come with you, so anything staged is committed "
                f"on '{target}' instead.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            self._refresh_branches()  # put the box back on the real branch
            return

        result = git_ops.switch_branch(repo, target)
        if not result.ok:
            QMessageBox.warning(
                self,
                "Could not switch branch",
                f"git refused to switch to '{target}':\n\n"
                f"{result.stderr.strip() or result.stdout.strip()}",
            )
            self._refresh_branches()
            return

        # The work tree is a different one now, so anything on screen describes
        # the branch we just left.
        self._clear_results()
        self._refresh_branches()
        self._load_staged_files()
        self.status.setText(f"Switched to '{target}'.")

    def _refresh_templates(self) -> None:
        """Show the template list, selecting the active repo's assignment."""
        current = self._current_repo_path()
        assigned = DEFAULT_TEMPLATE_NAME
        for r in self.settings.repos:
            if r.path == current and r.template:
                assigned = r.template
        self.template_combo.blockSignals(True)
        self.template_combo.clear()
        self.template_combo.addItems(self.settings.template_names())
        idx = self.template_combo.findText(assigned)
        self.template_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.template_combo.blockSignals(False)

    def refresh_provider(self) -> None:
        """Show the stored provider, without treating that as a user choice."""
        index = self.provider_combo.findData(self.settings.provider)
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.provider_combo.blockSignals(False)

    def _on_provider_changed(self, _index: int) -> None:
        key = self.provider_combo.currentData()
        if not key or key == self.settings.provider:
            return
        self.settings.provider = key
        self.settings.save()

    def _on_template_changed(self, _index: int) -> None:
        repo = self._current_repo_path()
        name = self.template_combo.currentText()
        if not repo or not name:
            return
        self.settings.set_repo_template(repo, name)
        self.settings.save()

    def _clear_results(self) -> None:
        """Drop everything a generation run produced.

        A message describing a diff that is no longer the staged one is worse
        than an empty box: it reads as current. So the editor, the file list,
        the diff pane and the coverage all go together, and the button goes
        back to saying "Generate" because there is no longer a result to
        regenerate.
        """
        self.editor.clear()
        self.file_list.clear()
        self.diff_view.clear()
        self._coverage = []
        self.progress.setText("")
        self.regen_btn.setText("Generate")
        self.status.setText("")

    def _on_refresh(self) -> None:
        """Re-read what is staged, discarding the message written for the old set.

        Staging happens outside this window -- in a terminal, an IDE, another
        git client -- and nothing here notices. Without this the panel shows
        whatever was staged when the tab was opened, and generating from it
        produces a message for the wrong changes.
        """
        self._clear_results()
        # Branches are switched outside this window too, and the box would
        # otherwise keep naming the one that was checked out when it was opened.
        self._refresh_branches()
        self._load_staged_files()

    def _on_repo_selected(self, path: str = "") -> None:
        """React to the picker's selection (it already updated the settings)."""
        if not path:
            return
        # Branches and templates are both per repository, so show the new one's.
        self._refresh_branches()
        self._refresh_templates()
        # Results belong to the previous repo - clear them rather than mislead.
        self._clear_results()
        # Show the new repository's staged files right away.
        self._load_staged_files()

    # ---- generation --------------------------------------------------------
    def _start(self) -> None:
        if self._before_generate is not None:
            # Pick up any settings edited in sibling tabs but not yet saved.
            self._before_generate()
        self._set_busy(True)
        self.regen_btn.setText("Regenerate")
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
        summarized = sum(1 for c in self._coverage if c.reason == "summarized")
        # Before a run there is nothing to report about what reached the model.
        if any(c.reason == "staged" for c in self._coverage):
            kept = sum(1 for c in self._coverage if c.reason == "staged")
            filtered = len(self._coverage) - kept
            note = f", {filtered} filtered as noise" if filtered else ""
            self.files_label.setText(f"Staged files ({kept}){note}")
        elif total_omitted:
            self.files_label.setText(
                f"Staged files ({len(self._coverage)}) - {incomplete} with omitted "
                f"content, {total_omitted} line(s) not sent"
            )
        elif summarized:
            self.files_label.setText(
                f"Staged files ({len(self._coverage)}) - all content reached the "
                f"model, {summarized} via summary (no raw diff omitted)"
            )
        else:
            self.files_label.setText(
                f"Staged files ({len(self._coverage)}) - all content sent to the model"
            )

        for cov in self._coverage:
            if cov.reason == "staged":
                suffix = ""  # nothing sent yet, so nothing to report
            elif cov.reason == "filtered":
                suffix = "  [filtered as noise - fully omitted]"
            elif cov.omitted_count:
                suffix = f"  [{cov.omitted_count}/{len(cov.lines)} lines omitted]"
            elif cov.reason == "summarized":
                suffix = "  [fully sent - as summary]"
            else:
                suffix = "  [fully sent]"
            item = QListWidgetItem(f"{cov.path}{suffix}")
            item.setToolTip(cov.path)
            if cov.omitted_count:
                item.setForeground(Qt.GlobalColor.red)
            elif cov.reason == "summarized":
                item.setForeground(Qt.GlobalColor.darkYellow)
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
        # Refresh clears the very widgets a running generation is about to
        # fill, so it waits with the rest.
        self.refresh_btn.setEnabled(not busy)
        self.copy_btn.setEnabled(not busy)
        self.commit_btn.setEnabled(not busy)
        # Switching branch mid-run changes the diff the worker is describing.
        self.branch_combo.setEnabled(not busy and bool(self._branches))

    # ---- actions -----------------------------------------------------------
    def _on_copy(self) -> None:
        text = self.editor.toPlainText().strip()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        self.progress.setText("Copied to clipboard.")

    def _on_push(self) -> None:
        repo = self.settings.active_repo_entry()
        if not repo:
            QMessageBox.warning(self, "No repo", "No active repository is selected.")
            return

        branch = git_ops.current_branch(repo.path)
        upstream = git_ops.get_upstream(repo.path)
        ahead = git_ops.unpushed_count(repo.path)

        if upstream:
            target = f"its upstream '{upstream}'"
            if ahead == 0:
                QMessageBox.information(
                    self,
                    "Nothing to push",
                    f"'{branch}' is already up to date with {upstream}.",
                )
                return
            count = f"{ahead} commit(s)" if ahead is not None else "commits"
        else:
            target = "a new upstream branch on 'origin'"
            count = "this branch"

        confirm = QMessageBox.question(
            self,
            "Confirm push",
            f"Push {count} from '{branch}' to {target}?\n\n"
            f"Repository: {repo.path}\n\n"
            "This publishes your commits to the remote.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.push_btn.setEnabled(False)
        self.progress.setText(f"Pushing '{branch}'...")
        worker = FunctionWorker(lambda p=repo.path: git_ops.push(p))
        worker.finished.connect(self._on_push_done)
        worker.error.connect(self._on_push_error)
        self._push_worker = worker
        self._push_thread = run_worker(worker)

    def _on_push_done(self, result) -> None:
        self.push_btn.setEnabled(True)
        # git reports push progress on stderr even when successful.
        detail = (result.stderr.strip() or result.stdout.strip() or "").strip()
        if result.ok:
            self.progress.setText("Pushed.")
            QMessageBox.information(self, "Pushed", detail or "Push complete.")
        else:
            self.progress.setText("Push failed.")
            QMessageBox.critical(self, "Push failed", detail or "git push failed.")

    def _on_push_error(self, message: str) -> None:
        self.push_btn.setEnabled(True)
        self.progress.setText("Push failed.")
        QMessageBox.critical(self, "Push failed", message)

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
            self.committed.emit()
        else:
            QMessageBox.critical(
                self,
                "Commit failed",
                result.stderr.strip() or result.stdout.strip() or "git commit failed.",
            )


class PreviewDialog(QDialog):
    """Standalone commit-message window (the tray's quick action)."""

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        repo = settings.active_repo_entry()
        self.setWindowTitle(
            f"Commit message - {repo.display() if repo else 'no repo'}"
        )
        self.setMinimumSize(1100, 560)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )

        self.panel = CommitPanel(settings, auto_start=True, parent=self)
        self.panel.committed.connect(self.accept)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        self.panel.btn_row.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)  # the panel already has margins
        layout.addWidget(self.panel)
