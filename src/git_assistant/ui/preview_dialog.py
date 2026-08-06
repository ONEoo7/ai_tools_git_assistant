"""Commit-message generation UI.

``CommitPanel`` is the reusable widget (message editor + per-file view of what
was omitted from the prompt). It is embedded both in the main window's first tab
and in ``PreviewDialog``, the standalone window the tray's quick action opens.
"""

from __future__ import annotations

import html
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFontDatabase, QGuiApplication
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import commit_history, commit_style, estimate, git_ops
from git_assistant.commit_generator import FileCoverage, GenerationResult
from git_assistant.config import DEFAULT_TEMPLATE_NAME, Settings
from git_assistant.diff_strategy import filter_files, split_diff
from git_assistant.providers import PROVIDERS
from git_assistant.ui.estimate_dialog import confirm
from git_assistant.ui.repo_picker import RepoPicker
from git_assistant.ui.side_panel import SidePanel
from git_assistant.ui.workers import (
    FunctionWorker,
    GeneratorWorker,
    RetryWorker,
    run_worker,
)

# Cap rendered lines per file so a huge diff can't freeze the view.
MAX_RENDER_LINES = 4000

_OMITTED_STYLE = "background-color:#5c1f1f; color:#ffb3b3;"
_SENT_STYLE = "color:#cfcfcf;"

# The length readout under the editor: quiet until it is over a hard cap.
_LENGTH_STYLE = "color: #888;"
_OVER_STYLE = "color: #b36b00;"

# Vertical gap between distinct groups within a pane (the default ~6px spacing
# is for related widgets, not for separating sections).
SECTION_GAP = 12

# Compared against the status text to clear it once repositories exist, so a
# generation result shown in the same label is not wiped by a refresh.
NO_REPOS_MESSAGE = "No repositories configured - add one in Repositories."


