"""Code Review tab: check the marked files against a table of rules.

Same shape as Generate Commit Message -- repository on the far left, the work
in the middle, every call to the model on the right -- because both tabs run the
configured provider over the current diff, and a reader should not have to learn
two layouts to follow one run.

What is deliberately visible here: which files were reviewed, which rules were
sent, and what came back for each file. A review that quietly skipped a file, or
ran with two thirds of the rules, reads exactly like a clean one otherwise.
"""

from __future__ import annotations

import html
from datetime import date
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import estimate
from git_assistant.config import Settings, norm_path
from git_assistant.providers import PROVIDERS
from git_assistant.review import history, languages
from git_assistant.review import report as report_mod
from git_assistant.review import xlsx
from git_assistant.review import profiles as profiles_mod
from git_assistant.review import shared as shared_profile
from git_assistant.review import versions as version_detection
from git_assistant.review.plan import build as build_plan
from git_assistant.review.reviewer import Candidate, staged_files
from git_assistant.review.rules import RuleStore, RuleTable
from git_assistant.ui.review_plan_dialog import confirm as confirm_plan
from git_assistant.ui.preview_dialog import SECTION_GAP
from git_assistant.ui.profile_tab import ProfileTab
from git_assistant.ui.repo_picker import RepoPicker
from git_assistant.ui.side_panel import SidePanel
from git_assistant.ui.workers import ReviewWorker, run_worker

NO_REPOS_MESSAGE = "No repositories configured - add one in Repositories."
NO_RULES_MESSAGE = "No rule table yet - import a spreadsheet under Rules."
INFO_COLOUR = "color: #8ab;"
MUTED_COLOUR = "color: #888;"

#: Said in the calls pane when a stored review is opened; its calls are not kept.
STORED_CALLS_NOTE = (
    "This is a stored review. Its findings are kept; the calls that produced "
    "them are not recorded -- forty prompts a run is more text than anyone "
    "reads twice."
)

_PROBLEM = QColor("#ff8080")
_PARTIAL = QColor("#d0a030")
_MUTED = QColor("#888888")


