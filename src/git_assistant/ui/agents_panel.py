"""Audit tab: run the ticked repository audits and read what they found.

Same shape as the Tags tab -- repository on the left, the work on the right --
because both answer a question about one repository and the tabs should not
each invent their own layout.

Two views of the same run: the report, and the measurements it was built from.
The second exists because a reader who does not trust a paragraph should not
have to trust it: every figure in the prose is in that tree.

Ticking and selecting are two different things here, and deliberately so. The
ticks say what a run does; the selection says which of its reports is on screen,
because three audits leave three reports and this pane can show one. A run of
several therefore ends with all of them in Previous Runs and one of them in
front.

Four panes, left to right, because they answer four questions and mixing them
was the old layout's mistake: where a run is aimed (repository, provider), what
it will do (the audits, each with its own settings -- see
``git_assistant.ui.audit_cards``), what it found, and what it found last time.
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
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import agents, repo_config
from git_assistant.agents import compare, history
from git_assistant.agents import report as report_mod
from git_assistant import estimate
from git_assistant.config import Settings, norm_path
from git_assistant.providers import PROVIDERS
from git_assistant.ui.audit_cards import AuditCard
from git_assistant.ui.estimate_dialog import confirm
from git_assistant.ui.settings_diff_dialog import SettingsDiffDialog
from git_assistant.ui.preview_dialog import SECTION_GAP
from git_assistant.ui.repo_picker import RepoPicker
from git_assistant.ui.side_panel import SidePanel
from git_assistant.ui.workers import AgentWorker, run_worker

NO_REPOS_MESSAGE = "No repositories configured - add one in Repositories."
NOTHING_TICKED_MESSAGE = "Tick at least one audit to run."
INFO_COLOUR = "color: #8ab;"
MUTED_COLOUR = "color: #888;"
COMPARISON_TAB = "Comparison"
#: The one audit fast mode means anything to: it is a property of the history
#: scan, and no other audit has one.
SIZE_AUDIT = "size-audit"


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


def _history_note(repo: str, runs: list, limit: int) -> str:
    if not repo:
        return ""
    if not runs:
        return "No previous runs for this repository yet."
    note = f"Keeping the newest {limit} run(s) per agent."
    if limit <= 0:
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
        #: The options the run in flight was started with. A report is recorded
        #: with the flags that produced it, not with whatever is ticked by the
        #: time it comes back.
        self._fast_used = False
        self._narrated = True  # replaced from the settings in force
        #: The audit being read. Not the ticked ones -- see the module docstring.
        self._selected = ""
        #: True while the controls are being filled from the settings. Every
        #: write goes through `write_audit`, and putting a stored value on
        #: screen must not be mistaken for somebody choosing it -- opening a
        #: tab is not a change, and a change forks a settings file.
        self._loading = False

        self.repo_picker = RepoPicker(settings)
        self.repo_picker.repoChanged.connect(self._on_repo_changed)

        # One card per audit, each carrying its own settings -- see
        # git_assistant.ui.audit_cards for why they are not in one column.
        self.cards = []
        for info in agents.infos():
            card = AuditCard(info, self)
            card.ticked.connect(self._on_ticks_changed)
            card.picked.connect(lambda c=card: self._select_agent(c.agent_id))
            if card.options is not None:
                card.options.changed.connect(self._on_audit_written)
            self.cards.append(card)

        # Applies to every audit, so it belongs to none of them: it is the
        # difference between a run that contacts a provider and one that does
        # not, whichever audits are ticked.
        self.narrate_check = QCheckBox("Write the narrative")
        self.narrate_check.setToolTip(
            "The measurements are taken by git either way. With this on, the "
            "configured provider writes the prose around them - and any figure "
            "it invents is rejected."
        )
        self.narrate_check.toggled.connect(self._on_options_changed)

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
            "Tick the audits to run and press Run. Nothing in the repository "
            "is changed."
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

        # Where a run is aimed: which repository, and through which provider.
        # Both are about the run rather than about any one audit, and both are
        # shared with other tabs.
        picker_pane = QWidget()
        picker_box = QVBoxLayout(picker_pane)
        picker_box.setContentsMargins(0, 0, SECTION_GAP, 0)
        picker_box.addWidget(self.repo_picker, 1)
        picker_box.addSpacing(SECTION_GAP)
        picker_box.addWidget(QLabel("Inference Providers:"))
        picker_box.addWidget(self.provider_combo)
        picker_box.addWidget(self.provider_label)

        self.audits_pane = self._build_audits_pane()

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
        splitter.addWidget(self.audits_pane)
        splitter.addWidget(content)
        splitter.addWidget(self.side_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 3)
        splitter.setStretchFactor(3, 2)
        splitter.setSizes([200, 300, 540, 300])

        outer = QVBoxLayout(self)
        outer.addWidget(splitter)

        self._reload_from_settings()
        self.refresh_repos()

    #: The calls half of the side pane, which is what a run talks to.
    calls_pane = property(lambda self: self.side_panel.calls)

    #: The size audit's own option, reached often enough to name here.
    fast_check = property(lambda self: self._options("size-audit").fast_check)

    # ---- the settings in force for this repository ---------------------------
    def audit_rules(self):
        """What the audits are configured with here. Read, never held."""
        return repo_config.for_repo(self.settings, self._repo_path()).audit

    def write_audit(self, mutate) -> None:
        """One change to this repository's audit settings; see repo_config.change.

        Forks to Custom exactly as the settings editor does, so a tick box here
        cannot edit the file a team shares. The prompt is only reached when the
        fork would replace Custom settings that already exist -- once per
        repository, not once per tick.
        """
        if self._loading:
            return
        problem = repo_config.change(
            self.settings,
            self._repo_path(),
            mutate,
            may_replace_custom=self._may_replace_custom,
        )
        if problem:
            self.status.setText(problem)

    def _may_replace_custom(self, before: dict, after: dict) -> bool:
        return SettingsDiffDialog(
            before,
            after,
            title="Replace your Custom settings",
            question=(
                "You already have Custom settings for this repository. Changing "
                "this here replaces them:"
            ),
            before_label="Custom now",
            after_label="After this change",
            parent=self,
        ).wanted()

    def _on_audit_written(self) -> None:
        """A card wrote something; the rest of the tab may be showing the old it."""
        self._refresh_history()

    def _reload_from_settings(self) -> None:
        """Put the settings in force for this repository onto every control.

        Called when the repository changes, because every one of these is that
        repository's answer and not the application's.
        """
        self._loading = True
        try:
            rules = self.audit_rules()
            self.narrate_check.blockSignals(True)
            self.narrate_check.setChecked(rules.narrate)
            self.narrate_check.blockSignals(False)
            self._reload_audit_options()
            self._select_stored_agent()
            self._restore_ticks()
        finally:
            self._loading = False

    def _reload_audit_options(self) -> None:
        """Put the settings in force onto every card. Never a change."""
        rules = self.audit_rules()
        for card in self.cards:
            if card.options is not None:
                card.options.show_rules(rules)

    def _options(self, agent_id: str):
        """One audit's settings widget, or ``None`` if it has none."""
        return next(
            (c.options for c in self.cards if c.agent_id == agent_id), None
        )

    def _build_audits_pane(self) -> QWidget:
        """Every audit, each with what it runs with.

        Scrolled rather than squeezed: an audit whose options are off the bottom
        of the pane is an audit nobody can configure, and the pane is narrow by
        design -- the report beside it is what the tab is for.
        """
        inner = QWidget()
        box = QVBoxLayout(inner)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(SECTION_GAP)
        for card in self.cards:
            box.addWidget(card)
        box.addStretch(1)

        area = self.audits_scroll = QScrollArea()
        area.setWidget(inner)
        area.setWidgetResizable(True)
        area.setFrameShape(QScrollArea.Shape.NoFrame)
        # Scrolled downwards only. A checkbox cannot wrap, so a pane narrower
        # than its longest label does not shorten the label -- it hides the end
        # of it, and "Only propose merged bran" is a different promise from the
        # one being made. Wide enough for the longest, and the splitter handle
        # is there for anyone who wants it wider.
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setMinimumWidth(
            inner.minimumSizeHint().width()
            + area.verticalScrollBar().sizeHint().width()
        )

        pane = QWidget()
        outer = QVBoxLayout(pane)
        outer.setContentsMargins(SECTION_GAP, 0, SECTION_GAP, 0)
        header = QLabel("Audits:")
        header.setToolTip(
            "Tick the audits to run. Click one to read its report and its "
            "previous runs."
        )
        outer.addWidget(header)
        outer.addWidget(area, 1)
        outer.addWidget(self.narrate_check)
        return pane

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
        """The audit whose report and history are on screen."""
        return self._selected

    def _checked_ids(self) -> list[str]:
        """The audits a run would carry out, in the order they are listed."""
        return [card.agent_id for card in self.cards if card.is_ticked()]

    def _known(self, agent_id: str) -> bool:
        return any(card.agent_id == agent_id for card in self.cards)

    def _select_stored_agent(self) -> None:
        stored = self.audit_rules().last
        first = self.cards[0].agent_id if self.cards else ""
        self._select_agent(stored if self._known(stored) else first)

    def _restore_ticks(self) -> None:
        """Tick what was ticked last time, or the audit being read.

        Never nothing: a tab that opens with no audit ticked opens with its Run
        button dead, and the reason is four pixels wide.
        """
        wanted = [
            agent_id
            for agent_id in self.audit_rules().selected
            if self._known(agent_id)
        ]
        self._set_ticks(wanted or [self._agent_id()])

    def _set_ticks(self, agent_ids) -> None:
        wanted = set(agent_ids)
        for card in self.cards:  # silently, so this is one save and not three
            card.set_ticked(card.agent_id in wanted)
        self._on_ticks_changed()

    def _on_ticks_changed(self) -> None:
        chosen = self._checked_ids()
        if chosen != self.audit_rules().selected:
            self.write_audit(
                lambda data: data.setdefault("audit", {}).update({"selected": chosen})
            )
        self.run_btn.setText(f"Run {len(chosen)} audits" if len(chosen) > 1 else "Run")
        self.run_btn.setToolTip(
            "Runs: " + ", ".join(self._label_of(a) for a in chosen)
            if chosen
            else NOTHING_TICKED_MESSAGE
        )
        # A dead Run button with nothing said about it is a bug report.
        if not chosen and self.status.text() != NO_REPOS_MESSAGE:
            self.status.setText(NOTHING_TICKED_MESSAGE)
        elif chosen and self.status.text() == NOTHING_TICKED_MESSAGE:
            self.status.setText("")
        self._update_run_enabled()

    @staticmethod
    def _label_of(agent_id: str) -> str:
        return next((i.label for i in agents.infos() if i.id == agent_id), agent_id)

    def _can_run(self) -> bool:
        return bool(self._repo_path()) and bool(self._checked_ids())

    def _update_run_enabled(self) -> None:
        self.run_btn.setEnabled(self._can_run() and self._worker is None)

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
        self._update_run_enabled()

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
        title = self._label_of(self._agent_id()) if self._agent_id() else "Audit"
        name = Path(self._repo_path()).name if self._repo_path() else "no repository"
        suffix = " (stored run)" if stored is not None else ""
        self.header.setText(f"{title} — {name}{suffix}")

    def _on_repo_changed(self, _path: str = "") -> None:
        self._reload_from_settings()
        self._refresh_header()
        self._sync_shown_report()
        self._refresh_history()
        self._update_run_enabled()

    def _select_agent(self, agent_id: str) -> None:
        """Read this audit's report, history and description from now on.

        Says nothing about what a run does: an audit can be read without being
        ticked, and ticked without being read.
        """
        if not self._known(agent_id) or agent_id == self._selected:
            return
        self._selected = agent_id
        for card in self.cards:
            card.set_selected(card.agent_id == agent_id)
            # For the selection a finished run makes rather than a click: the
            # highlight is the only thing saying which report is on screen, and
            # it is no use below the fold.
            if card.agent_id == agent_id:
                self.audits_scroll.ensureWidgetVisible(card)
        if self.audit_rules().last != agent_id:
            self.write_audit(
                lambda data: data.setdefault("audit", {}).update({"last": agent_id})
            )
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
        """The one option that is about the run rather than about one audit."""
        on = self.narrate_check.isChecked()
        if on != self.audit_rules().narrate:
            self.write_audit(
                lambda data: data.setdefault("audit", {}).update({"narrate": on})
            )

    # ---- running ------------------------------------------------------------
    def _on_run(self) -> None:
        repo = self._repo_path()
        chosen = self._checked_ids()
        if not repo or not chosen:
            return
        if self._before_run is not None:
            self._before_run()  # pick up settings edited in sibling tabs
        # Only the prose costs anything, so this is asked only when it is asked
        # for -- an audit written from the measurements sends nothing. Priced
        # for the whole run: several audits divide the window between them.
        if not confirm(
            self,
            estimate.for_audits(
                self.settings, chosen, narrate=self.narrate_check.isChecked()
            ),
        ):
            return
        # Held rather than read back later: the options are enabled again the
        # moment the run finishes, and a report is recorded with the flags it
        # was produced under, not the ones on screen afterwards.
        self._fast_used = self.fast_check.isChecked() and SIZE_AUDIT in chosen
        self._narrated = self.narrate_check.isChecked()
        self._set_running(True)
        self.status.setText("Starting...")
        self.side_panel.calls.reset()  # these belong to the run about to start

        worker = AgentWorker(
            self.settings,
            chosen,
            repo,
            fast=self._fast_used,
            narrate=self._narrated,
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
        self.run_btn.setEnabled(not running and self._can_run())
        self.cancel_btn.setEnabled(running)
        self.repo_picker.setEnabled(not running)
        # The whole pane, ticks and settings alike: they decided what this run
        # does, and changing them under it would describe a run that is not the
        # one in flight.
        self.audits_pane.setEnabled(not running)
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

    def _on_finished(self, runs) -> None:
        """Record every audit that finished, and put one of them on screen."""
        self._worker = None
        self._set_running(False)

        many = len(runs) > 1
        notes: list[str] = []
        stored_runs: dict[str, object] = {}
        for outcome in runs:
            prefix = f"{outcome.label}: " if many else ""
            if not outcome.ok:
                notes.append(f"{prefix}not run — {outcome.problem}")
                continue
            report = outcome.report
            stored, problem = history.record(
                report,
                narrated=self._narrated,
                fast=self._fast_used and report.agent_id == SIZE_AUDIT,
                limit=self.audit_rules().history_limit,
            )
            stored_runs[outcome.agent_id] = stored
            if problem:
                notes.append(f"{prefix}not saved to history: {problem}")
            notes += [f"{prefix}{warning}" for warning in report.warnings]

        done = [outcome for outcome in runs if outcome.ok]
        if not done:
            self.status.setText(" ".join(notes) or "Nothing was audited.")
            return

        # The one being read stays the one being read, if it ran. Otherwise the
        # first that did: a finished run with nothing on screen looks like a run
        # that produced nothing.
        shown = next((o for o in done if o.agent_id == self._agent_id()), done[0])
        self._select_agent(shown.agent_id)
        self._show_report(shown.report)

        parts = [
            f"Done — {len(done)} audit(s), all recorded."
            if many
            else f"Done — {len(list(shown.report.walk()))} section(s)."
        ]
        parts += notes
        # "Did it improve?" is the question the audit exists to answer, so it is
        # answered without being asked -- but the Report tab stays in front,
        # because Run was pressed to see the report.
        self._refresh_history(select=stored_runs.get(shown.agent_id))
        parts.append(self._auto_compare(stored_runs.get(shown.agent_id)))
        if many:
            parts.append("Select an audit on the left to read its report.")
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
        self.history_note.setText(_history_note(repo, runs, self.audit_rules().history_limit))

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