def _history_note(repo: str, runs: list, settings) -> str:
    if not repo:
        return ""
    if not runs:
        return "No messages generated for this repository yet."
    if settings.commit_history_limit <= 0:
        return "Keeping every generated message."
    return f"Keeping the newest {settings.commit_history_limit} message(s)."


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
        #: The window's shared progress bar, set by whoever owns this panel.
        #: None when there is none -- a panel in a test, or the tray's own
        #: window -- and every report to it is then a no-op.
        self.busy = None
        self._worker: GeneratorWorker | None = None
        self._push_thread = None
        self._push_worker = None
        self._coverage: list[FileCoverage] = []
        self._branches: list[str] = []  # local branches of the selected repo
        #: The recorded run the editor is showing, so "has this been edited?"
        #: has an answer before a stored message replaces it.
        self._shown_run = None

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

        # The provider is half of what a generation uses; the model is the
        # other half, and it is chosen a tab away. Named here as the Audit and
        # Code Review tabs name it, so the three read alike.
        self.provider_label = QLabel("")
        self.provider_label.setWordWrap(True)
        self.provider_label.setStyleSheet("color: #888;")
        # Ignored, not Preferred: a long model id must not widen the pane it
        # sits in, which is the narrowest of the three.
        self.provider_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.progress = QLabel("")
        self.progress.setStyleSheet("color: #888;")

        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText("The generated commit message will appear here...")

        self.length_label = QLabel("")
        self.length_label.setWordWrap(True)

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
        repos_box.addWidget(self.provider_label)

        # ---- left pane: the commit message -------------------------------
        left = QWidget()
        left_box = QVBoxLayout(left)
        # Keep the panes off the splitter handle; without this the labels and
        # boxes sit flush against the divider. This pane has a handle on BOTH
        # sides, so it needs the gap on both.
        left_box.setContentsMargins(SECTION_GAP, 0, SECTION_GAP, 0)
        left_box.addWidget(QLabel("Commit message"))
        left_box.addWidget(self.editor)
        # Under the editor and live, not only after a generation: the message
        # is editable, and a length rule that only judged the model would be
        # silent about the line the user typed over it.
        left_box.addWidget(self.length_label)
        self.editor.textChanged.connect(self._refresh_length)
        self._refresh_length()

        # ---- right pane: staged files + what was omitted ------------------
        right = QWidget()
        right_box = QVBoxLayout(right)
        # A handle on both sides, so a gap on both: with one only, the file list
        # sits flush against the divider on the right and inset on the left,
        # which reads as a misalignment rather than as a margin.
        right_box.setContentsMargins(SECTION_GAP, 0, SECTION_GAP, 0)
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
        splitter.addWidget(self._build_side_pane())
        splitter.setStretchFactor(0, 1)  # repo picker stays narrow
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 4)
        splitter.setStretchFactor(3, 3)
        splitter.setSizes([200, 380, 440, 380])

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

    # ---- previous runs, and what was sent to the model ----------------------
    def _build_side_pane(self) -> QWidget:
        """The shared right-hand pane; see git_assistant.ui.side_panel."""
        self.side_panel = SidePanel(
            self._build_history_pane(),
            repo_name=lambda: Path(self._current_repo_path()).name,
            margins=(SECTION_GAP, 0, 0, 0),
        )
        self.calls_pane.noted.connect(self.progress.setText)
        return self.side_panel

    def _build_history_pane(self) -> QWidget:
        """Every message generated for this repository.

        Regenerating is the normal thing to do, and the second message is often
        worse than the first. Until this list existed, the first was gone.
        """
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 0, 0, 0)

        self.runs_tree = QTreeWidget()
        self.runs_tree.setHeaderLabels(["When", "Message"])
        self.runs_tree.setRootIsDecorated(False)
        self.runs_tree.setColumnWidth(0, 110)
        self.runs_tree.itemDoubleClicked.connect(lambda *_: self._on_open_run())
        self.runs_tree.itemSelectionChanged.connect(self._on_run_selection)
        self.runs_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.runs_tree.customContextMenuRequested.connect(self._on_runs_menu)
        box.addWidget(self.runs_tree, 1)

        row = QHBoxLayout()
        self.open_run_btn = QPushButton("Open")
        self.open_run_btn.setToolTip("Put this message back in the editor.")
        self.open_run_btn.clicked.connect(self._on_open_run)
        self.delete_run_btn = QPushButton("Delete")
        self.delete_run_btn.clicked.connect(self._on_delete_run)
        for button in (self.open_run_btn, self.delete_run_btn):
            button.setEnabled(False)
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)

        self.history_note = QLabel("")
        self.history_note.setWordWrap(True)
        self.history_note.setStyleSheet("color: #888;")
        box.addWidget(self.history_note)
        return pane

    #: The calls half of the side pane, which is what a run talks to.
    calls_pane = property(lambda self: self.side_panel.calls)

    # The pane owns these now. Kept as names on the panel because that is what
    # the window, and the tests that pin this behaviour, reach for.
    calls_label = property(lambda self: self.calls_pane.calls_label)
    calls_list = property(lambda self: self.calls_pane.calls_list)
    call_view = property(lambda self: self.calls_pane.call_view)
    copy_call_btn = property(lambda self: self.calls_pane.copy_call_btn)
    copy_all_calls_btn = property(lambda self: self.calls_pane.copy_all_calls_btn)
    save_calls_btn = property(lambda self: self.calls_pane.save_calls_btn)
    _calls = property(lambda self: self.calls_pane.calls)

    def _reset_calls(self) -> None:
        self.calls_pane.reset()

    def _on_call(self, call) -> None:
        self.calls_pane.add_call(call)

    # ---- repository selection ----------------------------------------------
    def refresh_repos(self) -> None:
        """Reload the repo list (call after repositories are added/removed)."""
        self.repo_picker.refresh()
        self._refresh_branches()
        self._refresh_templates()
        # The Connection & Model tab can change this while we are not looking.
        self.refresh_provider()
        self._load_staged_files()
        self._refresh_history()
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

    def _refresh_length(self) -> None:
        """Say how long the message is, and whether that is a problem.

        Reported, never enforced: cutting a subject line at 72 characters
        would produce exactly the mangled subject the limit exists to prevent.
        """
        limits = commit_style.Limits.of(self.settings)
        if not limits.asks_anything() or not self.editor.toPlainText().strip():
            self.length_label.setText("")
            return
        measured = commit_style.measure(self.editor.toPlainText(), limits)
        note = measured.note()
        self.length_label.setText(
            f"{measured.label()}  -  {note}" if note else measured.label()
        )
        self.length_label.setStyleSheet(
            _OVER_STYLE if measured.too_long else _LENGTH_STYLE
        )

    def refresh_provider(self) -> None:
        """Show the stored provider, without treating that as a user choice."""
        index = self.provider_combo.findData(self.settings.provider)
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.provider_combo.blockSignals(False)
        # The combo names the provider; the model is the other half of what a
        # generation will actually use, and it is chosen in Connection & Model.
        model = self.settings.active_model() or "no model selected"
        self.provider_label.setText(f"Model: {model}")

    def _on_provider_changed(self, _index: int) -> None:
        key = self.provider_combo.currentData()
        if not key or key == self.settings.provider:
            return
        self.settings.provider = key
        self.settings.save()
        self.refresh_provider()  # the model line belongs to the new provider

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
        self._shown_run = None
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
        self._refresh_history()

    # ---- generation --------------------------------------------------------
    def _start(self) -> None:
        if self._before_generate is not None:
            # Pick up any settings edited in sibling tabs but not yet saved.
            self._before_generate()
        # What this is about to send, while it can still be declined.
        if not confirm(self, estimate.for_commit(self.settings)):
            return
        self._set_busy(True)
        self.regen_btn.setText("Regenerate")
        self.progress.setText("Starting...")
        self.status.setText("")
        self._reset_calls()  # these belong to the run about to start
        worker = GeneratorWorker(self.settings)
        worker.progress.connect(self.progress.setText)
        worker.call.connect(self._on_call)
        worker.finished.connect(self._on_finished)
        worker.error.connect(self._on_error)
        self._worker = worker
        self._thread = run_worker(worker)

    def _on_finished(self, result: GenerationResult) -> None:
        self.editor.setPlainText(result.message)
        self._record(result)
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
        # Said out loud: a message written from ten notes when twelve chunks
        # were sent describes less than it appears to.
        if result.blank_notes:
            chunks += f" ({result.blank_notes} returned nothing)"
        self.status.setText(
            f"Strategy: {result.strategy}{chunks} - "
            f"~{result.input_tokens} input tokens / {result.input_budget} budget "
            f"(context {result.context_window}){dropped}"
        )
        self._populate_files(result.file_coverage)
        self.progress.setText("Done.")
        self._set_busy(False)
        self._offer_retry(result)

    # ---- asking again when it came back too long -----------------------------
    def _offer_retry(self, result: GenerationResult) -> None:
        """Ask whether to pay for a shorter one. Never decides on its own.

        The message is kept and shown either way: a run the user declines to
        redo still produced something, and throwing it away to make a point
        about its length would be worse than the length.
        """
        measured = commit_style.measure(
            result.message, commit_style.Limits.of(self.settings)
        )
        if not measured.too_long or result.retry is None:
            return
        note = measured.retry_note()
        priced = estimate.for_retry(self.settings, result.retry, note)
        # The same window every other spend goes through, so the answer to
        # "what will this cost" is in the place the user already knows.
        priced.problem = ""
        if not confirm(self, priced):
            self.status.setText(
                f"{measured.note()} Kept as it is; edit it or press Regenerate."
            )
            return
        self._start_retry(result.retry, note)

    def _start_retry(self, retry, note: str) -> None:
        self._set_busy(True)
        self.progress.setText("Asking for a shorter message...")
        worker = RetryWorker(self.settings, retry, note)
        worker.progress.connect(self.progress.setText)
        worker.call.connect(self._on_call)
        worker.finished.connect(self._on_retried)
        worker.error.connect(self._on_error)
        self._worker = worker
        self._thread = run_worker(worker)

    def _on_retried(self, message: str) -> None:
        """The second answer. Shown whether or not it is any shorter."""
        self.editor.setPlainText(message)
        self._set_busy(False)
        measured = commit_style.measure(
            message, commit_style.Limits.of(self.settings)
        )
        # Not offered a third time: a model that ignored the instruction once
        # will ignore it again, and asking in a loop spends money on that.
        self.progress.setText(
            "Still over the limit - shorten it by hand."
            if measured.too_long
            else "Done."
        )

    # ---- previous runs -----------------------------------------------------
    def _record(self, result: GenerationResult) -> None:
        """Keep the message, so regenerating cannot lose a better earlier one."""
        repo = self._current_repo_path()
        if not repo:
            return
        head = git_ops._run(repo, ["rev-parse", "HEAD"])
        stored, problem = commit_history.record(
            repo,
            result,
            branch=git_ops.current_branch(repo),
            head=head.stdout.strip() if head.ok else "",
            dirty=git_ops.has_uncommitted_changes(repo),
            model=self.settings.active_model(),
            provider=self.settings.provider,
            limit=self.settings.commit_history_limit,
        )
        self._shown_run = stored
        if problem:
            self.progress.setText(f"Not saved to history: {problem}")
        self._refresh_history(select=stored)

    def _refresh_history(self, select=None) -> None:
        repo = self._current_repo_path()
        self.runs_tree.clear()
        runs = commit_history.list_runs(repo) if repo else []
        for stored in runs:
            item = QTreeWidgetItem([stored.when_label(), stored.result_label()])
            item.setData(0, Qt.ItemDataRole.UserRole, stored)
            item.setToolTip(0, stored.describe())
            item.setToolTip(1, stored.describe())
            if stored.pinned:
                item.setText(0, f"📌 {stored.when_label()}")
            if stored.committed:
                item.setForeground(1, Qt.GlobalColor.darkGreen)
            if select is not None and stored.run_id == select.run_id:
                self.runs_tree.setCurrentItem(item)
            self.runs_tree.addTopLevelItem(item)
        self._on_run_selection()
        self.history_note.setText(_history_note(repo, runs, self.settings))

    def _selected_runs(self) -> list:
        return [
            item.data(0, Qt.ItemDataRole.UserRole)
            for item in self.runs_tree.selectedItems()
        ]

    def _on_run_selection(self) -> None:
        chosen = self._selected_runs()
        self.open_run_btn.setEnabled(len(chosen) == 1)
        self.delete_run_btn.setEnabled(bool(chosen))

    def _on_open_run(self) -> None:
        chosen = self._selected_runs()
        if len(chosen) != 1:
            return
        stored = chosen[0]
        current = self.editor.toPlainText().strip()
        # Only ask when there is something to lose: an edited message the user
        # typed is not the same as the one this panel put there.
        if current and current != (
            self._shown_run.message.strip() if self._shown_run else ""
        ):
            if (
                QMessageBox.question(
                    self,
                    "Replace the message",
                    "Replace what is in the editor with the message from "
                    f"{stored.when_label()}?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
        self.editor.setPlainText(stored.message)
        self._shown_run = stored
        self.progress.setText(f"Showing the message generated at {stored.when_label()}.")

    def _on_delete_run(self) -> None:
        chosen = self._selected_runs()
        if not chosen:
            return
        for stored in chosen:
            commit_history.delete_run(stored)
            if self._shown_run is not None and stored.run_id == self._shown_run.run_id:
                self._shown_run = None
        self._refresh_history()

    def _on_runs_menu(self, point) -> None:
        chosen = self._selected_runs()
        if not chosen:
            return
        menu = QMenu(self)
        if len(chosen) == 1:
            menu.addAction("Open", self._on_open_run)
            pinned = chosen[0].pinned
            menu.addAction(
                "Unpin" if pinned else "Pin (never drop this one)",
                lambda: self._on_pin(chosen[0], not pinned),
            )
        menu.addAction("Delete", self._on_delete_run)
        menu.addSeparator()
        menu.addAction("Clear this repository's messages", self._on_clear_history)
        menu.exec(self.runs_tree.viewport().mapToGlobal(point))

    def _on_pin(self, stored, pinned: bool) -> None:
        commit_history.set_pinned(stored, pinned)
        self._refresh_history(select=stored)

    def _on_clear_history(self) -> None:
        repo = self._current_repo_path()
        if not repo:
            return
        if (
            QMessageBox.question(
                self,
                "Clear messages",
                f"Forget every generated message for {Path(repo).name}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            == QMessageBox.StandardButton.Yes
        ):
            commit_history.clear_repo(repo)
            self._shown_run = None
            self._refresh_history()

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


    # ---- the window's shared progress bar --------------------------------
    def _busy_start(self, what: str) -> None:
        """Say a task has begun, if this panel is in a window that has a bar."""
        if self.busy is not None:
            self.busy.start(self, what)

    def _busy_step(self, percent: int) -> None:
        if self.busy is not None:
            self.busy.step(self, percent)

    def _busy_stop(self) -> None:
        if self.busy is not None:
            self.busy.stop(self)

    def _set_busy(self, busy: bool) -> None:
        if busy:
            self._busy_start("Commit message")
        else:
            self._busy_stop()
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
            # Which of twenty stored messages is the one that shipped is the
            # first thing asked of the list; record it while we know.
            if self._shown_run is not None and message == self._shown_run.message.strip():
                commit_history.mark_committed(self._shown_run)
                self._refresh_history(select=self._shown_run)
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
