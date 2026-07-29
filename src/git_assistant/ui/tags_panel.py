"""Tags tab: inspect the current version, pick a bump, create and push a tag."""

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

from git_assistant import git_ops, versioning
from git_assistant.config import Settings
from git_assistant.ui.preview_dialog import SECTION_GAP
from git_assistant.ui.repo_picker import RepoPicker
from git_assistant.ui.workers import FunctionWorker, run_worker

CUSTOM = "custom"


class TagsPanel(QWidget):
    """Create release tags with a proposed semantic-version bump."""

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._thread = None
        self._worker = None
        self._current = None  # versioning.Version | None

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

        # Repository on the left, tag actions on the right - the same shape as
        # the Generate Commit Message tab.
        picker_pane = QWidget()
        picker_box = QVBoxLayout(picker_pane)
        picker_box.setContentsMargins(0, 0, SECTION_GAP, 0)
        picker_box.addWidget(self.repo_picker, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(picker_pane)
        splitter.addWidget(content)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([220, 700])

        # Default margins, exactly as the Generate Commit Message tab uses, so
        # every tab keeps the same gap between its border and its content.
        outer = QVBoxLayout(self)
        outer.addWidget(splitter)

        self.refresh()

    # ---- state -------------------------------------------------------------
    def _repo_path(self) -> str:
        return self.repo_picker.current_path()

    def refresh(self) -> None:
        """Reload repositories, tags and the proposed version."""
        self.repo_picker.refresh()
        self._reload_tags()

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
        self._reload_tags()

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
