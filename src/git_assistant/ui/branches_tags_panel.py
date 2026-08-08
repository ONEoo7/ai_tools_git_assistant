"""Branches & Tags: the two things this application does to refs.

Together because they are the same question asked twice -- what this repository
is called at this point, and what to call the next piece of work -- and because
both are answered against one repository, with one picker between them.

What a new branch is *named* is not decided here. It comes from the repository's
own settings, so a project whose branches are `dev/rem/<user>/<thing>` says so
once in a file it commits, rather than in every person's head. See
``git_assistant.repo_config``.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import git_ops, repo_config, versioning
from git_assistant.config import Settings
from git_assistant.ui.preview_dialog import SECTION_GAP
from git_assistant.ui.repo_picker import RepoPicker
from git_assistant.ui.workers import FunctionWorker, run_worker

CUSTOM = "custom"
MUTED_COLOUR = "color: #888;"
INFO_COLOUR = "color: #8ab;"


class BranchesTagsPanel(QWidget):
    """Create branches from the project's naming rules, and release tags."""

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._thread = None
        self._worker = None
        self._current = None  # versioning.Version | None
        #: The selected repository's settings, resolved when the repository
        #: changes and held: the branch-name preview asks for them on every
        #: keystroke, and that is a decision about this screen rather than a
        #: cache every other caller of `resolve` has to reason about.
        self._config = repo_config.RepoSettings()
        self._git_user = ""

        # The same picker the Generate tab uses, so both tabs behave identically.
        self.repo_picker = RepoPicker(settings)
        self.repo_picker.repoChanged.connect(self._on_repo_changed)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(SECTION_GAP, 0, 0, 0)

        self.current_label = QLabel("Current version: -")
        font = self.current_label.font()
        font.setBold(True)
        self.current_label.setFont(font)
        layout.addWidget(self.current_label)

        # ---- bump choice ---------------------------------------------------
        box = QGroupBox("New version")
        box_layout = QVBoxLayout(box)
        self.radios: dict[str, QRadioButton] = {}
        for part in versioning.PARTS:
            radio = QRadioButton(part)
            radio.toggled.connect(self._on_bump_toggled)
            self.radios[part] = radio
            box_layout.addWidget(radio)
        custom = QRadioButton("custom")
        custom.toggled.connect(self._on_bump_toggled)
        self.radios[CUSTOM] = custom
        box_layout.addWidget(custom)

        tag_row = QHBoxLayout()
        tag_row.addWidget(QLabel("Tag:"))
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("v1.2.3")
        tag_row.addWidget(self.tag_edit, 1)
        box_layout.addLayout(tag_row)

        msg_row = QHBoxLayout()
        msg_row.addWidget(QLabel("Message:"))
        self.msg_edit = QLineEdit()
        self.msg_edit.setPlaceholderText(
            "Optional - creates an annotated tag when set"
        )
        msg_row.addWidget(self.msg_edit, 1)
        box_layout.addLayout(msg_row)
        layout.addWidget(box)

        # Set only now: toggling fires _update_proposal, which needs tag_edit.
        self.radios["patch"].setChecked(True)

        # ---- actions --------------------------------------------------------
        btn_row = QHBoxLayout()
        self.create_btn = QPushButton("Create tag")
        self.create_btn.clicked.connect(self._on_create)
        self.push_btn = QPushButton("Push tag")
        self.push_btn.setToolTip("Publish the tag to the remote (asks first).")
        self.push_btn.clicked.connect(self._on_push)
        self.delete_btn = QPushButton("Delete tag")
        self.delete_btn.setToolTip(
            "Delete a local tag that has not been pushed yet.\n"
            "Tags already on the remote are refused - removing a published tag "
            "breaks anyone who fetched it."
        )
        self.delete_btn.clicked.connect(self._on_delete)
        btn_row.addWidget(self.create_btn)
        btn_row.addWidget(self.push_btn)
        btn_row.addWidget(self.delete_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #8ab;")
        layout.addWidget(self.status)

        layout.addWidget(
            QLabel("Existing tags (newest first) - click one to push it:")
        )
        self.tag_list = QTreeWidget()
        self.tag_list.setHeaderLabels(["Tag", "Created"])
        self.tag_list.setRootIsDecorated(False)
        self.tag_list.setColumnWidth(0, 220)
        self.tag_list.itemClicked.connect(self._on_tag_clicked)
        layout.addWidget(self.tag_list, 1)

        # Repository on the left, then the two halves of the tab - the same
        # shape as every other repo-driven tab.
        picker_pane = QWidget()
        picker_box = QVBoxLayout(picker_pane)
        picker_box.setContentsMargins(0, 0, SECTION_GAP, 0)
        picker_box.addWidget(self.repo_picker, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(picker_pane)
        splitter.addWidget(self._build_branches_pane())
        splitter.addWidget(content)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 3)
        splitter.setSizes([200, 480, 480])

        # Default margins, exactly as the Generate Commit Message tab uses, so
        # every tab keeps the same gap between its border and its content.
        outer = QVBoxLayout(self)
        outer.addWidget(splitter)

        self.refresh()

    # ---- branches ------------------------------------------------------------
    def _build_branches_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(SECTION_GAP, 0, SECTION_GAP, 0)

        header = QLabel("Branches")
        font = header.font()
        font.setBold(True)
        header.setFont(font)
        box.addWidget(header)

        # ---- what the project calls a new branch ----------------------------
        new_box = QGroupBox("New branch")
        new_layout = QVBoxLayout(new_box)
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.branch_name_edit = QLineEdit()
        self.branch_name_edit.setPlaceholderText("what you are about to work on")
        self.branch_name_edit.textChanged.connect(self._update_branch_preview)
        self.branch_name_edit.returnPressed.connect(self._on_create_branch)
        name_row.addWidget(self.branch_name_edit, 1)
        new_layout.addLayout(name_row)

        # The whole name, before it is created rather than after. The pattern
        # comes from the repository, and a pattern nobody can see is a rule
        # nobody can follow.
        self.branch_preview = QLabel("")
        self.branch_preview.setWordWrap(True)
        self.branch_preview.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        new_layout.addWidget(self.branch_preview)

        self.branch_pattern_note = QLabel("")
        self.branch_pattern_note.setWordWrap(True)
        self.branch_pattern_note.setStyleSheet(MUTED_COLOUR)
        new_layout.addWidget(self.branch_pattern_note)

        self.create_branch_btn = QPushButton("Create branch")
        self.create_branch_btn.setToolTip(
            "Create it from the current commit and switch to it."
        )
        self.create_branch_btn.clicked.connect(self._on_create_branch)
        new_layout.addWidget(self.create_branch_btn)
        box.addWidget(new_box)

        # ---- what is already here -------------------------------------------
        self.branch_list = QTreeWidget()
        self.branch_list.setHeaderLabels(["Branch", "Tracking", "Last commit"])
        self.branch_list.setRootIsDecorated(False)
        self.branch_list.setColumnWidth(0, 200)
        self.branch_list.setColumnWidth(1, 110)
        self.branch_list.itemSelectionChanged.connect(self._on_branch_selection)
        self.branch_list.itemDoubleClicked.connect(lambda *_: self._on_switch_branch())
        box.addWidget(self.branch_list, 1)

        row = QHBoxLayout()
        self.switch_btn = QPushButton("Switch")
        self.switch_btn.setToolTip("Check out the selected branch.")
        self.switch_btn.clicked.connect(self._on_switch_branch)
        self.push_branch_btn = QPushButton("Push")
        self.push_branch_btn.clicked.connect(self._on_push_branch)
        self.delete_branch_btn = QPushButton("Delete...")
        self.delete_branch_btn.setToolTip(
            "Delete the selected branch. Git refuses one whose commits are on "
            "no other branch; deleting it anyway has to be confirmed."
        )
        self.delete_branch_btn.clicked.connect(self._on_delete_branch)
        self.fetch_btn = QPushButton("Fetch")
        self.fetch_btn.clicked.connect(self._on_fetch)
        for button in (
            self.switch_btn,
            self.push_branch_btn,
            self.delete_branch_btn,
            self.fetch_btn,
        ):
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)

        self.branch_status = QLabel("")
        self.branch_status.setWordWrap(True)
        self.branch_status.setStyleSheet(INFO_COLOUR)
        box.addWidget(self.branch_status)
        return pane

    def _selected_branch(self) -> git_ops.BranchInfo | None:
        items = self.branch_list.selectedItems()
        return items[0].data(0, Qt.ItemDataRole.UserRole) if items else None

    def _on_branch_selection(self) -> None:
        chosen = self._selected_branch()
        self.switch_btn.setEnabled(chosen is not None and not chosen.current)
        self.push_branch_btn.setEnabled(chosen is not None)
        self.delete_branch_btn.setEnabled(chosen is not None and not chosen.current)

    def _reload_branches(self) -> None:
        repo = self._repo_path()
        self.branch_list.clear()
        if not repo:
            self._on_branch_selection()
            return
        for info in git_ops.list_branch_info(repo):
            item = QTreeWidgetItem(
                [info.name, info.tracking_label(), info.subject]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, info)
            if info.current:
                item.setText(0, f"* {info.name}")
                font = item.font(0)
                font.setBold(True)
                item.setFont(0, font)
            self.branch_list.addTopLevelItem(item)
        self._on_branch_selection()

    def _reload_config(self) -> None:
        """Re-read this repository's rules, and who git thinks we are."""
        repo = self._repo_path()
        self._config = repo_config.resolve(repo)
        # `{user}` blank in the config means "ask git", and this is the caller
        # that can: repo_config runs none of it.
        self._git_user = git_ops.get_identity(repo)[0] if repo else ""
        self._update_branch_preview()

    def _full_branch_name(self) -> str:
        return self._config.branch.render(
            self.branch_name_edit.text(), user=self._git_user
        )

    def _update_branch_preview(self, _text: str = "") -> None:
        pattern = self._config.branch.pattern
        typed = self.branch_name_edit.text().strip()
        full = self._full_branch_name()
        self.branch_preview.setText(f"Will create:  {full}" if full else "")
        source = (
            "this repository's settings"
            if repo_config.has_repo_config(self._repo_path())
            else f"your defaults ({repo_config.DEFAULTS_FILE})"
        )
        self.branch_pattern_note.setText(f"Pattern {pattern} - from {source}.")
        self.create_branch_btn.setEnabled(bool(full) and bool(typed))

    # ---- state -------------------------------------------------------------
    def _repo_path(self) -> str:
        return self.repo_picker.current_path()

    def refresh(self) -> None:
        """Reload repositories, branches, tags and the proposed version."""
        self.repo_picker.refresh()
        self._reload_config()
        self._reload_branches()
        self._reload_tags()

    #: The name every repo-driven tab answers to, so the window can ask a tab
    #: whether it is one instead of knowing where it sits.
    refresh_repos = refresh

    def _reload_tags(self) -> None:
        repo = self._repo_path()
        self.tag_list.clear()
        if not repo:
            self.current_label.setText("Current version: (no repository)")
            self._set_enabled(False)
            return
        self._set_enabled(True)
        dated = git_ops.list_tags_with_dates(repo)
        for name, created in dated:
            self.tag_list.addTopLevelItem(QTreeWidgetItem([name, created]))
        self._current = versioning.latest_version([name for name, _ in dated])
        if self._current is None:
            self.current_label.setText(
                "Current version: none yet - first tag proposed"
            )
        else:
            self.current_label.setText(f"Current version: {self._current}")
        self._update_proposal()

    def _set_enabled(self, on: bool) -> None:
        self.create_btn.setEnabled(on)
        self.push_btn.setEnabled(on)
        self.delete_btn.setEnabled(on)
        self.tag_edit.setEnabled(on)

    def _selected_part(self) -> str:
        for part, radio in self.radios.items():
            if radio.isChecked():
                return part
        return "patch"

    def _update_proposal(self) -> None:
        """Fill the tag box from the chosen bump (custom leaves it editable)."""
        part = self._selected_part()
        self.tag_edit.setReadOnly(part != CUSTOM)
        if part == CUSTOM:
            return
        self.tag_edit.setText(versioning.proposals(self._current)[part])

    def _on_bump_toggled(self, checked: bool) -> None:
        if checked:  # ignore the untoggle half of the pair
            self._update_proposal()

    def _on_tag_clicked(self, item, _column: int = 0) -> None:
        # Switch to custom so the proposal does not overwrite the picked tag.
        self.radios[CUSTOM].setChecked(True)
        self.tag_edit.setText(item.text(0))

    def _on_repo_changed(self, path: str = "") -> None:
        """React to the picker's selection (it already updated the settings)."""
        if not path:
            return
        self.status.setText("")
        self.branch_status.setText("")
        self._reload_config()
        self._reload_branches()
        self._reload_tags()

    # ---- branch actions ------------------------------------------------------
    def _on_create_branch(self) -> None:
        repo, name = self._repo_path(), self._full_branch_name()
        if not repo or not name:
            return
        if git_ops.branch_exists(repo, name):
            QMessageBox.warning(
                self, "Branch exists", f"'{name}' already exists in this repository."
            )
            return
        result = git_ops.create_branch(repo, name)
        if not result.ok:
            self.branch_status.setText("Could not create the branch.")
            QMessageBox.critical(
                self,
                "Could not create the branch",
                result.stderr.strip() or f"git refused to create '{name}'.",
            )
            return
        self.branch_name_edit.clear()
        self._reload_branches()
        self.branch_status.setText(f"Created and switched to '{name}'.")

    def _on_switch_branch(self) -> None:
        repo, chosen = self._repo_path(), self._selected_branch()
        if not repo or chosen is None or chosen.current:
            return
        result = git_ops.switch_branch(repo, chosen.name)
        if not result.ok:
            # git refuses when carrying the local changes over would overwrite
            # something. That refusal is the answer, not something to work past.
            QMessageBox.warning(
                self,
                "Could not switch",
                result.stderr.strip() or f"git refused to switch to '{chosen.name}'.",
            )
            return
        self._reload_branches()
        self._reload_tags()  # the version this repository is at may differ here
        self.branch_status.setText(f"On '{chosen.name}'.")

    def _on_delete_branch(self) -> None:
        repo, chosen = self._repo_path(), self._selected_branch()
        if not repo or chosen is None or chosen.current:
            return
        if (
            QMessageBox.question(
                self,
                "Delete branch",
                f"Delete the local branch '{chosen.name}'?\n\n"
                f"Repository: {repo}\n\n"
                "The remote is not touched.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        result = git_ops.delete_branch(repo, chosen.name)
        if not result.ok:
            # The one case worth a second question: git only refuses when the
            # commits are on no other branch, which means deleting it is the
            # last copy going.
            if (
                QMessageBox.question(
                    self,
                    "Delete unmerged branch",
                    f"git refused:\n\n{result.stderr.strip()}\n\n"
                    f"'{chosen.name}' holds commits that are on no other "
                    "branch. Delete it anyway and lose them?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                    QMessageBox.StandardButton.Cancel,
                )
                != QMessageBox.StandardButton.Yes
            ):
                self.branch_status.setText(f"'{chosen.name}' was not deleted.")
                return
            result = git_ops.delete_branch(repo, chosen.name, force=True)
            if not result.ok:
                QMessageBox.critical(
                    self, "Could not delete", result.stderr.strip() or "git refused."
                )
                return

        self.branch_status.setText(f"Deleted '{chosen.name}'.")
        self._reload_branches()
        if chosen.upstream and QMessageBox.question(
            self,
            "Delete it on the remote too?",
            f"'{chosen.name}' is also on {chosen.upstream.split('/')[0]}.\n\n"
            "Delete it there as well? Anyone who has it will keep their copy.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) == QMessageBox.StandardButton.Yes:
            self._run_off_thread(
                lambda r=repo, n=chosen.name: git_ops.delete_remote_branch(r, n),
                busy="Deleting it on the remote...",
                done=f"Deleted '{chosen.name}' locally and on the remote.",
                failed="The branch is gone locally; the remote still has it.",
            )

    def _on_push_branch(self) -> None:
        repo, chosen = self._repo_path(), self._selected_branch()
        if not repo or chosen is None:
            return
        upstream = self._config.branch.push_sets_upstream
        self._run_off_thread(
            lambda r=repo, n=chosen.name, u=upstream: git_ops.push_branch(
                r, n, set_upstream=u
            ),
            busy=f"Pushing '{chosen.name}'...",
            done=f"Pushed '{chosen.name}'.",
            failed="Push failed.",
        )

    def _on_fetch(self) -> None:
        repo = self._repo_path()
        if not repo:
            return
        rules = self._config.fetch
        depth = rules.effective_depth()
        self._run_off_thread(
            lambda r=repo, d=depth, p=rules.prune, t=rules.tags: git_ops.fetch(
                r, depth=d, prune=p, tags=t
            ),
            busy="Fetching..." if depth is None else f"Fetching (depth {depth})...",
            done="Fetched." if depth is None else f"Fetched, {depth} commit(s) deep.",
            failed="Fetch failed.",
        )

    def _run_off_thread(self, work, *, busy: str, done: str, failed: str) -> None:
        """Run one git command that talks to a remote, without freezing the window."""
        for button in self._remote_buttons():
            button.setEnabled(False)
        self.branch_status.setText(busy)
        worker = FunctionWorker(work)
        worker.finished.connect(
            lambda result: self._on_remote_done(result, done, failed)
        )
        worker.error.connect(lambda message: self._on_remote_failed(message, failed))
        self._worker = worker
        self._thread = run_worker(worker)

    def _remote_buttons(self) -> tuple:
        return (self.push_branch_btn, self.fetch_btn, self.delete_branch_btn)

    def _on_remote_done(self, result, done: str, failed: str) -> None:
        for button in self._remote_buttons():
            button.setEnabled(True)
        detail = (result.stderr.strip() or result.stdout.strip() or "").strip()
        if result.ok:
            self.branch_status.setText(done)
            self._reload_branches()
        else:
            self.branch_status.setText(failed)
            QMessageBox.critical(self, failed, detail or "git refused.")
        self._on_branch_selection()

    def _on_remote_failed(self, message: str, failed: str) -> None:
        for button in self._remote_buttons():
            button.setEnabled(True)
        self.branch_status.setText(failed)
        QMessageBox.critical(self, failed, message)
        self._on_branch_selection()

    # ---- actions -----------------------------------------------------------
    def _on_create(self) -> None:
        repo, name = self._repo_path(), self.tag_edit.text().strip()
        if not name:
            self.status.setText("Enter a tag name.")
            return
        if git_ops.tag_exists(repo, name):
            QMessageBox.warning(
                self, "Tag exists", f"'{name}' already exists in this repository."
            )
            return

        message = self.msg_edit.text().strip()
        kind = "annotated" if message else "lightweight"
        if (
            QMessageBox.question(
                self,
                "Create tag",
                f"Create {kind} tag '{name}' at the current HEAD?\n\n"
                f"Repository: {repo}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Yes,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        result = git_ops.create_tag(repo, name, message)
        if result.ok:
            self.status.setText(f"Created {kind} tag '{name}' (not pushed yet).")
            self._reload_tags()
            self.tag_edit.setText(name)
        else:
            QMessageBox.critical(
                self, "Could not create tag", result.stderr.strip() or "git tag failed."
            )

    def _on_push(self) -> None:
        repo, name = self._repo_path(), self.tag_edit.text().strip()
        if not name:
            self.status.setText("Enter or select a tag to push.")
            return
        if not git_ops.tag_exists(repo, name):
            QMessageBox.warning(
                self, "No such tag", f"'{name}' does not exist locally - create it first."
            )
            return

        if (
            QMessageBox.question(
                self,
                "Confirm push",
                f"Push tag '{name}' to 'origin'?\n\n"
                f"Repository: {repo}\n\n"
                "This publishes the tag to the remote.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return

        self.push_btn.setEnabled(False)
        self.status.setText(f"Pushing tag '{name}'...")
        worker = FunctionWorker(lambda r=repo, n=name: git_ops.push_tag(r, n))
        worker.finished.connect(lambda res, n=name: self._on_push_done(res, n))
        worker.error.connect(self._on_push_error)
        self._worker = worker
        self._thread = run_worker(worker)

    def _on_push_done(self, result, name: str) -> None:
        self.push_btn.setEnabled(True)
        detail = (result.stderr.strip() or result.stdout.strip() or "").strip()
        if result.ok:
            self.status.setText(f"Pushed tag '{name}'.")
            QMessageBox.information(self, "Tag pushed", detail or f"Pushed '{name}'.")
        else:
            self.status.setText("Push failed.")
            QMessageBox.critical(self, "Push failed", detail or "git push failed.")

    def _on_push_error(self, message: str) -> None:
        self.push_btn.setEnabled(True)
        self.status.setText("Push failed.")
        QMessageBox.critical(self, "Push failed", message)

    # ---- delete ------------------------------------------------------------
    def _on_delete(self) -> None:
        repo, name = self._repo_path(), self.tag_edit.text().strip()
        if not name:
            self.status.setText("Enter or select a tag to delete.")
            return
        if not git_ops.tag_exists(repo, name):
            QMessageBox.warning(
                self, "No such tag", f"'{name}' does not exist in this repository."
            )
            return

        # Whether it was pushed decides if deleting is safe, and answering that
        # means asking the remote - so it runs off the UI thread.
        self.delete_btn.setEnabled(False)
        self.status.setText(f"Checking whether '{name}' was pushed...")
        worker = FunctionWorker(
            lambda r=repo, n=name: git_ops.remote_tag_exists(r, n)
        )
        worker.finished.connect(lambda pushed, n=name: self._on_delete_checked(pushed, n))
        worker.error.connect(lambda m, n=name: self._on_delete_checked(None, n))
        self._worker = worker
        self._thread = run_worker(worker)

    def _on_delete_checked(self, pushed: bool | None, name: str) -> None:
        self.delete_btn.setEnabled(True)
        repo = self._repo_path()

        if pushed is True:
            self.status.setText("")
            QMessageBox.warning(
                self,
                "Tag already pushed",
                f"'{name}' has already been pushed to the remote, so it is not "
                f"deleted here.\n\nRemoving a published tag breaks anyone who "
                f"already fetched it. If you are certain, delete it deliberately "
                f"with:\n\n    git tag -d {name}\n"
                f"    git push origin :refs/tags/{name}",
            )
            return

        if pushed is None:
            # Cannot confirm; say so rather than guess in either direction.
            confirm = QMessageBox.question(
                self,
                "Could not reach the remote",
                f"Could not check whether '{name}' was already pushed.\n\n"
                f"Delete the local tag anyway? If it was pushed, the remote "
                f"copy stays and will come back on the next fetch.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
        else:
            confirm = QMessageBox.question(
                self,
                "Delete tag",
                f"Delete the local tag '{name}'?\n\n"
                f"It has not been pushed, so nothing on the remote changes.\n"
                f"Repository: {repo}",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
        if confirm != QMessageBox.StandardButton.Yes:
            self.status.setText("")
            return

        result = git_ops.delete_tag(repo, name)
        if result.ok:
            self.status.setText(f"Deleted local tag '{name}'.")
            self.tag_edit.clear()
            self._reload_tags()
        else:
            QMessageBox.critical(
                self,
                "Could not delete tag",
                result.stderr.strip() or "git tag -d failed.",
            )