class ReviewPanel(QWidget):
    """Pick a repository and a rule table, mark files, review them."""

    def __init__(self, settings: Settings, before_run=None, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._before_run = before_run
        self._thread = None
        #: The window's shared progress bar, set by whoever owns this panel.
        #: None when there is none -- a panel in a test, or the tray's own
        #: window -- and every report to it is then a no-op.
        self.busy = None
        self._worker: ReviewWorker | None = None
        self._store = RuleStore.load()
        self._candidates: list[Candidate] = []
        #: Every profile that can be chosen, held rather than rebuilt per call:
        #: the Profiles tab edits the object it was handed, and handing out a
        #: fresh one next time would quietly drop the edit.
        self._all_profiles: list = []
        #: The profile the Profiles tab has open. Not the one a review runs
        #: against -- that is the dropdown's, and only the dropdown's.
        self._viewed: str = ""
        #: What each language's version is here, read from what the repository
        #: declares. Free -- a handful of file reads -- so it is done on every
        #: repository change rather than behind a button.
        self._detected: dict[str, str] = {}
        #: The above, overruled by what the profile under review pins.
        self._versions: dict[str, str] = {}
        #: Where each detected version was read from, for the tooltip.
        self._version_sources: dict[str, str] = {}
        #: The tables a repository's own profile brought with it, if it has one.
        self._repo_tables: dict = {}
        self._run = None  # the ReviewRun on screen, stored or just finished
        #: Repository the findings on screen describe, so they stop being shown
        #: when they stop being about what is selected.
        self._shown_key: str = ""
        #: Files the user unmarked, per repository. Kept in memory only: staging
        #: is ephemeral, and a refresh must not silently re-mark them.
        self._unticked: dict[str, set[str]] = {}

        self.repo_picker = RepoPicker(settings)
        self.repo_picker.repoChanged.connect(self._on_repo_changed)

        # Which rules this repository is reviewed against, per language and
        # version. Per repository, not per application: two projects can hold
        # to different standards.
        self.profile_combo = QComboBox()
        self.profile_combo.setToolTip(
            "Which rules apply to which language, and at which version. The "
            "choice is remembered per repository."
        )
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        self.rules_note = QLabel("")
        self.rules_note.setWordWrap(True)
        self.rules_note.setStyleSheet(MUTED_COLOUR)
        self.rules_note.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )

        # Which backend does the reviewing. Application-wide, as everywhere else:
        # it is an account and a connection, not a property of a project.
        self.provider_combo = QComboBox()
        self.provider_combo.setToolTip(
            "Which inference provider reviews the files. Configure it under "
            "Inference Providers in the Connection & Model tab."
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

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(INFO_COLOUR)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_picker_pane())
        splitter.addWidget(self._build_files_pane())
        splitter.addWidget(self._build_results_pane())
        splitter.addWidget(self._build_side_pane())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setStretchFactor(2, 4)
        splitter.setStretchFactor(3, 3)
        splitter.setSizes([210, 280, 500, 340])

        layout = QVBoxLayout(self)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status)
        layout.addLayout(self._build_buttons())

        self.refresh_repos()

    # ---- panes ---------------------------------------------------------------
    def _build_picker_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 0, SECTION_GAP, 0)
        box.addWidget(self.repo_picker, 1)
        box.addSpacing(SECTION_GAP)
        box.addWidget(QLabel("Rules profile:"))
        box.addWidget(self.profile_combo)
        box.addWidget(self.rules_note)
        box.addSpacing(SECTION_GAP)
        box.addWidget(QLabel("Inference Providers:"))
        box.addWidget(self.provider_combo)
        box.addWidget(self.provider_label)
        return pane

    def _build_files_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(SECTION_GAP, 0, SECTION_GAP, 0)

        self.files_label = QLabel("Files to review")
        box.addWidget(self.files_label)

        self.files_list = QListWidget()
        self.files_list.itemChanged.connect(self._on_file_ticked)
        box.addWidget(self.files_list, 1)

        row = QHBoxLayout()
        self.mark_all_btn = QPushButton("Mark all")
        self.mark_all_btn.clicked.connect(lambda: self._mark_all(True))
        self.mark_none_btn = QPushButton("Mark none")
        self.mark_none_btn.clicked.connect(lambda: self._mark_all(False))
        row.addWidget(self.mark_all_btn)
        row.addWidget(self.mark_none_btn)
        row.addStretch(1)
        box.addLayout(row)
        return pane

    def _build_results_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(SECTION_GAP, 0, SECTION_GAP, 0)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_findings_tab(), "Findings")
        self.tabs.addTab(self._build_profile_tab(), "Profiles")
        self.tabs.addTab(self._build_rules_tab(), "Rules")
        box.addWidget(self.tabs, 1)
        return pane

    def _build_findings_tab(self) -> QWidget:
        tab = QWidget()
        box = QVBoxLayout(tab)
        self.coverage_note = QLabel("")
        self.coverage_note.setWordWrap(True)
        self.coverage_note.setStyleSheet(MUTED_COLOUR)
        box.addWidget(self.coverage_note)

        inner = QSplitter(Qt.Orientation.Vertical)
        self.findings_tree = QTreeWidget()
        self.findings_tree.setHeaderLabels(["Finding", "Where"])
        self.findings_tree.setColumnWidth(0, 320)
        self.findings_tree.currentItemChanged.connect(self._on_finding_selected)
        # Wrapped so each half can be inset from the handle between them; a
        # widget added to a splitter directly has no layout to hold a margin.
        inner.addWidget(_inset(self.findings_tree, (0, 0, 0, SECTION_GAP)))

        self.detail_view = QTextBrowser()
        self.detail_view.setPlaceholderText(
            "Select a finding to see the rule it breaks and the model's own words."
        )
        inner.addWidget(_inset(self.detail_view, (0, SECTION_GAP, 0, 0)))
        inner.setStretchFactor(0, 3)
        inner.setStretchFactor(1, 2)
        box.addWidget(inner, 1)
        return tab

    def _build_profile_tab(self) -> QWidget:
        """Which rules apply to which language, and at which version."""
        self.profile_tab = ProfileTab()
        self.profile_tab.changed.connect(self._on_profile_edited)
        self.profile_tab.selected.connect(self._on_profile_viewed)
        self.profile_tab.add_language_btn.clicked.connect(self._on_add_language)
        self.profile_tab.remove_language_btn.clicked.connect(
            self.profile_tab.remove_language
        )
        self.profile_tab.share_btn.clicked.connect(self._on_share_profile)
        self.profile_tab.copy_btn.clicked.connect(self._on_copy_repo_tables)
        return self.profile_tab

    def _on_profile_viewed(self, name: str) -> None:
        """Open another profile. What a review uses is the dropdown's decision."""
        if name == self._viewed:
            return
        self._viewed = name
        self._show_profile()

    def _on_profile_edited(self) -> None:
        """A version or a rule was changed: keep it, and re-read the versions.

        The profile is taken from the tab, not looked up again: the tab edits
        the object it was handed, and the shipped defaults are rebuilt on every
        lookup, so a second lookup would copy the unedited one.
        """
        profile = self.profile_tab.profile()
        if profile is not None and profile not in self.settings.review_profiles:
            # The shipped rules and a repository's own are read-only; editing
            # one makes a copy, so a change is never silently lost and never
            # rewrites what ships. The copy is *not* put under review: which
            # profile a review uses is chosen in the dropdown, and deciding it
            # here is how a review runs against rules nobody selected.
            copy = profiles_mod.Profile.from_dict(profile.to_dict())
            copy.name = _free_profile_name(f"{profile.name} (edited)", self.settings)
            self.settings.review_profiles.append(copy)
            self.settings.save()
            self._viewed = copy.name
            self._refresh_tables()
            self.status.setText(
                f"'{profile.name}' is read-only, so the change was kept in "
                f"'{copy.name}'. Choose that under Rules profile to review "
                "against it."
            )
            return
        self.settings.save()
        self._refresh_versions()

    def _on_add_language(self) -> None:
        missing = self.profile_tab.missing_languages()
        if not missing:
            QMessageBox.information(
                self, "Every language", "This profile already covers all of them."
            )
            return
        labels = [languages.label_of(language) for language in missing]
        chosen, ok = QInputDialog.getItem(
            self, "Add a language", "Language:", labels, 0, False
        )
        if ok and chosen in labels:
            self.profile_tab.add_language(missing[labels.index(chosen)])

    def _on_share_profile(self) -> None:
        repo = self._repo_path()
        profile = self.profile_tab.profile()  # the one open, not the one under review
        if not repo or profile is None:
            return
        path = shared_profile.profile_path(repo)
        if (
            QMessageBox.question(
                self,
                "Share with the repository",
                f"Write this profile to:\n{path}\n\n"
                "It becomes part of the repository, so it will show up in "
                "'git status' and reach everyone who clones it. The rules of "
                "your own tables are written into it in full.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        try:
            written = shared_profile.write(repo, profile, self._store)
        except OSError as exc:
            QMessageBox.critical(self, "Could not write", str(exc))
            return
        # The repository now ships a profile, so it belongs in the list; without
        # this it appears only after the next tab switch.
        self._refresh_tables()
        self.status.setText(f"Shared: {written}. Commit it to pass it on.")

    def _on_copy_repo_tables(self) -> None:
        """Add the tables this repository shipped to the user's own library."""
        if not self._repo_tables:
            return
        added = shared_profile.copy_into(self._store, self._repo_tables)
        self._refresh_tables()
        self.status.setText(f"Copied {', '.join(added)} into your rule tables.")

    def _build_rules_tab(self) -> QWidget:
        tab = QWidget()
        box = QVBoxLayout(tab)

        self.rules_table = QTableWidget(0, 2)
        self.rules_table.setHorizontalHeaderLabels(["ruleID", "ruleDetails"])
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.rules_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        box.addWidget(self.rules_table, 1)

        row = QHBoxLayout()
        self.import_xlsx_btn = QPushButton("Import spreadsheet...")
        self.import_xlsx_btn.setToolTip(
            "Read a .xlsx with a ruleID column and a ruleDetails column."
        )
        self.import_xlsx_btn.clicked.connect(self._on_import_xlsx)
        self.export_xlsx_btn = QPushButton("Export spreadsheet...")
        self.export_xlsx_btn.clicked.connect(self._on_export_xlsx)
        self.import_json_btn = QPushButton("Import JSON...")
        self.import_json_btn.setToolTip("Merge tables exported from another machine.")
        self.import_json_btn.clicked.connect(self._on_import_json)
        self.export_json_btn = QPushButton("Export JSON...")
        self.export_json_btn.clicked.connect(self._on_export_json)
        for button in (
            self.import_xlsx_btn,
            self.export_xlsx_btn,
            self.import_json_btn,
            self.export_json_btn,
        ):
            row.addWidget(button)
        row.addStretch(1)
        self.rename_table_btn = QPushButton("Rename...")
        self.rename_table_btn.clicked.connect(self._on_rename_table)
        self.delete_table_btn = QPushButton("Delete")
        self.delete_table_btn.clicked.connect(self._on_delete_table)
        row.addWidget(self.rename_table_btn)
        row.addWidget(self.delete_table_btn)
        box.addLayout(row)
        return tab

    def _build_history_pane(self) -> QWidget:
        """Every review recorded for this repository."""
        tab = QWidget()
        box = QVBoxLayout(tab)
        self.runs_tree = QTreeWidget()
        self.runs_tree.setHeaderLabels(["When", "Result", "Rules"])
        self.runs_tree.setRootIsDecorated(False)
        self.runs_tree.setColumnWidth(0, 120)
        # Several at once, to delete them. Opening stays a question about one
        # review, and the button greys out when the answer is ambiguous.
        self.runs_tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.runs_tree.itemDoubleClicked.connect(lambda *_: self._on_open_run())
        self.runs_tree.itemSelectionChanged.connect(self._on_run_selection)
        self.runs_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.runs_tree.customContextMenuRequested.connect(self._on_runs_menu)
        box.addWidget(self.runs_tree, 1)

        row = QHBoxLayout()
        self.open_run_btn = QPushButton("Open")
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
        self.history_note.setStyleSheet(MUTED_COLOUR)
        box.addWidget(self.history_note)
        return tab

    def _build_side_pane(self) -> QWidget:
        self.side_panel = SidePanel(
            self._build_history_pane(),
            repo_name=lambda: Path(self._repo_path()).name,
            margins=(SECTION_GAP, 0, 0, 0),
        )
        self.calls_pane.noted.connect(self.status.setText)
        return self.side_panel

    #: The calls half of the side pane, which is what a run talks to.
    calls_pane = property(lambda self: self.side_panel.calls)

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        self.review_btn = QPushButton("Review")
        self.review_btn.clicked.connect(self._on_review)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip(
            "Re-read the staged files. Use after staging something outside this window."
        )
        self.refresh_btn.clicked.connect(self._load_files)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._on_copy)
        self.export_btn = QPushButton("Export...")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._on_export_findings)
        row.addWidget(self.review_btn)
        row.addWidget(self.cancel_btn)
        row.addWidget(self.refresh_btn)
        row.addStretch(1)
        row.addWidget(self.copy_btn)
        row.addWidget(self.export_btn)
        return row

    # ---- state ---------------------------------------------------------------
    def _repo_path(self) -> str:
        return self.repo_picker.current_path()

    def refresh_repos(self) -> None:
        """Reload repositories, keeping any findings already on screen."""
        self.repo_picker.refresh()
        self.refresh_provider()
        self._refresh_tables()
        self._load_files()
        self._sync_shown_run()
        self._refresh_history()
        if self.repo_picker.count() == 0:
            self.status.setText(NO_REPOS_MESSAGE)
        elif self.status.text() == NO_REPOS_MESSAGE:
            self.status.setText("")
        self._update_review_button()

    def refresh_provider(self) -> None:
        """Show the stored provider, without treating that as a user choice."""
        index = self.provider_combo.findData(self.settings.provider)
        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.provider_combo.blockSignals(False)
        model = self.settings.active_model() or "no model selected"
        self.provider_label.setText(f"Model: {model}")

    def _on_provider_changed(self, _index: int) -> None:
        key = self.provider_combo.currentData()
        if not key or key == self.settings.provider:
            return
        self.settings.provider = key
        self.settings.save()
        self.refresh_provider()  # the model line belongs to the new provider

    def _on_repo_changed(self, _path: str = "") -> None:
        self._refresh_tables()
        self._load_files()
        self._sync_shown_run()
        self._refresh_history()
        self._update_review_button()

    def cancel_running(self) -> None:
        """Stop a review in flight (the window is closing)."""
        if self._worker is not None:
            self._worker.cancel()

    # ---- profiles ---------------------------------------------------------------
    def _reload_profiles(self) -> None:
        """Rebuild the list of profiles that can be chosen.

        The repository's own comes first when it has one: a clone should be
        reviewed against the standard the project holds to, and having to go
        and find it in a dropdown is how that does not happen.

        Done once per refresh and kept, not rebuilt per call: the shipped
        defaults and a repository's profile are built fresh each time they are
        read, so two calls hand out two objects and an edit made to one of them
        is lost the moment anything asks again.
        """
        repo_profile, self._repo_tables = shared_profile.read(self._repo_path())
        offered = [*self.settings.review_profiles, profiles_mod.defaults()]
        self._all_profiles = (
            [repo_profile, *offered] if repo_profile is not None else offered
        )

    def _profiles(self) -> list:
        return self._all_profiles

    def _refresh_tables(self) -> None:
        """Show the profiles, selecting the one this repository is assigned."""
        repo = self._repo_path()
        self._migrate(repo)
        self._reload_profiles()
        assigned = self.settings.review_profile_for_repo(repo) if repo else ""

        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for profile in self._all_profiles:
            self.profile_combo.addItem(profile.display(), profile.name)
        index = self.profile_combo.findData(assigned) if assigned else -1
        if index < 0 and self.profile_combo.count():
            index = 0
        self.profile_combo.setCurrentIndex(index)
        self.profile_combo.blockSignals(False)
        # A repository with no assignment gets the one now showing, so a review
        # never runs against rules the user cannot see in the box.
        chosen = self.profile_combo.currentData()
        if repo and chosen and chosen != assigned:
            self.settings.set_repo_review_profile(repo, chosen)
            self.settings.save()
        self._refresh_versions()
        self._show_table()

    def _migrate(self, repo: str) -> None:
        """A repository with one table and no profile keeps reviewing as it did.

        The imported profile checks every file against that one table, which is
        what a review did before profiles existed. Nothing is added to it: a
        review that suddenly reports thirty new findings is indistinguishable
        from a regression in the code.
        """
        if not repo or self.settings.review_profile_for_repo(repo):
            return
        table = self.settings.review_table_for_repo(repo)
        if not table or self._store.find(table) is None:
            return
        profile = profiles_mod.imported(table)
        if self.settings.review_profile(profile.name) is None:
            self.settings.review_profiles.append(profile)
        self.settings.set_repo_review_profile(repo, profile.name)
        self.settings.save()

    def _current_profile(self):
        """The profile a review would run against: the one in the dropdown."""
        wanted = self.profile_combo.currentData()
        return next((p for p in self._all_profiles if p.name == wanted), None)

    def _viewed_profile(self):
        """The profile the Profiles tab has open, which need not be that one."""
        found = next((p for p in self._all_profiles if p.name == self._viewed), None)
        # Falls back to the one under review, so opening the tab shows something
        # rather than nothing, and so a profile that has just been deleted or
        # renamed does not leave the tab blank.
        return found if found is not None else self._current_profile()

    def _refresh_versions(self) -> None:
        """What the repository declares about its own languages."""
        repo = self._repo_path()
        self._detected = {}
        self._version_sources = {}
        self._versions = {}
        if not repo:
            return
        found = version_detection.detect(repo)
        self._detected = {language: d.version for language, d in found.items()}
        self._version_sources = {
            language: d.describe() for language, d in found.items()
        }
        # What a review would use. A version the profile pins is a decision; a
        # detected one is only the best reading of what the repository says
        # about itself. The tab is shown the detection alone, so a version the
        # profile on screen does not pin is never labelled as one it does.
        self._versions = dict(self._detected)
        profile = self._current_profile()
        if profile is not None:
            self._versions.update(profiles_mod.versions_of(profile))

    def _current_table(self) -> RuleTable | None:
        """The table the Rules tab shows: the first of the user's own in use."""
        profile = self._current_profile()
        if profile is not None:
            for entry in profile.languages:
                for selection in entry.selections:
                    if not selection.is_builtin:
                        found = self._store.find(selection.target)
                        if found is not None:
                            return found
        names = self._store.names()
        return self._store.find(names[0]) if names else None

    def _show_table(self) -> None:
        # The profile is drawn whatever the Rules tab has to show: a user with
        # no tables of their own still has the shipped rules, and an empty
        # Profile tab would say the opposite.
        self._show_profile()
        table = self._current_table()
        self.rules_table.setRowCount(0)
        for name in ("rename_table_btn", "delete_table_btn", "export_xlsx_btn"):
            getattr(self, name).setEnabled(table is not None)
        if table is None:
            self.rules_note.setText(NO_RULES_MESSAGE)
            return
        self.rules_table.setRowCount(len(table.rules))
        for row, rule in enumerate(table.rules):
            self.rules_table.setItem(row, 0, QTableWidgetItem(rule.rule_id))
            self.rules_table.setItem(row, 1, QTableWidgetItem(rule.details))
        source = f" from {Path(table.source).name}" if table.source else ""
        self.rules_note.setText(f"{len(table.rules)} rule(s){source}")

    def _show_profile(self) -> None:
        """Draw the profile on screen, with what the repository declares filled in."""
        profile = self._viewed_profile()
        self._viewed = profile.name if profile is not None else ""
        self.profile_tab.show_profiles(
            self._all_profiles,
            self._viewed,
            in_use=self.profile_combo.currentData() or "",
        )
        inlined = dict(self._repo_tables) if profile and profile.from_repository() else {}
        self.profile_tab.show_profile(
            profile,
            _StoreView(self._store, inlined),
            self._detected,
            self._version_sources,
        )
        self.profile_tab.attach_version_pickers()

    def _on_profile_changed(self, _index: int) -> None:
        """Choose the profile the next review runs against.

        This says nothing about which profile the Profiles tab has open; it only
        moves the mark that says which one is in use.
        """
        repo = self._repo_path()
        chosen = self.profile_combo.currentData()
        if repo and chosen:
            self.settings.set_repo_review_profile(repo, chosen)
            self.settings.save()
        self._refresh_versions()
        self._show_table()
        self._update_review_button()

    def _on_import_xlsx(self) -> None:
        path, _chosen = QFileDialog.getOpenFileName(
            self, "Import rules", str(Path.home()), "Spreadsheets (*.xlsx);;All files (*)"
        )
        if not path:
            return
        try:
            table = xlsx.read_rules(path)
        except xlsx.XlsxError as exc:
            QMessageBox.warning(self, "Could not read the spreadsheet", str(exc))
            return
        stored = self._store.add(table)
        self._select_table(stored.name)
        self.status.setText(f"Imported {len(stored.rules)} rule(s) as '{stored.name}'.")

    def _on_export_xlsx(self) -> None:
        table = self._current_table()
        if table is None:
            return
        path, _chosen = QFileDialog.getSaveFileName(
            self,
            "Export rules",
            str(Path.home() / f"{table.name}.xlsx"),
            "Spreadsheets (*.xlsx)",
        )
        if not path:
            return
        try:
            xlsx.write_rules(path, table)
        except xlsx.XlsxError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.status.setText(f"Saved to {path}")

    def _on_import_json(self) -> None:
        path, _chosen = QFileDialog.getOpenFileName(
            self, "Import rule tables", str(Path.home()), "JSON files (*.json)"
        )
        if not path:
            return
        added, renamed = self._store.import_from(path)
        if not added:
            QMessageBox.warning(
                self, "Nothing imported", f"{Path(path).name} holds no rule tables."
            )
            return
        self._refresh_tables()
        note = f"Imported {added} table(s)."
        if renamed:
            note += f" {renamed} kept under a free name; nothing was overwritten."
        self.status.setText(note)

    def _on_export_json(self) -> None:
        if not self._store.tables:
            return
        path, _chosen = QFileDialog.getSaveFileName(
            self,
            "Export rule tables",
            str(Path.home() / "code_review_rules.json"),
            "JSON files (*.json)",
        )
        if not path:
            return
        try:
            self._store.export_to(path)
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.status.setText(f"Saved {len(self._store.tables)} table(s) to {path}")

    def _on_rename_table(self) -> None:
        table = self._current_table()
        if table is None:
            return
        new, ok = QInputDialog.getText(self, "Rename rule table", "Name:", text=table.name)
        new = (new or "").strip()
        if not ok or not new or new == table.name:
            return
        old = table.name
        if not self._store.rename(old, new):
            QMessageBox.warning(
                self, "Could not rename", f"Another table is already called '{new}'."
            )
            return
        # Repositories and profiles point at the table by name; without this
        # their next review would run against a table that no longer exists,
        # which looks exactly like a review that found nothing.
        self.settings.rename_review_table(old, new)
        profiles_mod.rename_table(self.settings.review_profiles, old, new)
        self.settings.save()
        self._select_table(new)

    def _on_delete_table(self) -> None:
        table = self._current_table()
        if table is None:
            return
        if (
            QMessageBox.question(
                self,
                "Delete rule table",
                f"Delete '{table.name}' and its {len(table.rules)} rule(s)?\n\n"
                "Reviews already recorded keep their findings.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        name = table.name
        self._store.remove(name)
        self.settings.remove_review_table(name)
        profiles_mod.remove_table(self.settings.review_profiles, name)
        self.settings.save()
        self._refresh_tables()
        self._update_review_button()

    def _select_table(self, name: str) -> None:
        """Make an imported table the one this repository is reviewed against.

        A table is not a profile, so importing one creates the profile that
        uses it -- otherwise the import would land in the library and change
        nothing about what the next review checks.
        """
        repo = self._repo_path()
        profile = profiles_mod.imported(name)
        if repo and self.settings.review_profile(profile.name) is None:
            self.settings.review_profiles.append(profile)
            self.settings.set_repo_review_profile(repo, profile.name)
            self.settings.save()
        self._refresh_tables()
        index = self.profile_combo.findData(profile.name)
        if index >= 0:
            self.profile_combo.setCurrentIndex(index)
        self._update_review_button()

    # ---- the files to review ----------------------------------------------------
    def _load_files(self) -> None:
        """Show what is staged now, keeping whatever the user unmarked.

        Done synchronously: it is one local ``git diff``, and a worker outliving
        the panel that owns it aborts the process rather than merely failing.
        """
        repo = self._repo_path()
        self._candidates = []
        if repo:
            try:
                self._candidates = staged_files(
                    repo, self.settings.diff_mode, self.settings.ignore_globs
                )
            except Exception:
                # A repo git cannot read shows nothing here; running surfaces
                # the real error.
                self._candidates = []
        self._populate_files()

    def _populate_files(self) -> None:
        unticked = self._unticked.setdefault(norm_path(self._repo_path()), set())
        self.files_list.blockSignals(True)
        self.files_list.clear()
        for candidate in self._candidates:
            suffix = "" if candidate.reviewable else "   [filtered as noise]"
            item = QListWidgetItem(f"{candidate.path}{suffix}")
            item.setData(Qt.ItemDataRole.UserRole, candidate.path)
            item.setToolTip(candidate.path)
            if candidate.reviewable:
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                marked = candidate.path not in unticked
                item.setCheckState(
                    Qt.CheckState.Checked if marked else Qt.CheckState.Unchecked
                )
            else:
                # Listed rather than left out: a file missing from this list
                # reads as a file with nothing wrong with it.
                item.setFlags(Qt.ItemFlag.NoItemFlags)
                item.setForeground(_MUTED)
            self.files_list.addItem(item)
        self.files_list.blockSignals(False)
        self._refresh_files_label()

    def _refresh_files_label(self) -> None:
        marked = len(self.marked_paths())
        total = sum(1 for c in self._candidates if c.reviewable)
        if not self._candidates:
            self.files_label.setText("Files to review - nothing staged")
        else:
            self.files_label.setText(f"Files to review - {marked} of {total} marked")
        self._update_review_button()

    def marked_paths(self) -> list[str]:
        out = []
        for row in range(self.files_list.count()):
            item = self.files_list.item(row)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable and (
                item.checkState() == Qt.CheckState.Checked
            ):
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out

    def _on_file_ticked(self, item: QListWidgetItem) -> None:
        """Remember what was unmarked, so a refresh does not undo the decision."""
        unticked = self._unticked.setdefault(norm_path(self._repo_path()), set())
        path = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            unticked.discard(path)
        else:
            unticked.add(path)
        self._refresh_files_label()

    def _mark_all(self, marked: bool) -> None:
        self.files_list.blockSignals(True)
        unticked = self._unticked.setdefault(norm_path(self._repo_path()), set())
        for row in range(self.files_list.count()):
            item = self.files_list.item(row)
            if not item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
                continue
            item.setCheckState(
                Qt.CheckState.Checked if marked else Qt.CheckState.Unchecked
            )
            path = item.data(Qt.ItemDataRole.UserRole)
            if marked:
                unticked.discard(path)
            else:
                unticked.add(path)
        self.files_list.blockSignals(False)
        self._refresh_files_label()

    # ---- running -----------------------------------------------------------------
    def _update_review_button(self) -> None:
        ready = bool(self._repo_path()) and self._current_profile() is not None
        self.review_btn.setEnabled(
            ready and bool(self.marked_paths()) and self._worker is None
        )

    def _inlined(self) -> dict:
        """The tables a repository's profile brought, when that is the one in use."""
        profile = self._current_profile()
        return dict(self._repo_tables) if profile and profile.from_repository() else {}

    def build_plan(self):
        """What a review of the marked files would do, against this profile."""
        repo = self._repo_path()
        profile = self._current_profile()
        if not repo or profile is None:
            return None
        marked = set(self.marked_paths())
        return build_plan(
            self.settings,
            repo,
            [c.path for c in self._candidates if c.path in marked],
            profiles_mod.rules_for(profile, self._store, inlined=self._inlined()),
            versions=self._versions,
            overrides=profile.overrides,
            profile=profile.name,
            candidates=self._candidates,
        )

    def _on_review(self) -> None:
        paths = self.marked_paths()
        if not self._repo_path() or self._current_profile() is None or not paths:
            return
        if self._before_run is not None:
            self._before_run()  # pick up settings edited in sibling tabs

        # What the run will do, decided once: the window shows this, the
        # estimate prices it, and the run executes it, so none of the three can
        # disagree about which files or which rules.
        plan = self.build_plan()
        profile = self._current_profile()
        if not confirm_plan(
            self,
            plan,
            estimate.for_review(self.settings, plan),
            rules_for=profiles_mod.rules_for(
                profile, self._store, inlined=self._inlined()
            ),
            price=lambda p: estimate.for_review(self.settings, p),
        ):
            return
        self._remember_languages(plan)

        self._set_running(True)
        self.status.setText("Starting...")
        self.calls_pane.reset()  # these belong to the run about to start
        self._clear_findings()

        worker = ReviewWorker(self.settings, plan)
        worker.progress.connect(self.status.setText)
        worker.call.connect(self.calls_pane.add_call)
        worker.finished.connect(self._on_finished)
        worker.error.connect(self._on_error)
        self._worker = worker
        self._thread = run_worker(worker)

    def _remember_languages(self, plan) -> None:
        """Keep a language corrected in the window, so it is asked once.

        Stored per extension on the profile: `.h` is C or C++ for a whole
        repository, not for one file of it.
        """
        profile = self._current_profile()
        if profile is None:
            return
        from git_assistant.metrics import ext_of
        from git_assistant.review import languages as language_ids

        changed = False
        for file in plan.files:
            extension = ext_of(file.path).lower()
            if extension not in language_ids.AMBIGUOUS or not file.language:
                continue
            if profile.overrides.get(extension) != file.language:
                profile.overrides[extension] = file.language
                changed = True
        if changed and profile in self.settings.review_profiles:
            self.settings.save()

    def _on_cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status.setText("Cancelling...")


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

    def _set_running(self, running: bool) -> None:
        self.cancel_btn.setEnabled(running)
        self.repo_picker.setEnabled(not running)
        self.files_list.setEnabled(not running)
        self.profile_combo.setEnabled(not running)
        if running:
            self._busy_start("Code review")
        else:
            self._busy_stop()
        if not running:
            self._update_review_button()
        else:
            self.review_btn.setEnabled(False)

    def _on_finished(self, run) -> None:
        self._worker = None
        self._set_running(False)
        self._show_run(run)

        parts = [run.summary() + "."]
        stored, problem = history.record(run, limit=self.settings.review_history_limit)
        if problem and stored is None:
            parts.append(f"(Not saved to history: {problem})")
        elif problem:
            # It *is* saved -- the run file is the record, and only the index
            # that lists it went wrong. Saying "not saved" here would send
            # someone re-running forty calls they already have on disk.
            parts.append(f"({problem})")
        self._refresh_history(select=stored)
        self.status.setText(" ".join(parts))

    def _on_error(self, message: str) -> None:
        self._worker = None
        self._set_running(False)
        self.status.setText(message)
        if message != "Cancelled.":
            QMessageBox.warning(self, "Review failed", message)

    # ---- what was found ------------------------------------------------------------
    def _clear_findings(self) -> None:
        self._run = None
        self._shown_key = ""
        self.findings_tree.clear()
        self.detail_view.clear()
        self.coverage_note.setText("")
        self.copy_btn.setEnabled(False)
        self.export_btn.setEnabled(False)

    def _sync_shown_run(self) -> None:
        """Drop findings that describe a repository other than the selected one."""
        if self._run is not None and self._shown_key != norm_path(self._repo_path()):
            self._clear_findings()

    def _show_run(self, run, stored=None) -> None:
        self._run = run
        self._shown_key = norm_path(run.repo_path)
        self.findings_tree.clear()
        self.detail_view.clear()

        for review in run.files:
            note = review.note()
            label = f"{review.path}"
            if review.findings:
                label += f"  ({len(review.findings)} finding(s))"
            elif not review.error:
                label += "  (no findings)"
            item = QTreeWidgetItem([label, note])
            item.setData(0, Qt.ItemDataRole.UserRole, review)
            if review.error:
                item.setForeground(0, _PROBLEM)
                item.setText(1, review.error)
            elif review.partial:
                item.setForeground(1, _PARTIAL)
            for finding in review.findings:
                if finding.parsed:
                    text = f"{finding.rule_id} — {finding.message or finding.rule_details}"
                else:
                    text = "the model's reply could not be read"
                child = QTreeWidgetItem([text, finding.label() if finding.parsed else ""])
                child.setData(0, Qt.ItemDataRole.UserRole, finding)
                child.setData(1, Qt.ItemDataRole.UserRole, review)
                if not finding.parsed or not finding.rule_known:
                    child.setForeground(0, _PARTIAL)
                item.addChild(child)
            item.setExpanded(True)
            self.findings_tree.addTopLevelItem(item)

        self.coverage_note.setText(_coverage_note(run, stored))
        self.copy_btn.setEnabled(True)
        self.export_btn.setEnabled(True)
        self.tabs.setCurrentIndex(0)

    def _on_finding_selected(self, current, _previous=None) -> None:
        if current is None:
            return
        payload = current.data(0, Qt.ItemDataRole.UserRole)
        if payload is None:
            return
        if hasattr(payload, "findings"):  # a file row
            self.detail_view.setHtml(_file_html(payload))
            return
        review = current.data(1, Qt.ItemDataRole.UserRole)
        self.detail_view.setHtml(report_mod.finding_html(payload, review))

    def _on_copy(self) -> None:
        if self._run is None:
            return
        QGuiApplication.clipboard().setText(report_mod.to_markdown(self._run))
        self.status.setText("Review copied to the clipboard.")

    def _on_export_findings(self) -> None:
        if self._run is None:
            return
        name = Path(self._run.repo_path).name or "repo"
        path, chosen = QFileDialog.getSaveFileName(
            self,
            "Export the review",
            str(Path.home() / f"{name}-code-review-{date.today().isoformat()}.md"),
            "Markdown (*.md);;Web page (*.html)",
        )
        if not path:
            return
        wants_html = Path(path).suffix.lower() in {".html", ".htm"} or "html" in chosen
        text = (
            report_mod.to_html(self._run)
            if wants_html
            else report_mod.to_markdown(self._run)
        )
        try:
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.status.setText(f"Saved to {path}")

    # ---- previous reviews -------------------------------------------------------
    def _refresh_history(self, select=None) -> None:
        repo = self._repo_path()
        self.runs_tree.clear()
        runs = history.list_runs(repo) if repo else []
        for stored in runs:
            item = QTreeWidgetItem(
                [stored.when_label(), stored.result_label(), stored.table_name]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, stored)
            item.setToolTip(0, _run_tooltip(stored))
            if stored.pinned:
                item.setText(0, f"📌 {stored.when_label()}")
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
        loaded = history.load_run(chosen[0])
        if loaded is None or loaded.run is None:
            QMessageBox.warning(
                self, "Could not open", "That review's file is missing or unreadable."
            )
            return
        self._show_run(loaded.run, stored=loaded)
        self.calls_pane.say(STORED_CALLS_NOTE)
        self.status.setText(f"Showing the review from {loaded.when_label()}.")

    def _on_delete_run(self) -> None:
        chosen = self._selected_runs()
        if not chosen:
            return
        if (
            QMessageBox.question(
                self,
                "Delete review(s)",
                f"Delete {len(chosen)} recorded review(s)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        for stored in chosen:
            history.delete_run(stored)
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
        menu.addAction("Clear this repository's reviews", self._on_clear_history)
        menu.exec(self.runs_tree.viewport().mapToGlobal(point))

    def _on_pin(self, stored, pinned: bool) -> None:
        history.set_pinned(stored, pinned)
        self._refresh_history(select=stored)

    def _on_clear_history(self) -> None:
        repo = self._repo_path()
        if not repo:
            return
        if (
            QMessageBox.question(
                self,
                "Clear reviews",
                f"Forget every recorded review of {Path(repo).name}?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            == QMessageBox.StandardButton.Yes
        ):
            history.clear_repo(repo)
            self._refresh_history()


# ---- text -----------------------------------------------------------------------------
def _inset(widget: QWidget, margins: tuple[int, int, int, int]) -> QWidget:
    """``widget`` in a wrapper that can hold a margin, for use in a splitter."""
    host = QWidget()
    box = QVBoxLayout(host)
    box.setContentsMargins(*margins)
    box.addWidget(widget)
    return host


def _free_profile_name(wanted: str, settings) -> str:
    """``wanted``, or the first numbered variant nobody is using.

    Two profiles with one name is one profile as far as lookup by name is
    concerned, and which of them a repository gets would be an accident.
    """
    taken = {p.name for p in settings.review_profiles}
    if wanted not in taken:
        return wanted
    return next(f"{wanted} {n}" for n in range(2, 1000) if f"{wanted} {n}" not in taken)


def _coverage_note(run, stored=None) -> str:
    """What this review does *not* cover, said before anything it found."""
    parts = []
    if stored is not None:
        parts.append(f"Stored review from {stored.when_label()}.")
    if run.rules_truncated():
        parts.append(
            f"Only {run.rules_sent} of {run.rules_total} rules were sent to the "
            "model; the rest were not checked."
        )
    partial = [r.path for r in run.files if r.partial and not r.error]
    if partial:
        parts.append(f"{len(partial)} file(s) reached the model only in part.")
    failed = run.failed()
    if failed:
        parts.append(f"{len(failed)} file(s) were not reviewed at all.")
    if not parts:
        parts.append(
            f"{run.rules_total} rule(s) checked against every marked file, in full."
        )
    return " ".join(parts)


def _file_html(review) -> str:
    e = html.escape
    out = [f"<h3>{e(review.path)}</h3>"]
    if review.note():
        out.append(f"<p><b>{e(review.note())}</b></p>")
    if review.error:
        out.append(f"<p>Not reviewed: {e(review.error)}</p>")
    if review.retried:
        out.append("<p>The first reply could not be read; the model was asked again.</p>")
    out.append(f"<p>{len(review.findings)} finding(s), answered in {review.seconds:.1f}s.</p>")
    if review.raw_reply:
        out.append(f"<p><b>The model's reply:</b></p><pre>{e(review.raw_reply)}</pre>")
    return "".join(out)


def _run_tooltip(stored) -> str:
    bits = [
        f"{stored.when_label()} — {stored.commit_label()}",
        f"rules: {stored.table_name or 'none'}",
        f"model: {stored.model or 'unknown'} ({stored.provider})",
    ]
    if stored.pinned:
        bits.append("pinned: kept regardless of the retention limit")
    return "\n".join(bits)


def _history_note(repo: str, runs: list, settings) -> str:
    if not repo:
        return ""
    if not runs:
        return "No reviews recorded for this repository yet."
    if settings.review_history_limit <= 0:
        return "Keeping every review."
    return f"Keeping the newest {settings.review_history_limit} review(s)."


class _StoreView:
    """The user's tables, plus any a repository's profile brought with it.

    The Profile tab looks tables up by name; a repository's own must be found
    the same way without ever entering the user's library.
    """

    def __init__(self, store, inlined: dict) -> None:
        self._store = store
        self._by_name = {t.name: t for t in inlined.values()}

    def find(self, name: str):
        return self._by_name.get(name) or self._store.find(name)

    def names(self) -> list[str]:
        return self._store.names()
