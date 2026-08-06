"""Audit tab: run a repository audit and read what it found.

Same shape as the Tags tab -- repository on the left, the work on the right --
because both answer a question about one repository and the tabs should not
each invent their own layout.

Two views of the same run: the report, and the measurements it was built from.
The second exists because a reader who does not trust a paragraph should not
have to trust it: every figure in the prose is in that tree.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import agents
from git_assistant.agents import compare, history
from git_assistant.agents import report as report_mod
from git_assistant import estimate
from git_assistant.config import Settings, norm_path
from git_assistant.providers import PROVIDERS
from git_assistant.ui.estimate_dialog import confirm
from git_assistant.ui.preview_dialog import SECTION_GAP
from git_assistant.ui.repo_picker import RepoPicker
from git_assistant.ui.side_panel import SidePanel
from git_assistant.ui.workers import AgentWorker, run_worker

NO_REPOS_MESSAGE = "No repositories configured - add one in Repositories."
INFO_COLOUR = "color: #8ab;"
MUTED_COLOUR = "color: #888;"
COMPARISON_TAB = "Comparison"


def _headline_text(run, previous) -> str:
    """The run's own number, and which way it moved against the run below it.

    Read from the history index, so drawing the list opens no run file.
    """
    keys = compare.headline_keys(run.agent_id)
    shown = next((k for k in keys if k in run.headline), "")
    if not shown:
        return f"{run.warnings} warning(s)" if run.warnings else ""
    value = run.headline[shown].get("value", "")
    if previous is None or shown not in previous.headline:
        return str(value)
    direction = compare.direction_of(
        previous.headline[shown].get("raw"),
        run.headline[shown].get("raw"),
        compare.resolve_polarity(
            run.agent_id, shown, same_head=bool(run.head) and run.head == previous.head
        ),
    )
    return f"{value}  {direction.arrow()}"


def _run_tooltip(run) -> str:
    bits = [
        f"{run.when_label()} — {run.commit_label()}",
        f"agent: {run.agent_id}",
        "narrated by the model" if run.narrated else "written from the measurements",
    ]
    if run.fast:
        bits.append("fast mode (totals only)")
    if run.warnings:
        bits.append(f"{run.warnings} warning(s)")
    if run.pinned:
        bits.append("pinned: kept regardless of the retention limit")
    return "\n".join(bits)


def _history_note(repo: str, runs: list, settings) -> str:
    if not repo:
        return ""
    if not runs:
        return "No previous runs for this repository yet."
    note = f"Keeping the newest {settings.agent_history_limit} run(s) per agent."
    if settings.agent_history_limit <= 0:
        note = "Keeping every run."
    unreadable = history.unreadable_count(repo)
    if unreadable:
        note += f" {unreadable} stored run(s) could not be read."
    return note


class AgentsPanel(QWidget):
    """Pick a repository, pick an agent, run it, read the report."""

    def __init__(self, settings: Settings, before_run=None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._before_run = before_run
        self._thread = None
        #: The window's shared progress bar, set by whoever owns this panel.
        #: None when there is none -- a panel in a test, or the tray's own
        #: window -- and every report to it is then a no-op.
        self.busy = None
        self._worker = None
        self._report = None
        #: (repository, agent) the displayed report describes. What is on screen
        #: has to stop being shown when it stops being about what is selected.
        self._shown_key: tuple[str, str] | None = None
        self._diff = None

        self.repo_picker = RepoPicker(settings)
        self.repo_picker.repoChanged.connect(self._on_repo_changed)

        self.agent_list = QListWidget()
        self.agent_list.setMaximumHeight(70)
        for info in agents.infos():
            item = QListWidgetItem(info.label)
            item.setData(Qt.ItemDataRole.UserRole, info.id)
            item.setToolTip(info.description)
            self.agent_list.addItem(item)
        self.agent_list.currentRowChanged.connect(self._on_agent_changed)

        self.agent_description = QLabel("")
        self.agent_description.setWordWrap(True)
        self.agent_description.setStyleSheet(MUTED_COLOUR)
        # A wrapped label asks for room for its longest line, which would push
        # this pane wider than the picker beside it needs. Let it take whatever
        # width the pane ends up with instead of arguing for one.
        self.agent_description.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        # Short labels: a checkbox cannot wrap, so its text sets the minimum
        # width of this whole pane. The detail lives in the tooltips.
        self.narrate_check = QCheckBox("Write the narrative")
        self.narrate_check.setToolTip(
            "The measurements are taken by git either way. With this on, the "
            "configured provider writes the prose around them - and any figure "
            "it invents is rejected."
        )
        self.narrate_check.setChecked(settings.agents_narrate)
        self.narrate_check.toggled.connect(self._on_options_changed)

        self.fast_check = QCheckBox("Fast mode")
        self.fast_check.setToolTip(
            "Skips the per-file breakdown of history, which is the slow part on "
            "a large repository."
        )
        self.fast_check.setChecked(settings.agent_fast_mode)
        self.fast_check.toggled.connect(self._on_options_changed)

        # The consistency audit's rules. Here rather than in Advanced because
        # they are read only by an agent, and this is where an agent is run.
        self.stale_months_spin = QSpinBox()
        self.stale_months_spin.setRange(0, 120)
        self.stale_months_spin.setSuffix(" months")
        self.stale_months_spin.setToolTip(
            "A branch untouched for longer than this counts as stale in the "
            "repository consistency audit. Nothing is deleted either way."
        )
        self.stale_months_spin.valueChanged.connect(self._on_rules_changed)

        self.merged_only_check = QCheckBox("Only propose merged branches")
        self.merged_only_check.setToolTip(
            "On, deletion is proposed only for branches whose commits are "
            "already on the default branch. Off, an unmerged branch can be "
            "proposed -- and its commits exist nowhere else."
        )
        self.merged_only_check.toggled.connect(self._on_rules_changed)

        self.keep_unpushed_check = QCheckBox("Keep unpushed work")
        self.keep_unpushed_check.setToolTip(
            "Never propose a branch holding commits its upstream has not got."
        )
        self.keep_unpushed_check.toggled.connect(self._on_rules_changed)

        self.protect_edit = QLineEdit()
        self.protect_edit.setToolTip(
            "Branch names never proposed for deletion, comma separated. "
            "Globs work: release/* spares every release branch. The default "
            "branch is protected whether or not it is listed."
        )
        self.protect_edit.editingFinished.connect(self._on_rules_changed)
        self._show_rules(settings.stale_rules())

        # The same application-wide setting the Generate tab exposes, offered
        # here too so the provider can be switched where the run is started.
        # Every copy re-reads it on refresh, so they cannot disagree.
        self.provider_combo = QComboBox()
        self.provider_combo.setToolTip(
            "Which inference provider writes the report's prose. Application-"
            "wide: the same choice as in the Connection & Model tab."
        )
        for provider in PROVIDERS:
            self.provider_combo.addItem(provider.display(), provider.key)
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)

        self.provider_label = QLabel("")
        self.provider_label.setWordWrap(True)
        self.provider_label.setStyleSheet(MUTED_COLOUR)
        self.provider_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        # ---- right pane -----------------------------------------------------
        self.header = QLabel("")
        font = self.header.font()
        font.setBold(True)
        self.header.setFont(font)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(INFO_COLOUR)


        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        self.view.setPlaceholderText(
            "Pick an agent and press Run. Nothing in the repository is changed."
        )

        self.facts_tree = QTreeWidget()
        self.facts_tree.setHeaderLabels(["Item", "Value"])
        self.facts_tree.setColumnWidth(0, 320)

        self.diff_view = QTextBrowser()
        self.diff_view.setOpenExternalLinks(False)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.view, "Report")
        self.tabs.addTab(self.facts_tree, "Measurements")
        self.diff_tab = self.tabs.addTab(self.diff_view, COMPARISON_TAB)
        self.tabs.setTabEnabled(self.diff_tab, False)

        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self._on_run)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.clicked.connect(self._on_copy)
        self.export_btn = QPushButton("Export...")
        self.export_btn.clicked.connect(self._on_export)
        for button in (self.copy_btn, self.export_btn):
            button.setEnabled(False)

        buttons = QHBoxLayout()
        buttons.addWidget(self.run_btn)
        buttons.addWidget(self.cancel_btn)
        buttons.addStretch(1)
        buttons.addWidget(self.copy_btn)
        buttons.addWidget(self.export_btn)

        content = QWidget()
        layout = QVBoxLayout(content)
        # A handle on both sides, so a gap on both: with one only, the report
        # sits flush against the divider on the right and inset on the left,
        # which reads as a misalignment rather than as a margin.
        layout.setContentsMargins(SECTION_GAP, 0, SECTION_GAP, 0)
        layout.addWidget(self.header)
        layout.addWidget(self.status)
        layout.addWidget(self.tabs, 1)
        layout.addLayout(buttons)

        picker_pane = QWidget()
        picker_box = QVBoxLayout(picker_pane)
        picker_box.setContentsMargins(0, 0, SECTION_GAP, 0)
        picker_box.addWidget(self.repo_picker, 1)
        picker_box.addSpacing(SECTION_GAP)
        picker_box.addWidget(QLabel("Agent:"))
        picker_box.addWidget(self.agent_list)
        picker_box.addWidget(self.agent_description)
        picker_box.addSpacing(SECTION_GAP)
        picker_box.addWidget(self.narrate_check)
        picker_box.addWidget(self.fast_check)
        picker_box.addSpacing(SECTION_GAP)
        picker_box.addWidget(QLabel("Stale branches after:"))
        picker_box.addWidget(self.stale_months_spin)
        picker_box.addWidget(self.merged_only_check)
        picker_box.addWidget(self.keep_unpushed_check)
        picker_box.addWidget(QLabel("Never delete:"))
        picker_box.addWidget(self.protect_edit)
        picker_box.addWidget(QLabel("Inference Providers:"))
        picker_box.addWidget(self.provider_combo)
        picker_box.addWidget(self.provider_label)

        # The same right-hand pane as the other run-and-read tabs: what previous
        # runs found, and how this one got there.
        self.side_panel = SidePanel(
            self._build_history_pane(),
            repo_name=lambda: Path(self._repo_path()).name,
            margins=(SECTION_GAP, 0, 0, 0),
        )
        self.side_panel.calls.noted.connect(self.status.setText)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(picker_pane)
        splitter.addWidget(content)
        splitter.addWidget(self.side_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 2)
        splitter.setSizes([220, 600, 320])

        outer = QVBoxLayout(self)
        outer.addWidget(splitter)

        self._select_stored_agent()
        self.refresh_repos()

    #: The calls half of the side pane, which is what a run talks to.
    calls_pane = property(lambda self: self.side_panel.calls)

    def _build_history_pane(self) -> QWidget:
        """Every previous run of this agent against this repository."""
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 0, 0, 0)

        self.runs_tree = QTreeWidget()
        self.runs_tree.setHeaderLabels(["When", "Result"])
        self.runs_tree.setRootIsDecorated(False)
        self.runs_tree.setColumnWidth(0, 110)
        self.runs_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.runs_tree.itemDoubleClicked.connect(lambda *_: self._on_open_run())
        self.runs_tree.itemSelectionChanged.connect(self._on_run_selection)
        self.runs_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.runs_tree.customContextMenuRequested.connect(self._on_runs_menu)
        box.addWidget(self.runs_tree, 1)

        row = QHBoxLayout()
        self.open_run_btn = QPushButton("Open")
        self.open_run_btn.clicked.connect(self._on_open_run)
        self.compare_btn = QPushButton("Compare")
        self.compare_btn.setToolTip("Select two runs to compare them.")
        self.compare_btn.clicked.connect(self._on_compare_selected)
        self.delete_run_btn = QPushButton("Delete")
        self.delete_run_btn.clicked.connect(self._on_delete_run)
        for button in (self.open_run_btn, self.compare_btn, self.delete_run_btn):
            button.setEnabled(False)
            row.addWidget(button)
        box.addLayout(row)

        self.history_note = QLabel("")
        self.history_note.setWordWrap(True)
        self.history_note.setStyleSheet(MUTED_COLOUR)
        self.history_note.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        box.addWidget(self.history_note)
        return pane

    # ---- state --------------------------------------------------------------
    def _repo_path(self) -> str:
        return self.repo_picker.current_path()

    def _agent_id(self) -> str:
        item = self.agent_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _select_stored_agent(self) -> None:
        wanted = self.settings.agent_last_id
        for row in range(self.agent_list.count()):
            if self.agent_list.item(row).data(Qt.ItemDataRole.UserRole) == wanted:
                self.agent_list.setCurrentRow(row)
                return
        self.agent_list.setCurrentRow(0)

    def refresh_repos(self) -> None:
        """Reload repositories, keeping any report already on screen.

        Switching tabs calls this; a report that took minutes to produce must
        survive it.
        """
        self.repo_picker.refresh()
        self.refresh_provider()
        self._refresh_header()
        # Covers the case the two signal handlers cannot: a repository removed
        # in another tab makes the picker fall back to a different one silently.
        self._sync_shown_report()
        self._refresh_history()
        if self.repo_picker.count() == 0:
            self.status.setText(NO_REPOS_MESSAGE)
        elif self.status.text() == NO_REPOS_MESSAGE:
            self.status.setText("")
        self.run_btn.setEnabled(bool(self._repo_path()) and self._worker is None)

    def refresh_provider(self) -> None:
        """Show the stored provider, without treating that as a user choice.

        Called on every refresh because the Connection & Model tab and the
        Generate tab can both change it while this one is not looking.
        """
        index = self.provider_combo.findData(self.settings.provider)
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.provider_combo.blockSignals(False)
        # The combo names the provider; the model is the other half of what a
        # run will actually use, and it is chosen in Connection & Model.
        model = self.settings.active_model() or "no model selected"
        self.provider_label.setText(f"Model: {model}")

    def _on_provider_changed(self, _index: int) -> None:
        key = self.provider_combo.currentData()
        if not key or key == self.settings.provider:
            return
        self.settings.provider = key
        self.settings.save()
        self.refresh_provider()  # the model line belongs to the new provider

    def _refresh_header(self, stored=None) -> None:
        item = self.agent_list.currentItem()
        name = Path(self._repo_path()).name if self._repo_path() else "no repository"
        suffix = " (stored run)" if stored is not None else ""
        self.header.setText(f"{item.text() if item else 'Agent'} — {name}{suffix}")

    def _on_repo_changed(self, _path: str = "") -> None:
        self._refresh_header()
        self._sync_shown_report()
        self._refresh_history()
        self.run_btn.setEnabled(bool(self._repo_path()) and self._worker is None)

    def _on_agent_changed(self, _row: int = -1) -> None:
        agent_id = self._agent_id()
        if not agent_id:
            return
        info = next(i for i in agents.infos() if i.id == agent_id)
        self.agent_description.setText(f"{info.description}\n\n{info.cost_hint}")
        # Fast mode is a property of the history scan; the config audit has none.
        self.fast_check.setVisible(agent_id == "size-audit")
        self.settings.agent_last_id = agent_id
        self.settings.save()
        self._refresh_header()
        self._sync_shown_report()
        self._refresh_history()

    # ---- what is on screen ---------------------------------------------------
    def _show_report(self, report, stored=None) -> None:
        """Render a report and remember what it describes.

        One path for a fresh run and for a stored one, so Copy, Export and the
        measurements behave the same either way -- with the difference said out
        loud rather than left to be inferred.
        """
        self._report = report
        self._shown_key = (norm_path(report.repo_path), report.agent_id)
        self.view.setHtml(report_mod.to_html(report))
        self._fill_facts(report)
        for button in (self.copy_btn, self.export_btn):
            button.setEnabled(True)
        self.tabs.setCurrentIndex(0)
        self._refresh_header(stored=stored)

    def _clear_report(self) -> None:
        """Forget the report on screen: it no longer describes what is selected."""
        self._report = None
        self._shown_key = None
        self.view.clear()
        self.facts_tree.clear()
        self.diff_view.clear()
        self.tabs.setTabEnabled(self.diff_tab, False)
        self.tabs.setTabText(self.diff_tab, COMPARISON_TAB)
        self.tabs.setCurrentIndex(0)
        for button in (self.copy_btn, self.export_btn):
            button.setEnabled(False)
        if self.status.text() != NO_REPOS_MESSAGE:
            self.status.setText("")

    def _sync_shown_report(self) -> None:
        """Drop a report that is no longer about the selected repo and agent.

        A comparison, not an unconditional clear: refreshing the repository list
        must not throw away a report that took minutes to produce.
        """
        if self._shown_key is None:
            return
        if self._shown_key == (norm_path(self._repo_path()), self._agent_id()):
            return
        self._clear_report()

    def _on_options_changed(self, _checked: bool = False) -> None:
        self.settings.agents_narrate = self.narrate_check.isChecked()
        self.settings.agent_fast_mode = self.fast_check.isChecked()
        self.settings.save()

    def _show_rules(self, rules) -> None:
        for widget, value in (
            (self.stale_months_spin, rules.months),
            (self.merged_only_check, rules.merged_only),
            (self.keep_unpushed_check, rules.keep_unpushed),
        ):
            widget.blockSignals(True)
            widget.setValue(value) if hasattr(widget, "setValue") else widget.setChecked(value)
            widget.blockSignals(False)
        self.protect_edit.setText(", ".join(rules.protect))

    def _on_rules_changed(self, *_args) -> None:
        """Keep the stale-branch rules. Read back, never held as widgets.

        The audit asks `settings.stale_rules()` for an object, so what is stored
        has to survive a round trip through the settings file -- storing the
        widgets' values directly would work until someone hand-edited it.
        """
        from git_assistant.agents.branches import StaleRules

        self.settings.set_stale_rules(
            StaleRules(
                months=self.stale_months_spin.value(),
                protect=[
                    part.strip()
                    for part in self.protect_edit.text().split(",")
                    if part.strip()
                ],
                merged_only=self.merged_only_check.isChecked(),
                keep_unpushed=self.keep_unpushed_check.isChecked(),
            )
        )
        self.settings.save()

    # ---- running ------------------------------------------------------------
    def _on_run(self) -> None:
        repo = self._repo_path()
        agent_id = self._agent_id()
        if not repo or not agent_id:
            return
        if self._before_run is not None:
            self._before_run()  # pick up settings edited in sibling tabs
        # Only the prose costs anything, so this is asked only when it is asked
        # for -- an audit written from the measurements sends nothing.
        if not confirm(
            self,
            estimate.for_audit(
                self.settings, agent_id, narrate=self.narrate_check.isChecked()
            ),
        ):
            return
        self._set_running(True)
        self.status.setText("Starting...")
        self.side_panel.calls.reset()  # these belong to the run about to start

        worker = AgentWorker(
            self.settings,
            agent_id,
            repo,
            fast=self.fast_check.isChecked() and agent_id == "size-audit",
            narrate=self.narrate_check.isChecked(),
        )
        worker.progress.connect(self.status.setText)
        worker.progressPct.connect(self._on_pct)
        worker.call.connect(self.side_panel.calls.add_call)
        worker.finished.connect(self._on_finished)
        worker.error.connect(self._on_error)
        self._worker = worker
        self._thread = run_worker(worker)

    def _on_cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status.setText("Cancelling...")
            self.cancel_btn.setEnabled(False)

    def cancel_running(self) -> None:
        """Stop a run in flight (the window is closing).

        Without this the git children of a cancelled scan keep reading the
        object store after the window they belong to is gone.
        """
        if self._worker is not None:
            self._worker.cancel()

    def _on_pct(self, pct: int) -> None:
        self._busy_step(pct)

    def _set_running(self, running: bool) -> None:
        self.run_btn.setEnabled(not running and bool(self._repo_path()))
        self.cancel_btn.setEnabled(running)
        self.agent_list.setEnabled(not running)
        self.repo_picker.setEnabled(not running)
        if running:
            self._busy_start("Repository audit")
        else:
            self._busy_stop()

    # ---- the window's shared progress bar --------------------------------
    def _busy_start(self, what: str) -> None:
        """Say a task has begun, if this panel is in a window that has a bar.

        `busy` is set by the window that owns this panel; a panel constructed on
        its own -- in a test, or by the tray -- reports to nothing and works.
        """
        if self.busy is not None:
            self.busy.start(self, what)

    def _busy_step(self, percent: int) -> None:
        if self.busy is not None:
            self.busy.step(self, percent)

    def _busy_stop(self) -> None:
        if self.busy is not None:
            self.busy.stop(self)

    def _on_finished(self, report) -> None:
        self._worker = None
        self._set_running(False)
        self._show_report(report)
        parts = [f"Done — {len(list(report.walk()))} section(s)."]
        parts += report.warnings

        # "Did it improve?" is the question the audit exists to answer, so it is
        # answered without being asked -- but the Report tab stays in front,
        # because Run was pressed to see the report.
        stored, problem = history.record(
            report,
            narrated=self.narrate_check.isChecked(),
            fast=self.fast_check.isChecked(),
            limit=self.settings.agent_history_limit,
        )
        if problem:
            parts.append(f"(Not saved to history: {problem})")
        self._refresh_history(select=stored)
        parts.append(self._auto_compare(stored))
        self.status.setText(" ".join(p for p in parts if p))

    def _auto_compare(self, stored) -> str:
        """Compare a finished run with the previous one, if there is one."""
        self._set_diff(None)
        if stored is None:
            return ""
        earlier = [
            run
            for run in history.list_runs(stored.repo_path, stored.agent_id)
            if run.run_id != stored.run_id
        ]
        if not earlier:
            return "First run recorded — the next one will be compared against it."
        previous = history.load_run(earlier[0])
        if previous is None:
            return "Recorded. The previous run could not be read to compare against."
        difference = compare.diff(previous, stored)
        if difference is None:
            return "Recorded."
        self._set_diff(difference)
        return difference.summary()

    def _set_diff(self, difference) -> None:
        """Fill (or empty) the Comparison tab, saying in its title what it found."""
        self._diff = difference
        if difference is None:
            self.diff_view.clear()
            self.tabs.setTabEnabled(self.diff_tab, False)
            self.tabs.setTabText(self.diff_tab, COMPARISON_TAB)
            return
        self.diff_view.setHtml(compare.to_html(difference))
        self.tabs.setTabEnabled(self.diff_tab, True)
        self.tabs.setTabText(
            self.diff_tab, f"{COMPARISON_TAB} {difference.verdict().arrow()}"
        )

    def _on_error(self, message: str) -> None:
        self._worker = None
        self._set_running(False)
        self.status.setText(message)
        if message != "Cancelled.":
            QMessageBox.warning(self, "Agent failed", message)

    def _fill_facts(self, report) -> None:
        """Every measurement, so the prose can be checked against it."""
        self.facts_tree.clear()
        for section in report.walk():
            parent = QTreeWidgetItem([f"{section.number} {section.title}", ""])
            self.facts_tree.addTopLevelItem(parent)
            for item in section.facts:
                parent.addChild(QTreeWidgetItem([item.label, item.value]))
            for table in section.tables:
                node = QTreeWidgetItem([table.title or "Table", ""])
                parent.addChild(node)
                for row in table.rows:
                    node.addChild(
                        QTreeWidgetItem([str(row[0]), "  ".join(str(c) for c in row[1:])])
                    )
            parent.setExpanded(True)

    # ---- previous runs -------------------------------------------------------
    def _refresh_history(self, select=None) -> None:
        """List this repository's runs of this agent, newest first."""
        repo, agent_id = self._repo_path(), self._agent_id()
        self.runs_tree.clear()
        runs = history.list_runs(repo, agent_id) if repo and agent_id else []
        for index, run in enumerate(runs):
            previous = runs[index + 1] if index + 1 < len(runs) else None
            item = QTreeWidgetItem([run.when_label(), _headline_text(run, previous)])
            item.setData(0, Qt.ItemDataRole.UserRole, run)
            item.setToolTip(0, _run_tooltip(run))
            item.setToolTip(1, _run_tooltip(run))
            if run.pinned:
                item.setText(0, f"📌 {run.when_label()}")
            if select is not None and run.run_id == select.run_id:
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
        self.compare_btn.setEnabled(len(chosen) == 2)

    def _on_open_run(self) -> None:
        chosen = self._selected_runs()
        if len(chosen) != 1:
            return
        run = history.load_run(chosen[0])
        if run is None:
            QMessageBox.warning(
                self, "Run not available", "That run's file could not be read."
            )
            self._refresh_history()
            return
        self._show_report(run.report, stored=run)
        self.status.setText(
            f"Showing a stored run from {run.when_label()} — {run.commit_label()}. "
            "Press Run for a fresh one."
        )

    def _on_compare_selected(self) -> None:
        chosen = self._selected_runs()
        if len(chosen) != 2:
            return
        older, newer = sorted(chosen, key=lambda r: r.started_at)
        self._compare(older, newer)

    def _compare(self, older, newer) -> None:
        loaded = [history.load_run(run) for run in (older, newer)]
        if any(run is None for run in loaded):
            QMessageBox.warning(
                self, "Cannot compare", "One of those runs could not be read."
            )
            return
        difference = compare.diff(loaded[0], loaded[1])
        if difference is None:
            QMessageBox.information(
                self, "Cannot compare", "Those runs are from different agents."
            )
            return
        self._set_diff(difference)
        self.tabs.setCurrentIndex(self.diff_tab)
        self.status.setText(difference.summary())

    def _on_compare_with_current(self) -> None:
        chosen = self._selected_runs()
        if len(chosen) != 1 or self._report is None:
            return
        older = history.load_run(chosen[0])
        if older is None:
            return
        current = history.StoredRun(
            run_id="current",
            agent_id=self._report.agent_id,
            repo_path=self._report.repo_path,
            started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            head=self._report.head,
            branch=self._report.branch,
            dirty=self._report.dirty,
            report=self._report,
        )
        difference = compare.diff(older, current)
        if difference is not None:
            self._set_diff(difference)
            self.tabs.setCurrentIndex(self.diff_tab)
            self.status.setText(difference.summary())

    def _on_delete_run(self) -> None:
        chosen = self._selected_runs()
        if not chosen:
            return
        if QMessageBox.question(
            self,
            "Delete runs",
            f"Delete {len(chosen)} recorded run(s)? The repository is not touched.",
        ) != QMessageBox.StandardButton.Yes:
            return
        for run in chosen:
            history.delete_run(run)
        self._refresh_history()

    def _on_pin_run(self) -> None:
        """Pin a baseline so the retention cap never removes it."""
        for run in self._selected_runs():
            history.set_pinned(run, not run.pinned)
        self._refresh_history()

    def _on_clear_history(self) -> None:
        repo = self._repo_path()
        if not repo:
            return
        if QMessageBox.question(
            self,
            "Clear history",
            f"Forget every recorded run for {Path(repo).name}? "
            "The repository itself is not touched.",
        ) != QMessageBox.StandardButton.Yes:
            return
        history.clear_repo(repo)
        self._refresh_history()

    def _on_runs_menu(self, point) -> None:
        chosen = self._selected_runs()
        menu = QMenu(self)
        open_action = menu.addAction("Open")
        open_action.setEnabled(len(chosen) == 1)
        against_current = menu.addAction("Compare with the report on screen")
        against_current.setEnabled(len(chosen) == 1 and self._report is not None)
        compare_two = menu.addAction("Compare the two selected")
        compare_two.setEnabled(len(chosen) == 2)
        menu.addSeparator()
        pinned = bool(chosen) and all(run.pinned for run in chosen)
        pin = menu.addAction("Unpin" if pinned else "Pin as baseline")
        pin.setEnabled(bool(chosen))
        delete = menu.addAction("Delete")
        delete.setEnabled(bool(chosen))
        menu.addSeparator()
        clear = menu.addAction("Clear this repository's history")

        action = menu.exec(self.runs_tree.viewport().mapToGlobal(point))
        if action == open_action:
            self._on_open_run()
        elif action == against_current:
            self._on_compare_with_current()
        elif action == compare_two:
            self._on_compare_selected()
        elif action == pin:
            self._on_pin_run()
        elif action == delete:
            self._on_delete_run()
        elif action == clear:
            self._on_clear_history()

    # ---- output -------------------------------------------------------------
    def _showing_comparison(self) -> bool:
        """Copy and Export act on whichever of the two is being read."""
        return self.tabs.currentIndex() == self.diff_tab and self._diff is not None

    def _on_copy(self) -> None:
        if self._showing_comparison():
            QGuiApplication.clipboard().setText(compare.to_markdown(self._diff))
            self.status.setText("Comparison copied to the clipboard as Markdown.")
            return
        if self._report is None:
            return
        QGuiApplication.clipboard().setText(report_mod.to_markdown(self._report))
        self.status.setText("Report copied to the clipboard as Markdown.")

    def _on_export(self) -> None:
        comparison = self._showing_comparison()
        if self._report is None and not comparison:
            return
        kind = "comparison" if comparison else self._report.agent_id
        name = Path((self._diff if comparison else self._report).repo_path).name
        suggested = f"{name}-{kind}-{date.today().isoformat()}.md"
        path, chosen = QFileDialog.getSaveFileName(
            self,
            "Export comparison" if comparison else "Export report",
            str(Path.home() / suggested),
            "Markdown (*.md);;Web page (*.html);;Plain text (*.txt)",
        )
        if not path:
            return
        suffix = Path(path).suffix.lower()
        as_html = suffix == ".html" or "Web page" in chosen
        if comparison:
            text = compare.to_html(self._diff) if as_html else compare.to_markdown(self._diff)
        elif as_html:
            text = report_mod.to_html(self._report)
        elif suffix == ".txt" or "Plain text" in chosen:
            text = report_mod.to_text(self._report)
        else:
            text = report_mod.to_markdown(self._report)
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.status.setText(f"Exported to {path}")
