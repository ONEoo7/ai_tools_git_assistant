"""Settings dialog: connection, model picker, repo manager, template, advanced."""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import __version__, git_ops
from git_assistant.commit_generator import MIN_PARALLEL_CONTEXT
from git_assistant.config import RepoEntry, Settings, config_path
from git_assistant.lmstudio_client import LMStudioClient, ModelInfo
from git_assistant.prompts import DEFAULT_TEMPLATE
from git_assistant.ui.preview_dialog import CommitPanel
from git_assistant.ui.tags_panel import TagsPanel
from git_assistant.updating.client import (
    UpdateConfig,
    ensure_update_config,
    update_config_path,
)
from git_assistant.tokenizer import input_budget, reserved_output
from git_assistant.ui.workers import FunctionWorker, run_worker

# Shown in place of the online version until an update check reports one.
UNKNOWN_VERSION = "?"


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._thread = None
        self._conn_worker = None
        self._scan_thread = None
        self._scan_worker = None
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
        tabs.addTab(self._build_template_tab(), "Template")
        tabs.addTab(self._build_advanced_tab(), "Advanced")

        # No Save/Cancel: edits are written to disk automatically (debounced).
        open_cfg_btn = QPushButton("Open config folder")
        open_cfg_btn.setToolTip(str(config_path()))
        open_cfg_btn.clicked.connect(self._on_open_config)

        self.saved_hint = QLabel("Changes are saved automatically")
        self.saved_hint.setStyleSheet("color: #888;")

        # Bottom-left version indicator: "<installed> -> <available>".
        # Placeholders for now; wire set_online_version() to the updater later.
        self.version_current = QLabel(f"v{__version__}")
        self.version_current.setToolTip("Installed version")
        self.version_arrow = QLabel("->")
        self.version_online = QLabel(UNKNOWN_VERSION)
        self.version_online.setToolTip("Latest available version")
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
        layout.addWidget(tabs)
        layout.addLayout(bottom)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._autosave)

        self._load_into_widgets()
        self._ready = True
        self._connect_autosave()

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

    def set_online_version(self, version: str | None) -> None:
        """Show the latest available version (hook for the updater).

        Pass None when the check failed or has not run yet.
        """
        self.version_online.setText(version or UNKNOWN_VERSION)
        newer = bool(version) and version != __version__
        # Highlight only when an actual update is available.
        self.version_online.setStyleSheet(
            "color: #4caf50; font-weight: bold;" if newer else "color: #888;"
        )

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
        form = QFormLayout(w)

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

        form.addRow("LM Studio IP:", self.ip_edit)
        form.addRow("Port:", self.port_spin)
        form.addRow("", self.test_btn)
        form.addRow("Model:", self.model_combo)
        form.addRow("Parallel requests:", self.parallel_spin)
        form.addRow("Status:", self.conn_status)
        form.addRow("Effective budget:", self.budget_label)
        return w

    def _build_repos_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(
            QLabel(
                "Repositories in the tray menu, grouped by scanned folder.\n"
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
                "Prompt template. Placeholders: {branch}, {diffstat}, {diff}"
            )
        )
        self.template_edit = QPlainTextEdit()
        self.template_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.template_edit)

        reset_btn = QPushButton("Reset to default")
        reset_btn.clicked.connect(
            lambda: self.template_edit.setPlainText(DEFAULT_TEMPLATE)
        )
        layout.addWidget(reset_btn, alignment=Qt.AlignmentFlag.AlignLeft)
        return w

    def _build_advanced_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        form_container = QWidget()
        form = QFormLayout(form_container)

        self.diff_mode_combo = QComboBox()
        self.diff_mode_combo.addItem("Staged changes (git diff --cached)", "cached")
        self.diff_mode_combo.addItem("All uncommitted (git diff HEAD)", "working")

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
        edit_update_btn = QPushButton("Edit...")
        edit_update_btn.setToolTip(str(update_config_path()))
        edit_update_btn.clicked.connect(self._on_edit_update_config)
        update_row.addWidget(self.update_source, 1)
        update_row.addWidget(edit_update_btn)

        form.addRow("Diff source:", self.diff_mode_combo)
        form.addRow("Context window size:", self.ctx_size_spin)
        form.addRow("Output reserve (margin):", self.margin_spin)
        form.addRow("Ignore globs:", self.ignore_edit)
        form.addRow("Update service:", update_row)

        # Keep the Connection tab's effective-budget readout in sync.
        self.ctx_size_spin.valueChanged.connect(self._update_budget_label)
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
        if s.selected_model:
            self.model_combo.addItem(s.selected_model, s.selected_model)
            self.model_combo.setCurrentIndex(0)

        self._populate_repo_tree(s.repos, s.scan_roots)

        self.template_edit.setPlainText(s.prompt_template or DEFAULT_TEMPLATE)

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
        self.model_combo.currentIndexChanged.connect(self._schedule_save)
        self.diff_mode_combo.currentIndexChanged.connect(self._schedule_save)
        self.ctx_size_spin.valueChanged.connect(self._schedule_save)
        self.margin_spin.valueChanged.connect(self._schedule_save)
        self.ignore_edit.textChanged.connect(self._schedule_save)
        self.template_edit.textChanged.connect(self._schedule_save)
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
        s.selected_model = self.model_combo.currentData() or self.model_combo.currentText()
        s.parallel_calls = self.parallel_spin.value()

        repos, roots, watched = self._collect_repos_and_roots()
        s.repos = repos
        s.scan_roots = roots
        s.watched_roots = watched
        if s.active_repo not in {r.path for r in repos}:
            s.active_repo = repos[0].path if repos else ""

        s.prompt_template = self.template_edit.toPlainText() or DEFAULT_TEMPLATE
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

    def _all_repo_items(self):
        tree = self.repo_tree
        for i in range(tree.topLevelItemCount()):
            top = tree.topLevelItem(i)
            if self._item_kind(top) == "repo":
                yield top
            else:
                for j in range(top.childCount()):
                    yield top.child(j)

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
            n = top.childCount()
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
            header.child(j)
            for j in range(header.childCount())
            if not git_ops.has_git_dir(
                header.child(j).data(0, Qt.ItemDataRole.UserRole).path
            )
        ]
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
            header.removeChild(c)
        return len(gone)

    def _populate_repo_tree(self, repos: list[RepoEntry], roots: list[str]) -> None:
        self.repo_tree.clear()
        # Keyed by normalized path so "D:/x" and "D:\x" are one group, not two.
        grouped: dict[str, list[RepoEntry]] = {self._norm(r): [] for r in roots}
        display: dict[str, str] = {self._norm(r): r for r in roots}
        ungrouped: list[RepoEntry] = []
        inferred: list[str] = []
        for entry in repos:
            r = self._root_for(entry.path, roots)
            if r is None:
                # No explicit scan root covers this repo (e.g. added before scan
                # roots were recorded): group it under its containing directory.
                r = os.path.dirname(entry.path.rstrip("\\/"))
                if not r:
                    ungrouped.append(entry)
                    continue
                r = os.path.normpath(r)
            key = self._norm(r)
            if key not in grouped:
                grouped[key] = []
                display[key] = r
                inferred.append(key)
            grouped[key].append(entry)

        seen: set[str] = set()
        for key in [*(self._norm(x) for x in roots), *sorted(inferred)]:
            if key in seen:
                continue  # roots that normalize to the same folder
            seen.add(key)
            path = display[key]
            header = self._new_root_header(
                path, len(grouped[key]), self._is_watched(path)
            )
            self.repo_tree.addTopLevelItem(header)
            for entry in grouped[key]:
                header.addChild(self._make_repo_item(entry))
            header.setExpanded(True)
        if ungrouped:
            other = self._ensure_other_header()
            for entry in ungrouped:
                other.addChild(self._make_repo_item(entry))
            other.setExpanded(True)
        self._refresh_counts()

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
                [top] if kind == "repo" else [top.child(j) for j in range(top.childCount())]
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
        owner = git_ops.repo_owner(path) or ""
        parent = self._header_for_repo_path(path)
        parent.addChild(self._make_repo_item(RepoEntry(path=path, owner=owner)))
        parent.setExpanded(True)
        self._refresh_counts()

    def _on_remove_repo(self) -> None:
        selected = self.repo_tree.selectedItems()
        headers = [it for it in selected if self._item_kind(it) in ("root", "other")]
        # QTreeWidgetItem is unhashable; compare by id().
        header_ids = {id(it) for it in headers}
        # Repo items whose header is also being removed are dropped with it.
        repo_items = [
            it
            for it in selected
            if self._item_kind(it) == "repo" and id(it.parent()) not in header_ids
        ]
        if headers:
            n = sum(h.childCount() for h in headers)
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
        kind = self._item_kind(item)
        if kind == "root":
            return self._header_path(item)
        if kind == "repo":
            parent = item.parent()
            if parent is not None and self._item_kind(parent) == "root":
                return self._header_path(parent)
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
                (header or self._ensure_other_header()).addChild(new_item)
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
        ip = self.ip_edit.text().strip() or "127.0.0.1"
        port = self.port_spin.value()
        base_url = f"http://{ip}:{port}"
        self.conn_status.setText("Connecting...")
        self.test_btn.setEnabled(False)

        client = LMStudioClient(base_url)
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
            self.conn_status.setText("Connected, but no models are loaded in LM Studio.")
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
                "Set a Context window size in Advanced, "
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
