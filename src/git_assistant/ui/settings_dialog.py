"""Settings dialog: connection, model picker, repo manager, template, advanced."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import __version__, git_ops
from git_assistant.commit_generator import MIN_PARALLEL_CONTEXT
from git_assistant.config import (
    DEFAULT_TEMPLATE_NAME,
    RepoEntry,
    RepoNode,
    Settings,
    Template,
    build_repo_tree,
    config_path,
)
from git_assistant import credentials, providers
from git_assistant.identities import IdentityStore
from git_assistant.llm import LLMError, ModelInfo, build_client
from git_assistant.prompts import DEFAULT_TEMPLATE
from git_assistant.providers import PROVIDERS
from git_assistant.ui.identities_panel import IdentitiesPanel
from git_assistant.ui.identity_bar import IdentityBar
from git_assistant.ui.preview_dialog import SECTION_GAP, CommitPanel
from git_assistant.ui.tags_panel import TagsPanel
from git_assistant.features import NO_UPDATES_NOTE, UPDATES_SUPPORTED

# Absent from the no-update build; see git_assistant.features.
if UPDATES_SUPPORTED:
    from git_assistant.ui.update_prompt import UpdateCheckWorker
    from git_assistant.updating.client import (
        UpdateConfig,
        ensure_update_config,
        update_config_path,
    )
from git_assistant.tokenizer import input_budget, reserved_output
from git_assistant.ui.workers import FunctionWorker, run_worker

INFO_COLOUR = "color: #8ab;"
WARN_COLOUR = "color: #b36b00;"

# Shown in place of the online version until an update check reports one.
UNKNOWN_VERSION = "?"

#: Href for the "vX.Y.Z available" anchor. Never navigated to — `linkActivated`
#: intercepts it — but QLabel needs an href before it will render an anchor at
#: all, and a real-looking URL here would be opened in a browser by anything
#: that handled the click differently.
INSTALL_LINK = "action:install"


class SettingsDialog(QDialog):
    #: Emitted with an `UpdateResult` when the user clicks the "vX.Y.Z
    #: available" readout. The tray connects it to the same handler the tray
    #: menu uses, so both routes lead to one consent dialog and one installer.
    installRequested = pyqtSignal(object)  # noqa: N815 - Qt signal naming

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._thread = None
        self._conn_worker = None
        self._scan_thread = None
        self._scan_worker = None
        self._update_thread = None
        self._update_worker = None
        # The full UpdateResult from this window's own check. Needed to install:
        # a version string is enough to display but not enough to download and
        # verify, so the readout is only clickable when this is set.
        self._available = None
        self._model_contexts: dict[str, int] = {}  # model id -> detected ctx
        self.setWindowTitle("Git Assistant")
        # QDialog shows only a Close button by default; this is a real app
        # window, so give it the usual minimise/maximise chrome too.
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        self.setMinimumSize(1100, 620)  # the commit tab needs side-by-side room

        tabs = QTabWidget()
        self.tabs = tabs
        self._ready = False  # set once every tab's widgets exist
        tabs.currentChanged.connect(self._on_tab_changed)
        tabs.addTab(self._build_commit_tab(), "Generate Commit Message")
        tabs.addTab(self._build_tags_tab(), "Tags")
        tabs.addTab(self._build_connection_tab(), "Connection && Model")
        tabs.addTab(self._build_repos_tab(), "Repositories")
        # Identities are read from their own file, seeded from git on first run.
        self.identity_store = IdentityStore.bootstrap()
        self.identities_panel = IdentitiesPanel(self.identity_store)
        self.identities_tab_index = tabs.addTab(self.identities_panel, "Identities")
        tabs.addTab(self._build_template_tab(), "Template")
        tabs.addTab(self._build_advanced_tab(), "Advanced")

        # Above the tabs, not inside one: the identity applies to whichever
        # repository is active, and both repo-driven tabs can change that. Each
        # tab owns its own RepoPicker, so the bar follows both.
        self.identity_bar = IdentityBar(self.settings, self.identity_store)
        for panel in (self.commit_panel, self.tags_panel):
            panel.repo_picker.repoChanged.connect(self.identity_bar.set_repo)
        # Editing the list must re-offer it; picking "Manage identities..."
        # is a request for the tab that owns the list.
        self.identities_panel.identitiesChanged.connect(self.identity_bar.refresh)
        self.identity_bar.manageRequested.connect(
            lambda: tabs.setCurrentIndex(self.identities_tab_index)
        )

        # No Save/Cancel: edits are written to disk automatically (debounced).
        open_cfg_btn = QPushButton("Open config folder")
        open_cfg_btn.setToolTip(str(config_path()))
        open_cfg_btn.clicked.connect(self._on_open_config)

        self.saved_hint = QLabel("Changes are saved automatically")
        self.saved_hint.setStyleSheet("color: #888;")

        # Bottom-left version indicator: "<installed> -> <available>".
        # The arrow is hidden unless there is actually something to point at,
        # so "up to date" does not render as "v0.3.4 -> up to date".
        self.version_current = QLabel(f"v{__version__}")
        self.version_current.setToolTip("Installed version")
        self.version_arrow = QLabel("->")
        self.version_online = QLabel(UNKNOWN_VERSION)
        self.version_online.setToolTip("Latest available version")
        # Only links are interactive. Leaving the default (selectable text)
        # would let a drag select the label instead of activating the anchor.
        self.version_online.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard
        )
        self.version_online.linkActivated.connect(self._on_install_clicked)
        for lbl in (self.version_current, self.version_arrow, self.version_online):
            lbl.setStyleSheet("color: #888;")

        bottom = QHBoxLayout()
        bottom.addWidget(self.version_current)
        bottom.addWidget(self.version_arrow)
        bottom.addWidget(self.version_online)
        bottom.addStretch(1)
        bottom.addWidget(self.saved_hint)
        bottom.addWidget(open_cfg_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(self.identity_bar)
        layout.addWidget(tabs)
        layout.addLayout(bottom)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._autosave)

        self._load_into_widgets()
        self._ready = True
        self._connect_autosave()
        self.refresh_update_status()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # Write any debounced edit that has not landed yet.
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._autosave()
        super().closeEvent(event)

    def _on_tab_changed(self, index: int) -> None:
        # Fires while tabs are still being added, before later tabs' widgets
        # exist - ignore until construction has finished.
        if not self._ready or index > 1:
            return
        # Returning to a repo-driven tab: pick up repos added in other tabs.
        repos, roots, _watched = self._collect_repos_and_roots()
        self.settings.repos = repos
        self.settings.scan_roots = roots
        if index == 0:
            self.commit_panel.refresh_repos()
        else:
            self.tags_panel.refresh()
        # RepoPicker.refresh() repopulates with signals blocked, so no
        # repoChanged arrives even when the active repo moved (via the tray, or
        # the other tab). Re-read it here or the bar keeps naming the old repo's
        # identity.
        self.identity_bar.set_repo(self.settings.active_repo)

    def set_online_version(self, version: str | None) -> None:
        """Report the result of a *completed* update check.

        `None` means the check ran and found nothing newer, not "unknown" — the
        tray calls this when its own check comes back empty, and showing `?`
        there would put a shrug next to a question the application has just
        answered. Use `set_update_error` for a check that failed.

        Called by the tray with a bare version string, which is enough to show
        but not to install. `_on_update_found` keeps the full result so the
        readout can be clicked; without one the text is shown but stays inert.
        """
        if not version:
            self._on_update_none()
            return
        self._show_update_state(
            f"v{version} available",
            highlight=version != __version__,
            arrow=True,
            link=self._available is not None,
            tooltip=(
                "Click to install"
                if self._available is not None
                else "Use 'Check for updates...' in the tray menu to install it"
            ),
        )

    def set_update_error(self, message: str) -> None:
        """Report a check that could not complete, including one that failed to
        verify. Not the same as "up to date" and must never render as it."""
        self._on_update_error(message)

    def _show_update_state(
        self,
        text: str,
        *,
        highlight: bool = False,
        arrow: bool = False,
        link: bool = False,
        tooltip: str = "",
    ) -> None:
        """One place that writes the bottom-right version readout.

        Every outcome gets its own words. This used to be a literal `?` that
        nothing ever replaced -- `set_online_version` had no callers at all --
        so "checking", "up to date", "the server is unreachable" and "updates
        are switched off" were indistinguishable, and all four looked like a
        broken updater.

        `link` renders the text as an anchor, which is what gives it the hand
        cursor and the keyboard focus a clickable thing needs. The colour goes
        inline rather than in the stylesheet because a stylesheet `color` does
        not reach the inside of an anchor -- it would render as the default
        blue and look like an unrelated hyperlink.
        """
        colour = "#4caf50" if highlight else "#888"
        self.version_arrow.setVisible(arrow)
        if link:
            self.version_online.setText(
                f'<a href="{INSTALL_LINK}" '
                f'style="color: {colour}; font-weight: bold; text-decoration: none;">'
                f"{text}</a>"
            )
        else:
            self.version_online.setText(text)
        self.version_online.setToolTip(tooltip or "Latest available version")
        self.version_online.setStyleSheet(
            f"color: {colour};" + (" font-weight: bold;" if highlight else "")
        )

    def refresh_update_status(self) -> None:
        """Ask the update service what it has, off the GUI thread.

        Runs when the window is opened *and* when an already-open window is
        raised from the tray. Both matter: this window is constructed once and
        then raised, so a check that only ran in `__init__` would answer with
        whatever was true the first time it was opened -- possibly hours
        earlier, and possibly before the release now being offered existed.

        The tray also pushes its own results in through `set_online_version`,
        so the two readouts cannot disagree.
        """
        if not UPDATES_SUPPORTED:
            # Not "updates are off", which invites looking for the switch that
            # turns them on. There isn't one: the code is not in this build.
            self._show_update_state("no updater", tooltip=NO_UPDATES_NOTE)
            return

        if self._update_thread is not None:
            return  # one at a time

        config = UpdateConfig.load()
        reason = config.unavailable_reason()
        if reason is not None:
            # Nothing to ask, and a worker that raises immediately would only
            # turn a clear reason into a stack trace in a label.
            self._show_update_state("updates are off", tooltip=reason)
            return

        self._show_update_state("checking...", tooltip="Contacting the update service")

        worker = UpdateCheckWorker(config)
        worker.found.connect(self._on_update_found)
        worker.none_available.connect(self._on_update_none)
        worker.error.connect(self._on_update_error)
        # Held for the same reason as the connection test's worker: PyQt can
        # collect it mid-flight and leave the label stuck on "checking...".
        self._update_worker = worker
        thread = run_worker(worker)
        self._update_thread = thread
        # Cleared on thread.finished, not worker.finished, so the guard above
        # stays true for as long as the thread is genuinely alive.
        thread.finished.connect(self._forget_update_worker)

    def _forget_update_worker(self) -> None:
        self._update_worker = None
        self._update_thread = None

    def _on_update_found(self, result: object) -> None:
        self._available = result
        self.set_online_version(getattr(result, "version", None))

    def _on_install_clicked(self, _href: str) -> None:
        """Hand the click to whoever owns installing.

        A signal rather than calling `ask_to_install` here, so there is one
        place that decides what accepting an update does. Two copies of that
        decision is how a consent dialog ends up meaning different things
        depending on which button you pressed to reach it.
        """
        if self._available is not None:
            self.installRequested.emit(self._available)

    def _on_update_none(self) -> None:
        self._show_update_state(
            "up to date",
            tooltip=f"The update service has nothing newer than v{__version__}",
        )

    def _on_update_error(self, message: str) -> None:
        # Verification failures land here too, and they are not "no update".
        self._show_update_state("update check failed", tooltip=message)

    # ---- tabs --------------------------------------------------------------
    def _build_commit_tab(self) -> QWidget:
        # Does not auto-generate: opening the window should cost nothing.
        # Settings edited in other tabs are applied just before a run.
        self.commit_panel = CommitPanel(
            self.settings,
            auto_start=False,
            before_generate=self._apply_to_settings,
        )
        return self.commit_panel

    def _build_tags_tab(self) -> QWidget:
        self.tags_panel = TagsPanel(self.settings)
        return self.tags_panel

    def _build_connection_tab(self) -> QWidget:
        w = QWidget()
        outer = QHBoxLayout(w)

        # ---- left: which backend generates the message --------------------
        providers_pane = QWidget()
        providers_box = QVBoxLayout(providers_pane)
        providers_box.setContentsMargins(0, 0, SECTION_GAP, 0)
        providers_box.addWidget(QLabel("Inference Providers"))

        self.provider_list = QListWidget()
        self.provider_list.setMaximumWidth(220)
        for provider in PROVIDERS:
            item = QListWidgetItem(provider.display())
            item.setData(Qt.ItemDataRole.UserRole, provider.key)
            if not provider.implemented:
                item.setToolTip(
                    f"{provider.label} is listed but has no client yet. "
                    "Selecting it is remembered; generating with it will say so."
                )
            self.provider_list.addItem(item)
        self.provider_list.currentItemChanged.connect(self._on_provider_selected)
        providers_box.addWidget(self.provider_list, 1)

        self.provider_note = QLabel("")
        self.provider_note.setWordWrap(True)
        self.provider_note.setStyleSheet("color: #8ab;")
        providers_box.addWidget(self.provider_note)

        form_container = QWidget()
        form = QFormLayout(form_container)

        outer.addWidget(providers_pane)
        outer.addWidget(form_container, 1)

        self.ip_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)

        self.test_btn = QPushButton("Test connection && list models")
        self.test_btn.clicked.connect(self._on_test_connection)
        self.conn_status = QLabel("")
        self.conn_status.setWordWrap(True)

        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(360)
        self.model_combo.currentIndexChanged.connect(self._update_budget_label)

        self.ctx_size_spin = QSpinBox()
        self.ctx_size_spin.setRange(0, 1_048_576)
        self.ctx_size_spin.setSingleStep(1024)
        self.ctx_size_spin.setGroupSeparatorShown(True)
        self.ctx_size_spin.setSpecialValueText("Auto-detect")
        self.ctx_size_spin.setToolTip(
            "Total tokens per request (input + output combined). Set this to "
            "match the context length the model is loaded with in LM Studio. "
            "0 = auto-detect the model's maximum."
        )
        self.ctx_size_spin.valueChanged.connect(self._update_budget_label)

        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 32)
        self.parallel_spin.setToolTip(
            "How many LLM requests to run at once when a large diff is split "
            "into chunks (map-reduce). Higher is faster, but concurrent requests "
            "SHARE the model's context window, so each chunk gets a smaller "
            "slice. 1 = sequential (largest chunks)."
        )
        self.parallel_spin.valueChanged.connect(self._update_budget_label)

        self.budget_label = QLabel("")
        self.budget_label.setWordWrap(True)
        self.budget_label.setStyleSheet("color: #8ab;")

        # ---- credentials, for the providers that need one ------------------
        # Never written to settings.json. The field shows a placeholder rather
        # than the stored key: reading it back to display it would put the
        # secret on screen and in Qt's widget memory for no benefit, and the
        # only things anyone needs to know are whether one is stored and how
        # to replace it.
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setMinimumWidth(360)
        self.api_key_edit.editingFinished.connect(self._on_api_key_entered)

        self.clear_key_btn = QPushButton("Remove")
        self.clear_key_btn.setToolTip(
            "Delete this provider's key from the Windows Credential Manager."
        )
        self.clear_key_btn.clicked.connect(self._on_clear_api_key)

        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key_edit, 1)
        key_row.addWidget(self.clear_key_btn)
        self.key_row_host = QWidget()
        self.key_row_host.setLayout(key_row)

        self.key_status = QLabel("")
        self.key_status.setWordWrap(True)
        self.key_status.setStyleSheet(INFO_COLOUR)

        self.endpoint_edit = QLineEdit()
        self.endpoint_edit.setMinimumWidth(360)
        self.endpoint_edit.setToolTip(
            "The address of your deployment. Shown only for providers whose "
            "address is not fixed by the vendor."
        )

        form.addRow("LM Studio IP:", self.ip_edit)
        form.addRow("Port:", self.port_spin)
        form.addRow("Endpoint:", self.endpoint_edit)
        form.addRow("API key:", self.key_row_host)
        form.addRow("", self.key_status)
        form.addRow("", self.test_btn)
        form.addRow("Model:", self.model_combo)
        form.addRow("Context window size:", self.ctx_size_spin)
        form.addRow("Parallel requests:", self.parallel_spin)
        form.addRow("Status:", self.conn_status)
        form.addRow("Effective budget:", self.budget_label)

        # Rows whose visibility depends on the provider. Held by field widget
        # because that is what setRowVisible takes, and hiding the field alone
        # would leave its label behind.
        self._provider_rows = {
            "lmstudio_only": (self.ip_edit, self.port_spin),
            "endpoint": (self.endpoint_edit,),
            "key": (self.key_row_host, self.key_status),
        }
        self._conn_form = form
        return w

    def _apply_provider_fields(self, provider) -> None:
        """Show only the settings the selected provider actually has.

        Greying them out instead would leave an LM Studio IP box on screen for
        a hosted API that has no address to set, which reads as something the
        user forgot to fill in.
        """
        form = self._conn_form
        for field in self._provider_rows["lmstudio_only"]:
            form.setRowVisible(field, provider.key == "lmstudio")
        for field in self._provider_rows["endpoint"]:
            form.setRowVisible(field, provider.needs_endpoint)
        for field in self._provider_rows["key"]:
            form.setRowVisible(field, provider.needs_api_key)

        self.endpoint_edit.setPlaceholderText(provider.endpoint_hint)
        # Real, editable text rather than a greyed hint. For the local servers
        # the default address *is* the answer, and a placeholder reads as an
        # example you still have to type out yourself. Azure has no default --
        # its address is per-resource -- so that field stays empty and the hint
        # is all there is to show.
        self.endpoint_edit.setText(
            self.settings.provider_endpoint(provider.key) or provider.base_url
        )
        self._refresh_key_status(provider)

    # ---- API keys ----------------------------------------------------------
    def _refresh_key_status(self, provider) -> None:
        """Say whether a key is stored, and where it lives. Never show it."""
        if not provider.needs_api_key:
            self.api_key_edit.setPlaceholderText("")
            return

        if not credentials.available():
            self.key_status.setText(
                "No credential store on this platform, so keys cannot be saved."
            )
            self.key_status.setStyleSheet(WARN_COLOUR)
            self.api_key_edit.setEnabled(False)
            self.clear_key_btn.setEnabled(False)
            return

        self.api_key_edit.setEnabled(True)
        stored = credentials.has_secret(provider.key)
        self.clear_key_btn.setEnabled(stored)
        self.api_key_edit.setPlaceholderText(
            "A key is stored - type a new one to replace it"
            if stored
            else "Paste the API key"
        )
        self.key_status.setText(
            (
                "Stored in the Windows Credential Manager as "
                f"'{credentials.target_for(provider.key)}', not in settings.json. "
                if stored
                else "No key stored yet. "
            )
            + provider.key_help
        )
        self.key_status.setStyleSheet(INFO_COLOUR if stored else WARN_COLOUR)

    def _on_api_key_entered(self) -> None:
        """Store what was typed, then clear the box.

        Cleared immediately so the secret is not left in a widget for the rest
        of the session, and because the box is not a display of the key -- it
        is only ever an input for a new one.
        """
        key = self.api_key_edit.text().strip()
        if not key:
            return
        provider = providers.get(self.settings.provider)
        self.api_key_edit.clear()
        try:
            credentials.set_secret(provider.key, key)
        except credentials.CredentialError as exc:
            QMessageBox.warning(self, "Could not save the API key", str(exc))
        self._refresh_key_status(provider)

    def _on_clear_api_key(self) -> None:
        provider = providers.get(self.settings.provider)
        confirm = QMessageBox.question(
            self,
            "Remove API key?",
            f"Delete the stored {provider.label} key from the Windows "
            "Credential Manager?",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            credentials.delete_secret(provider.key)
        except credentials.CredentialError as exc:
            QMessageBox.warning(self, "Could not remove the API key", str(exc))
        self._refresh_key_status(provider)

    def _on_provider_selected(self, current=None, _previous=None) -> None:
        """Record the chosen provider and reshape the form around it.

        Each provider needs different settings -- a local address, a key, an
        endpoint -- so the rows that do not apply are hidden rather than
        greyed. Model and endpoint are stored per provider, so switching does
        not carry one backend's model name into another's request.
        """
        item = current or self.provider_list.currentItem()
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        provider = providers.get(key)

        if provider.implemented:
            self.provider_note.setText("")
        else:
            self.provider_note.setText(
                f"{provider.label} has no client yet. It is remembered as your "
                "choice, but generating will report that it is unavailable "
                "rather than quietly using LM Studio."
            )
        self.provider_note.setStyleSheet(
            INFO_COLOUR if provider.implemented else WARN_COLOUR
        )

        if self._ready:
            self.settings.provider = key
            self._schedule_save()
            self.commit_panel.refresh_provider()

        self._apply_provider_fields(provider)
        self._show_provider_model(provider)

    def _show_provider_model(self, provider) -> None:
        """Show the model stored for this provider, not the previous one's.

        Listing is per provider and needs a working connection, so the combo
        starts with just the remembered value; "Test connection" fills the
        rest in.
        """
        stored = self.settings.provider_model(provider.key)
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        if stored:
            self.model_combo.addItem(stored, stored)
            self.model_combo.setCurrentIndex(0)
        self.model_combo.blockSignals(False)
        self._update_budget_label()

    def _build_repos_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(
            QLabel(
                "Repositories in the tray menu, grouped by scanned folder and "
                "nested under the repository they are a submodule of.\n"
                "Tick a folder to auto-add new repos cloned into it."
            )
        )

        self.repo_tree = QTreeWidget()
        self.repo_tree.setHeaderHidden(True)
        self.repo_tree.setSelectionMode(
            QTreeWidget.SelectionMode.ExtendedSelection
        )
        layout.addWidget(self.repo_tree)

        row = QHBoxLayout()
        add_btn = QPushButton("Add repo...")
        add_btn.clicked.connect(self._on_add_repo)
        self.scan_btn = QPushButton("Add folder (scan for repos)...")
        self.scan_btn.clicked.connect(self._on_scan_folder)
        self.rescan_btn = QPushButton("Rescan selected folder")
        self.rescan_btn.setToolTip(
            "Re-scan the selected folder for new repositories and refresh owners."
        )
        self.rescan_btn.clicked.connect(self._on_rescan_selected)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._on_remove_repo)
        row.addWidget(add_btn)
        row.addWidget(self.scan_btn)
        row.addWidget(self.rescan_btn)
        row.addWidget(remove_btn)
        row.addStretch(1)
        self.trust_btn = QPushButton("Fix blocked repos...")
        self.trust_btn.setToolTip(
            "Run: git config --global --add safe.directory '*'\n"
            "Clears git 'dubious ownership' errors for repos owned by "
            "another account."
        )
        self.trust_btn.clicked.connect(self._on_trust_all)
        row.addWidget(self.trust_btn)
        layout.addLayout(row)

        self.scan_status = QLabel("")
        self.scan_status.setStyleSheet("color: #8ab;")
        self.scan_status.setWordWrap(True)
        layout.addWidget(self.scan_status)
        return w

    def _build_template_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(
            QLabel(
                "Prompt templates. Assign one per repository in the "
                "Generate Commit Message tab.\n"
                "Placeholders: {branch}, {diffstat}, {diff}"
            )
        )

        # ---- left: filter + template list ---------------------------------
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, SECTION_GAP, 0)
        self.template_filter = QLineEdit()
        self.template_filter.setPlaceholderText("Filter templates...")
        self.template_filter.setClearButtonEnabled(True)
        self.template_filter.textChanged.connect(self._apply_template_filter)
        left_box.addWidget(self.template_filter)

        self.template_list = QListWidget()
        self.template_list.currentTextChanged.connect(self._on_template_selected)
        left_box.addWidget(self.template_list, 1)

        list_btns = QHBoxLayout()
        for text, slot in (
            ("New", self._on_template_new),
            ("Duplicate", self._on_template_duplicate),
            ("Rename", self._on_template_rename),
            ("Delete", self._on_template_delete),
        ):
            b = QPushButton(text)
            b.clicked.connect(slot)
            list_btns.addWidget(b)
        left_box.addLayout(list_btns)

        # ---- right: the template body --------------------------------------
        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(SECTION_GAP, 0, 0, 0)
        self.template_name_label = QLabel("")
        font = self.template_name_label.font()
        font.setBold(True)
        self.template_name_label.setFont(font)
        right_box.addWidget(self.template_name_label)

        self.template_edit = QPlainTextEdit()
        self.template_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.template_edit.textChanged.connect(self._on_template_text_changed)
        right_box.addWidget(self.template_edit, 1)

        edit_btns = QHBoxLayout()
        reset_btn = QPushButton("Reset to default text")
        reset_btn.setToolTip("Replace this template's body with the built-in one.")
        reset_btn.clicked.connect(
            lambda: self.template_edit.setPlainText(DEFAULT_TEMPLATE)
        )
        import_btn = QPushButton("Import...")
        import_btn.clicked.connect(self._on_template_import)
        export_btn = QPushButton("Export...")
        export_btn.clicked.connect(self._on_template_export)
        edit_btns.addWidget(reset_btn)
        edit_btns.addStretch(1)
        edit_btns.addWidget(import_btn)
        edit_btns.addWidget(export_btn)
        right_box.addLayout(edit_btns)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        layout.addWidget(splitter, 1)

        self.template_status = QLabel("")
        self.template_status.setStyleSheet("color: #8ab;")
        self.template_status.setWordWrap(True)
        layout.addWidget(self.template_status)
        return w

    def _build_advanced_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        form_container = QWidget()
        form = QFormLayout(form_container)

        self.diff_mode_combo = QComboBox()
        self.diff_mode_combo.addItem("Staged changes (git diff --cached)", "cached")
        self.diff_mode_combo.addItem("All uncommitted (git diff HEAD)", "working")

        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0.0, 0.5)
        self.margin_spin.setSingleStep(0.05)
        self.margin_spin.setDecimals(2)
        self.margin_spin.setToolTip(
            "Fraction of the context window reserved for the generated message. "
            "The rest is available for the diff."
        )

        self.ignore_edit = QPlainTextEdit()
        self.ignore_edit.setPlaceholderText("One glob per line, e.g. *.lock")
        self.ignore_edit.setMaximumHeight(140)

        # Where updates come from. Read-only here on purpose: this dialog saves
        # automatically, and the update address is the one setting that must
        # not be changed by a stray keystroke in a window that writes as you
        # type. The button opens the file instead, so changing it is a
        # deliberate act.
        update_row = QHBoxLayout()
        self.update_source = QLabel()
        self.update_source.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.update_source.setWordWrap(True)
        update_row.addWidget(self.update_source, 1)
        # No Edit button without an updater: it opens `update.json`, and a file
        # that configures a subsystem this build does not contain is worse than
        # no file -- it implies the address is the reason nothing updates.
        if UPDATES_SUPPORTED:
            edit_update_btn = QPushButton("Edit...")
            edit_update_btn.setToolTip(str(update_config_path()))
            edit_update_btn.clicked.connect(self._on_edit_update_config)
            update_row.addWidget(edit_update_btn)

        form.addRow("Diff source:", self.diff_mode_combo)
        form.addRow("Output reserve (margin):", self.margin_spin)
        form.addRow("Ignore globs:", self.ignore_edit)
        form.addRow("Update service:", update_row)

        # Keep the Connection tab's effective-budget readout in sync.
        self.margin_spin.valueChanged.connect(self._update_budget_label)

        # Pin the form to the top; extra vertical space goes to the stretch below.
        outer.addWidget(form_container)
        outer.addStretch(1)
        return w

    # ---- load / save -------------------------------------------------------
    def _load_into_widgets(self) -> None:
        s = self.settings
        self.ip_edit.setText(s.lmstudio_ip)
        self.port_spin.setValue(s.lmstudio_port)
        self.parallel_spin.setValue(s.parallel_calls)

        # Selecting the stored provider also fills in the note beside the list.
        # `_ready` is still False here, so this does not count as a user choice.
        for row in range(self.provider_list.count()):
            if self.provider_list.item(row).data(Qt.ItemDataRole.UserRole) == s.provider:
                self.provider_list.setCurrentRow(row)
                break
        else:
            self.provider_list.setCurrentRow(0)
        # Fills the model combo and the provider-specific rows for whichever
        # provider was stored, so there is nothing to populate separately here.
        self._on_provider_selected()

        self._populate_repo_tree(s.repos, s.scan_roots)

        self._reload_templates(select=DEFAULT_TEMPLATE_NAME)

        idx = self.diff_mode_combo.findData(s.diff_mode)
        self.diff_mode_combo.setCurrentIndex(max(0, idx))
        self.ctx_size_spin.setValue(s.context_window)
        self.margin_spin.setValue(s.safety_margin)
        self.ignore_edit.setPlainText("\n".join(s.ignore_globs))
        self._update_budget_label()
        self._refresh_update_source()

    # ---- autosave ----------------------------------------------------------
    def _connect_autosave(self) -> None:
        """Persist edits automatically; no Save button to press."""
        self.ip_edit.textChanged.connect(self._schedule_save)
        self.port_spin.valueChanged.connect(self._schedule_save)
        self.parallel_spin.valueChanged.connect(self._schedule_save)
        self.endpoint_edit.textChanged.connect(self._schedule_save)
        self.model_combo.currentIndexChanged.connect(self._schedule_save)
        self.diff_mode_combo.currentIndexChanged.connect(self._schedule_save)
        self.ctx_size_spin.valueChanged.connect(self._schedule_save)
        self.margin_spin.valueChanged.connect(self._schedule_save)
        self.ignore_edit.textChanged.connect(self._schedule_save)
        # The template editor saves via _on_template_text_changed, which knows
        # which template the text belongs to.
        # Repo tree: checkbox toggles plus add/remove/scan (which call directly).
        self.repo_tree.itemChanged.connect(self._schedule_save)

    def _schedule_save(self, *_args) -> None:
        # Debounced so typing does not rewrite the file on every keystroke.
        if self._ready:
            self._save_timer.start()

    def _autosave(self) -> None:
        self._apply_to_settings()
        self.settings.save()

    def _apply_to_settings(self) -> None:
        """Copy every widget's value into ``self.settings`` (without saving)."""
        s = self.settings
        s.lmstudio_ip = self.ip_edit.text().strip() or "127.0.0.1"
        s.lmstudio_port = self.port_spin.value()
        s.parallel_calls = self.parallel_spin.value()

        # Model and endpoint belong to the selected provider, so switching does
        # not carry one backend's model name into another's request. The API
        # key is deliberately absent: it is in the Credential Manager, and
        # nothing on this path may write it to settings.json.
        provider_key = s.provider
        s.set_provider_model(
            provider_key,
            self.model_combo.currentData() or self.model_combo.currentText(),
        )
        # An untouched default is stored as "unset", so it keeps tracking
        # `Provider.base_url` instead of pinning today's value into
        # settings.json -- where a later change to the default would never
        # reach the people who never edited the field.
        endpoint = self.endpoint_edit.text().strip()
        if endpoint == providers.get(provider_key).base_url:
            endpoint = ""
        s.set_provider_endpoint(provider_key, endpoint)

        repos, roots, watched = self._collect_repos_and_roots()
        s.repos = repos
        s.scan_roots = roots
        s.watched_roots = watched
        if s.active_repo not in {r.path for r in repos}:
            s.active_repo = repos[0].path if repos else ""

        # Template bodies are written as they are edited (they belong to
        # whichever template is selected), so nothing to collect here.
        s.diff_mode = self.diff_mode_combo.currentData()

        # Stored as typed. A value above the model's real maximum is flagged in
        # the budget label and clamped at generation time - a modal warning here
        # would fire mid-keystroke now that saving is automatic.
        s.context_window = self.ctx_size_spin.value()

        s.safety_margin = self.margin_spin.value()
        s.ignore_globs = [
            line.strip()
            for line in self.ignore_edit.toPlainText().splitlines()
            if line.strip()
        ]

    # ---- templates ---------------------------------------------------------
    def _reload_templates(self, select: str | None = None) -> None:
        """Rebuild the list from settings, keeping (or choosing) a selection."""
        want = select or self.template_list.currentItem()
        want = want if isinstance(want, str) else (want.text() if want else None)
        self.template_list.blockSignals(True)
        self.template_list.clear()
        self.template_list.addItems(self.settings.template_names())
        self.template_list.blockSignals(False)
        self._apply_template_filter(self.template_filter.text())
        items = self.template_list.findItems(want or "", Qt.MatchFlag.MatchExactly)
        if items and not items[0].isHidden():
            self.template_list.setCurrentItem(items[0])
        else:
            for i in range(self.template_list.count()):
                if not self.template_list.item(i).isHidden():
                    self.template_list.setCurrentRow(i)
                    break

    def _apply_template_filter(self, text: str) -> None:
        """Hide templates whose name does not contain the filter text."""
        needle = (text or "").strip().lower()
        for i in range(self.template_list.count()):
            item = self.template_list.item(i)
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _current_template(self) -> str:
        item = self.template_list.currentItem()
        return item.text() if item else DEFAULT_TEMPLATE_NAME

    def _on_template_selected(self, name: str) -> None:
        if not name:
            return
        self.template_name_label.setText(name)
        self.template_edit.blockSignals(True)
        self.template_edit.setPlainText(self.settings.template_text(name))
        self.template_edit.blockSignals(False)
        self.template_status.setText("")

    def _on_template_text_changed(self) -> None:
        """Write the edited body back to whichever template is selected."""
        if not self._ready:
            return
        name = self._current_template()
        text = self.template_edit.toPlainText()
        if name == DEFAULT_TEMPLATE_NAME:
            self.settings.prompt_template = text
        else:
            for t in self.settings.templates:
                if t.name == name:
                    t.text = text
                    break
        self._schedule_save()

    def _unique_template_name(self, base: str) -> str:
        existing = set(self.settings.template_names())
        if base not in existing:
            return base
        n = 2
        while f"{base} ({n})" in existing:
            n += 1
        return f"{base} ({n})"

    def _on_template_new(self) -> None:
        name, ok = QInputDialog.getText(
            self, "New template", "Name (e.g. the project it is for):"
        )
        name = (name or "").strip()
        if not ok or not name:
            return
        if name in self.settings.template_names():
            QMessageBox.warning(self, "Name in use", f"'{name}' already exists.")
            return
        self.settings.templates.append(Template(name=name, text=DEFAULT_TEMPLATE))
        self._schedule_save()
        self._reload_templates(select=name)

    def _on_template_duplicate(self) -> None:
        source = self._current_template()
        name = self._unique_template_name(f"{source} copy")
        self.settings.templates.append(
            Template(name=name, text=self.settings.template_text(source))
        )
        self._schedule_save()
        self._reload_templates(select=name)

    def _on_template_rename(self) -> None:
        old = self._current_template()
        if old == DEFAULT_TEMPLATE_NAME:
            QMessageBox.information(
                self, "Cannot rename", "The default template keeps its name."
            )
            return
        new, ok = QInputDialog.getText(self, "Rename template", "New name:", text=old)
        new = (new or "").strip()
        if not ok or not new or new == old:
            return
        if new in self.settings.template_names():
            QMessageBox.warning(self, "Name in use", f"'{new}' already exists.")
            return
        self.settings.rename_template(old, new)  # also repoints repositories
        self._schedule_save()
        self._reload_templates(select=new)

    def _on_template_delete(self) -> None:
        name = self._current_template()
        if name == DEFAULT_TEMPLATE_NAME:
            QMessageBox.information(
                self,
                "Cannot delete",
                "The default template is always available. Use 'Reset to default "
                "text' to restore its contents.",
            )
            return
        users = [r.display() for r in self.settings.repos if r.template == name]
        note = (
            f"\n\n{len(users)} repository(ies) use it and will fall back to the "
            f"default:\n  " + "\n  ".join(users[:8])
            if users
            else ""
        )
        if (
            QMessageBox.question(
                self, "Delete template", f"Delete the template '{name}'?{note}"
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.settings.remove_template(name)
        self._schedule_save()
        self._reload_templates(select=DEFAULT_TEMPLATE_NAME)

    def _on_template_export(self) -> None:
        name = self._current_template()
        suggested = re.sub(r"[^\w.-]+", "_", name) + ".json"
        path, _ = QFileDialog.getSaveFileName(
            self, "Export template", suggested, "Template files (*.json)"
        )
        if not path:
            return
        payload = [{"name": name, "text": self.settings.template_text(name)}]
        try:
            Path(path).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.template_status.setText(f"Exported '{name}' to {path}")

    def _on_template_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import templates", "", "Template files (*.json)"
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.critical(self, "Import failed", f"Could not read the file:\n{exc}")
            return

        # Accept a single template or a list of them.
        items = data if isinstance(data, list) else [data]
        added, last = 0, None
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            # Never silently overwrite an existing template.
            name = self._unique_template_name(str(item["name"]).strip())
            self.settings.templates.append(
                Template(name=name, text=str(item.get("text", DEFAULT_TEMPLATE)))
            )
            added += 1
            last = name
        if not added:
            QMessageBox.warning(
                self,
                "Nothing imported",
                "The file contained no templates. Expected JSON like:\n"
                '[{"name": "My project", "text": "..."}]',
            )
            return
        self._schedule_save()
        self._reload_templates(select=last)
        self.template_status.setText(f"Imported {added} template(s) from {path}")

    # ---- repo tree helpers -------------------------------------------------
    @staticmethod
    def _norm(path: str) -> str:
        return os.path.normcase(os.path.normpath(path))

    def _item_kind(self, item: QTreeWidgetItem) -> str:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, RepoEntry):
            return "repo"
        if isinstance(data, tuple) and data:
            return data[0]  # "root" | "other"
        return "unknown"

    def _header_path(self, item: QTreeWidgetItem) -> str:
        return item.data(0, Qt.ItemDataRole.UserRole)[1]

    def _make_repo_item(self, entry: RepoEntry) -> QTreeWidgetItem:
        # The parent header shows the directory, so the row needs only the name.
        it = QTreeWidgetItem([entry.display()])
        it.setData(0, Qt.ItemDataRole.UserRole, entry)
        it.setToolTip(0, entry.path)
        return it

    def _make_header(self, text: str, data: tuple) -> QTreeWidgetItem:
        it = QTreeWidgetItem([text])
        it.setData(0, Qt.ItemDataRole.UserRole, data)
        font = it.font(0)
        font.setBold(True)
        it.setFont(0, font)
        return it

    def _new_root_header(
        self, path: str, count: int, watched: bool
    ) -> QTreeWidgetItem:
        it = self._make_header(f"{path}   ({count})", ("root", path))
        it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        it.setCheckState(
            0, Qt.CheckState.Checked if watched else Qt.CheckState.Unchecked
        )
        it.setToolTip(
            0, f"{path}\nTick to auto-add repos cloned into this folder."
        )
        return it

    def _is_watched(self, path: str) -> bool:
        watched = {self._norm(w) for w in self.settings.watched_roots}
        return self._norm(path) in watched

    def _root_for(self, path: str, roots: list[str]) -> str | None:
        """Deepest scan root that contains ``path``, or None."""
        np = self._norm(path)
        best, best_len = None, -1
        for r in roots:
            nr = self._norm(r)
            if (np == nr or np.startswith(nr + os.sep)) and len(nr) > best_len:
                best, best_len = r, len(nr)
        return best

    def _repo_items_under(self, item: QTreeWidgetItem):
        """Repo rows below ``item``, parents before their submodules."""
        for j in range(item.childCount()):
            child = item.child(j)
            if self._item_kind(child) == "repo":
                yield child
            yield from self._repo_items_under(child)

    def _all_repo_items(self):
        """Every repo row in the tree, at any nesting depth."""
        tree = self.repo_tree
        for i in range(tree.topLevelItemCount()):
            top = tree.topLevelItem(i)
            if self._item_kind(top) == "repo":
                yield top
            yield from self._repo_items_under(top)

    def _repo_items_by_path(self) -> dict:
        out = {}
        for it in self._all_repo_items():
            entry: RepoEntry = it.data(0, Qt.ItemDataRole.UserRole)
            out[self._norm(entry.path)] = it
        return out

    def _find_header(self, kind: str, path: str | None = None) -> QTreeWidgetItem | None:
        for i in range(self.repo_tree.topLevelItemCount()):
            top = self.repo_tree.topLevelItem(i)
            if self._item_kind(top) != kind:
                continue
            if kind != "root" or self._norm(self._header_path(top)) == self._norm(path):
                return top
        return None

    def _ensure_root_header(self, folder: str) -> QTreeWidgetItem:
        existing = self._find_header("root", folder)
        if existing is not None:
            return existing
        header = self._new_root_header(folder, 0, self._is_watched(folder))
        # Keep the "Other" group last.
        other = self._find_header("other")
        if other is not None:
            idx = self.repo_tree.indexOfTopLevelItem(other)
            self.repo_tree.insertTopLevelItem(idx, header)
        else:
            self.repo_tree.addTopLevelItem(header)
        return header

    def _current_roots(self) -> list[str]:
        return [
            self._header_path(self.repo_tree.topLevelItem(i))
            for i in range(self.repo_tree.topLevelItemCount())
            if self._item_kind(self.repo_tree.topLevelItem(i)) == "root"
        ]

    def _header_for_repo_path(self, path: str) -> QTreeWidgetItem:
        """Folder header a repo belongs under, creating it when needed."""
        root = self._root_for(path, self._current_roots())
        if root is None:
            root = os.path.dirname(path.rstrip("\\/"))
        return self._ensure_root_header(root) if root else self._ensure_other_header()

    def _containing_repo_item(self, path: str) -> QTreeWidgetItem | None:
        """Deepest repo row whose working tree contains ``path``, if any.

        A submodule lives inside its parent's working tree, so containment is
        what nests it -- no second source of truth to keep in step with the
        paths themselves.
        """
        np = self._norm(path)
        best, best_len = None, -1
        for it in self._all_repo_items():
            entry: RepoEntry = it.data(0, Qt.ItemDataRole.UserRole)
            key = self._norm(entry.path)
            if np.startswith(key + os.sep) and len(key) > best_len:
                best, best_len = it, len(key)
        return best

    def _parent_for_repo_path(self, path: str) -> QTreeWidgetItem:
        """Row a repo should hang off: its containing repo, else its folder."""
        return self._containing_repo_item(path) or self._header_for_repo_path(path)

    def _ensure_other_header(self) -> QTreeWidgetItem:
        existing = self._find_header("other")
        if existing is not None:
            return existing
        header = self._make_header("Other repositories   (0)", ("other", None))
        self.repo_tree.addTopLevelItem(header)
        return header

    def _refresh_counts(self) -> None:
        # Runs after every repo add/remove/scan, so persist those edits too.
        self._schedule_save()
        for i in range(self.repo_tree.topLevelItemCount()):
            top = self.repo_tree.topLevelItem(i)
            kind = self._item_kind(top)
            # Submodules are repos of their own, so they count towards the total.
            n = sum(1 for _ in self._repo_items_under(top))
            if kind == "root":
                top.setText(0, f"{self._header_path(top)}   ({n})")
            elif kind == "other":
                top.setText(0, f"Other repositories   ({n})")

    def _prune_empty_other(self) -> None:
        other = self._find_header("other")
        if other is not None and other.childCount() == 0:
            self.repo_tree.takeTopLevelItem(self.repo_tree.indexOfTopLevelItem(other))

    def _prune_missing_under(self, header: QTreeWidgetItem | None) -> int:
        """Offer to remove repos under ``header`` whose folder is gone from disk.

        Only repos that truly no longer have a ``.git`` are considered, so repos
        merely missed by the scan heuristic (depth, pruning) are never dropped.
        Returns the number removed (0 if declined or none missing).
        """
        if header is None:
            return 0
        gone = [
            it
            for it in self._repo_items_under(header)
            if not git_ops.has_git_dir(it.data(0, Qt.ItemDataRole.UserRole).path)
        ]
        # A missing repo takes its submodules with it; listing those separately
        # would inflate the count with rows the user cannot see anyway.
        gone_ids = {id(it) for it in gone}
        gone = [it for it in gone if id(it.parent()) not in gone_ids]
        if not gone:
            return 0
        listing = "\n".join(
            f"  {c.data(0, Qt.ItemDataRole.UserRole).path}" for c in gone[:10]
        )
        if len(gone) > 10:
            listing += f"\n  ... and {len(gone) - 10} more"
        if (
            QMessageBox.question(
                self,
                "Remove missing repositories",
                f"{len(gone)} repositor(y/ies) under this folder no longer exist "
                f"on disk:\n\n{listing}\n\nRemove them from the list?",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return 0
        for c in gone:
            c.parent().removeChild(c)  # may be a submodule row, not a header child
        return len(gone)

    def _populate_repo_tree(self, repos: list[RepoEntry], roots: list[str]) -> None:
        self.repo_tree.clear()
        # Submodules hang off their parent repo, so only the outermost repos are
        # assigned to a folder group; the rest travel with their parent.
        nodes = build_repo_tree(repos)
        # Keyed by normalized path so "D:/x" and "D:\x" are one group, not two.
        grouped: dict[str, list[RepoNode]] = {self._norm(r): [] for r in roots}
        display: dict[str, str] = {self._norm(r): r for r in roots}
        ungrouped: list[RepoNode] = []
        inferred: list[str] = []
        for node in nodes:
            r = self._root_for(node.entry.path, roots)
            if r is None:
                # No explicit scan root covers this repo (e.g. added before scan
                # roots were recorded): group it under its containing directory.
                r = os.path.dirname(node.entry.path.rstrip("\\/"))
                if not r:
                    ungrouped.append(node)
                    continue
                r = os.path.normpath(r)
            key = self._norm(r)
            if key not in grouped:
                grouped[key] = []
                display[key] = r
                inferred.append(key)
            grouped[key].append(node)

        seen: set[str] = set()
        for key in [*(self._norm(x) for x in roots), *sorted(inferred)]:
            if key in seen:
                continue  # roots that normalize to the same folder
            seen.add(key)
            path = display[key]
            header = self._new_root_header(path, 0, self._is_watched(path))
            self.repo_tree.addTopLevelItem(header)
            self._add_repo_nodes(header, grouped[key])
            header.setExpanded(True)
        if ungrouped:
            other = self._ensure_other_header()
            self._add_repo_nodes(other, ungrouped)
            other.setExpanded(True)
        # Header counts include the nested rows, so they are filled in here.
        self._refresh_counts()

    def _add_repo_nodes(
        self, parent: QTreeWidgetItem, nodes: list[RepoNode]
    ) -> None:
        """Add ``nodes`` under ``parent``, submodules nested in their repo."""

        def add(node: RepoNode, target: QTreeWidgetItem) -> None:
            item = self._make_repo_item(node.entry)
            target.addChild(item)
            for child in node.children:
                add(child, item)
            item.setExpanded(True)

        for node in nodes:
            add(node, parent)

    def _collect_repos_and_roots(
        self,
    ) -> tuple[list[RepoEntry], list[str], list[str]]:
        repos: list[RepoEntry] = []
        roots: list[str] = []
        watched: list[str] = []
        seen: set[str] = set()
        for i in range(self.repo_tree.topLevelItemCount()):
            top = self.repo_tree.topLevelItem(i)
            kind = self._item_kind(top)
            if kind == "root":
                path = self._header_path(top)
                roots.append(path)
                if top.checkState(0) == Qt.CheckState.Checked:
                    watched.append(path)
            items = (
                [top, *self._repo_items_under(top)]
                if kind == "repo"
                else list(self._repo_items_under(top))
            )
            for it in items:
                entry: RepoEntry = it.data(0, Qt.ItemDataRole.UserRole)
                key = self._norm(entry.path)
                if key not in seen:
                    seen.add(key)
                    repos.append(entry)
        return repos, roots, watched

    # ---- repo actions ------------------------------------------------------
    def _on_add_repo(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select a git repository")
        if not path:
            return
        if not git_ops.is_git_repo(path):
            QMessageBox.warning(
                self, "Not a git repo", f"{path}\nis not a git repository."
            )
            return
        if self._norm(path) in self._repo_items_by_path():
            return  # already present
        self._add_repo_row(path, git_ops.repo_owner(path) or "")
        # A repo added by hand can bring submodules of its own with it.
        for sub in git_ops.find_submodules(path):
            if self._norm(sub) not in self._repo_items_by_path():
                self._add_repo_row(sub, git_ops.repo_owner(sub) or "")
        self._refresh_counts()

    def _add_repo_row(self, path: str, owner: str) -> QTreeWidgetItem:
        """Insert a repo row under its containing repo, or its folder group."""
        parent = self._parent_for_repo_path(path)
        item = self._make_repo_item(RepoEntry(path=path, owner=owner))
        parent.addChild(item)
        parent.setExpanded(True)
        return item

    def _on_remove_repo(self) -> None:
        selected = self.repo_tree.selectedItems()
        headers = [it for it in selected if self._item_kind(it) in ("root", "other")]
        # QTreeWidgetItem is unhashable; compare by id().
        selected_ids = {id(it) for it in selected}

        def has_selected_ancestor(item: QTreeWidgetItem) -> bool:
            """True when a header or parent repo above it is going away too."""
            parent = item.parent()
            while parent is not None:
                if id(parent) in selected_ids:
                    return True
                parent = parent.parent()
            return False

        # Rows removed along with an ancestor are not removed a second time; a
        # repo takes its submodules with it, since they live inside it on disk.
        repo_items = [
            it
            for it in selected
            if self._item_kind(it) == "repo" and not has_selected_ancestor(it)
        ]
        if headers:
            n = sum(sum(1 for _ in self._repo_items_under(h)) for h in headers)
            if (
                QMessageBox.question(
                    self,
                    "Remove folder group",
                    f"Remove {len(headers)} folder group(s) and their {n} repo(s)?",
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
        for it in repo_items:
            parent = it.parent()
            if parent is not None:
                parent.removeChild(it)
            else:
                self.repo_tree.takeTopLevelItem(self.repo_tree.indexOfTopLevelItem(it))
        for h in headers:
            self.repo_tree.takeTopLevelItem(self.repo_tree.indexOfTopLevelItem(h))
        self._prune_empty_other()
        self._refresh_counts()

    def _folder_of(self, item: QTreeWidgetItem | None) -> str | None:
        """Folder a tree item belongs to (the item itself if it is a header)."""
        if item is None:
            return None
        # Walk up: a submodule row sits under its parent repo, not directly
        # under the folder header it belongs to.
        while item is not None:
            if self._item_kind(item) == "root":
                return self._header_path(item)
            item = item.parent()
        return None

    def _selected_root_folder(self) -> str | None:
        """Folder to rescan: the selected row, else the focused row, else the
        only folder there is (nothing to disambiguate in that case)."""
        for it in self.repo_tree.selectedItems():
            folder = self._folder_of(it)
            if folder:
                return folder
        folder = self._folder_of(self.repo_tree.currentItem())
        if folder:
            return folder
        roots = self._current_roots()
        return roots[0] if len(roots) == 1 else None

    def _on_scan_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select a folder to scan for git repositories"
        )
        if folder:
            self._start_scan(folder)

    def _on_rescan_selected(self) -> None:
        folder = self._selected_root_folder()
        if not folder:
            QMessageBox.information(
                self,
                "Select a folder",
                "Click a folder row (or a repo inside it) to choose what to "
                "rescan.\n\nNote: the tick box controls auto-watching, not "
                "selection.",
            )
            return
        self._start_scan(folder)

    def _start_scan(self, folder: str) -> None:
        self._scanning_folder = folder
        self.scan_status.setText(f"Scanning {folder} ...")
        self.scan_btn.setEnabled(False)
        self.rescan_btn.setEnabled(False)
        worker = FunctionWorker(
            lambda f=folder: [
                (p, *git_ops.resolve_repo_meta(p)) for p in git_ops.find_git_repos(f)
            ]
        )
        worker.finished.connect(self._on_scan_done)
        worker.error.connect(self._on_scan_error)
        self._scan_worker = worker
        self._scan_thread = run_worker(worker)

    def _on_scan_done(self, results: list[tuple[str, str, bool]]) -> None:
        self.scan_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        folder = getattr(self, "_scanning_folder", None)
        header = self._ensure_root_header(folder) if folder else None
        existing = self._repo_items_by_path()

        added = backfilled = blocked = 0
        for path, owner, is_blocked in results:
            if is_blocked:
                blocked += 1
            item = existing.get(self._norm(path))
            if item is None:
                new_item = self._make_repo_item(RepoEntry(path=path, owner=owner))
                # Submodules come back after their parent (the scan sorts by
                # path), so the row they nest under already exists here.
                parent = (
                    self._containing_repo_item(path)
                    or header
                    or self._ensure_other_header()
                )
                parent.addChild(new_item)
                parent.setExpanded(True)
                existing[self._norm(path)] = new_item
                added += 1
            elif owner:
                entry: RepoEntry = item.data(0, Qt.ItemDataRole.UserRole)
                if entry.owner != owner:  # backfill / update a stale owner
                    entry.owner = owner
                    item.setData(0, Qt.ItemDataRole.UserRole, entry)
                    item.setText(0, f"{entry.display()}  -  {entry.path}")
                    backfilled += 1

        pruned = self._prune_missing_under(header)

        if header is not None:
            header.setExpanded(True)
        self._prune_empty_other()
        self._refresh_counts()

        if not results and not pruned:
            self.scan_status.setText(f"No git repositories found in {folder}.")
            return
        msg = f"Found {len(results)} repo(s) in {folder}; added {added} new"
        if backfilled:
            msg += f", updated {backfilled} owner(s)"
        if pruned:
            msg += f", removed {pruned} missing"
        if blocked:
            msg += (
                f". {blocked} blocked by git ownership check - run:  "
                "git config --global --add safe.directory '*'"
            )
        else:
            msg += "."
        self.scan_status.setText(msg)

    def _on_scan_error(self, message: str) -> None:
        self.scan_btn.setEnabled(True)
        self.rescan_btn.setEnabled(True)
        self.scan_status.setText(f"Scan failed: {message}")

    def _on_open_config(self) -> None:
        folder = config_path().parent
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ---- update service ----------------------------------------------------
    def _refresh_update_source(self) -> None:
        """Show where this installation looks for updates, and why.

        "It is checking the wrong server" and "it is not checking at all" look
        identical from the outside otherwise -- the menu item is simply absent
        -- which leaves no way to tell a missing address from a typo in one.
        """
        if not UPDATES_SUPPORTED:
            self.update_source.setText(NO_UPDATES_NOTE)
            self.update_source.setStyleSheet("color: #888;")
            return

        config = UpdateConfig.load()
        if config.problem:
            self.update_source.setText(config.problem)
            self.update_source.setStyleSheet("color: #c0392b;")
            return

        reason = config.unavailable_reason()
        if config.base_url:
            text = config.base_url
            if config.origin:
                text += f"  (from {config.origin})"
            if reason:
                # Configured, but something else disables updating: a source
                # checkout, or a build packaged without the verifier.
                text += f"\nUpdates are off: {reason}"
        else:
            text = reason or "not configured"

        self.update_source.setText(text)
        self.update_source.setStyleSheet("color: #888;")

    def _on_edit_update_config(self) -> None:
        """Open `update.json`, creating an inert template if it is absent.

        The template overrides nothing, so this cannot change where updates
        come from -- it only gives somebody a file to edit. Without it the
        answer to "where do I change the URL" was "create this JSON file by
        hand in a directory the application never mentions".
        """
        path = ensure_update_config()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        self._refresh_update_source()

    def _on_trust_all(self) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Trust all git repositories?")
        box.setText("Fix repositories blocked by git's ownership check?")
        box.setInformativeText(
            "This runs:\n"
            "    git config --global --add safe.directory '*'\n\n"
            "It tells Git to trust every repository on this machine, regardless "
            "of which Windows account owns the folder. This clears the "
            "'dubious ownership' errors that block repos copied or restored from "
            "another account.\n\n"
            "Security note: only do this on a machine you trust. It disables a "
            "safeguard meant to stop untrusted repositories (e.g. on shared or "
            "network drives) from running code via their git config or hooks.\n\n"
            "This changes your global git configuration. Proceed?"
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return

        result = git_ops.trust_all_repositories()
        if result.ok:
            if result.stdout.strip() == "already trusted":
                self.scan_status.setText("All repositories are already trusted.")
            else:
                self.scan_status.setText(
                    "Done. All repositories are now trusted - re-scan the folder "
                    "to fill in owners for the previously blocked repos."
                )
        else:
            QMessageBox.critical(
                self,
                "Could not update git config",
                result.stderr.strip() or "git config failed.",
            )

    # ---- connection test / model listing -----------------------------------
    def _on_test_connection(self) -> None:
        """List the selected provider's models, off the GUI thread.

        Applies the pending edits first: the address, endpoint and provider may
        all have been typed a moment ago and not yet been through the debounced
        save, and testing the previous ones would report on a connection the
        user is no longer asking about.
        """
        self._apply_to_settings()
        self.conn_status.setText("Connecting...")
        self.test_btn.setEnabled(False)

        try:
            client = build_client(self.settings)
        except LLMError as exc:
            # Missing key or endpoint: a configuration answer, not a network
            # one, so it is reported without a round trip.
            self.test_btn.setEnabled(True)
            self.conn_status.setText(str(exc))
            return

        worker = FunctionWorker(client.list_models)
        worker.finished.connect(self._on_models_loaded)
        worker.error.connect(self._on_models_error)
        # Retain strong references so PyQt6 GC can't collect the worker/thread
        # mid-flight (which would leave the status stuck on "Connecting...").
        self._conn_worker = worker
        self._thread = run_worker(worker)

    def _on_models_loaded(self, models: list[ModelInfo]) -> None:
        self.test_btn.setEnabled(True)
        if not models:
            provider = providers.get(self.settings.provider)
            self.conn_status.setText(
                "Connected, but no models are loaded in LM Studio."
                if provider.key == "lmstudio"
                else f"Connected, but {provider.label} returned no models."
            )
            return
        previous = self.model_combo.currentData()
        self.model_combo.clear()
        self._model_contexts = {}
        for m in models:
            self.model_combo.addItem(m.label(), m.id)
            if m.max_context_length:
                self._model_contexts[m.id] = m.max_context_length
        # Restore previous selection if still present.
        if previous:
            idx = self.model_combo.findData(previous)
            if idx >= 0:
                self.model_combo.setCurrentIndex(idx)
        self.conn_status.setText(f"Connected. {len(models)} model(s) available.")
        self._update_budget_label()

    def _on_models_error(self, message: str) -> None:
        self.test_btn.setEnabled(True)
        self.conn_status.setText(f"Failed: {message}")

    # ---- effective-budget readout ------------------------------------------
    def _update_budget_label(self) -> None:
        """Show how the context window splits into diff input vs. reserved output.

        Mirrors CommitGenerator: output = window * margin, diff budget = the rest,
        so input + output always fits the window. The window is the configured
        size (clamped to the detected max) or the detected max when size is 0.
        """
        needed = ("budget_label", "ctx_size_spin", "margin_spin")
        if not all(hasattr(self, n) for n in needed):
            return  # tabs not fully built yet
        model_id = self.model_combo.currentData() or self.model_combo.currentText()
        detected = self._model_contexts.get(model_id)
        size = self.ctx_size_spin.value()
        margin = self.margin_spin.value()

        clamped = False
        if size > 0:
            source = "configured"
            if detected and size > detected:
                window, clamped = detected, True
            else:
                window = size
        elif detected:
            source = "auto"
            window = detected
        else:
            self.budget_label.setText(
                "Set a Context window size above, "
                "or test the connection to auto-detect it."
            )
            return

        out = reserved_output(window, margin)
        diff_budget = input_budget(window, out)
        note = ", clamped to model max" if clamped else ""
        text = (
            f"~{diff_budget:,} tokens for the diff  "
            f"(window {window:,} [{source}{note}] "
            f"- {out:,} output @ {int(margin * 100)}%)"
        )
        # Parallel requests share the model's context window, so each concurrent
        # chunk gets a fraction of it.
        requested = self.parallel_spin.value()
        affordable = max(1, window // MIN_PARALLEL_CONTEXT)
        workers = min(requested, affordable)
        if workers > 1:
            per_req = window // workers
            text += (
                f"\nParallel: {workers} request(s) share the window "
                f"-> ~{per_req:,} tokens each per chunk"
            )
            if workers < requested:
                text += f"  (capped from {requested}: window too small)"
        self.budget_label.setText(text)
