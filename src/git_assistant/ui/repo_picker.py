"""Filterable repository picker, shared by every tab that acts on one repo.

One implementation so the tabs cannot drift apart in behaviour: selecting a
repository here is what sets the active repository and records it as recent.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant.config import Settings


class RepoPicker(QWidget):
    """A filter box above a list of repositories."""

    repoChanged = pyqtSignal(str)  # emitted with the newly selected path

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter repositories...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)

        self.repo_list = QListWidget()
        self.repo_list.currentItemChanged.connect(self._on_selected)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(QLabel("Repository"))
        box.addWidget(self.filter_edit)
        box.addWidget(self.repo_list, 1)

        self.refresh()

    # ---- state -------------------------------------------------------------
    def count(self) -> int:
        return self.repo_list.count()

    def current_path(self) -> str:
        item = self.repo_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def refresh(self) -> None:
        """Reload from settings (call after repositories are added or removed)."""
        self.repo_list.blockSignals(True)
        self.repo_list.clear()
        for entry in self.settings.ordered_repos():
            item = QListWidgetItem(entry.display())
            item.setData(Qt.ItemDataRole.UserRole, entry.path)
            item.setToolTip(entry.path)
            self.repo_list.addItem(item)
            if entry.path == self.settings.active_repo:
                self.repo_list.setCurrentItem(item)
        if self.repo_list.currentRow() < 0 and self.repo_list.count():
            self.repo_list.setCurrentRow(0)
        self.repo_list.blockSignals(False)
        self._apply_filter(self.filter_edit.text())

    def _apply_filter(self, text: str) -> None:
        """Hide repositories whose name does not contain the filter text.

        The selected repository stays visible even when filtered out, so the
        list never implies that nothing is selected.
        """
        needle = (text or "").strip().lower()
        current = self.repo_list.currentItem()
        for i in range(self.repo_list.count()):
            item = self.repo_list.item(i)
            hide = bool(needle) and needle not in item.text().lower()
            item.setHidden(hide and item is not current)

    def _on_selected(self, _current=None, _previous=None) -> None:
        path = self.current_path()
        if not path:
            return
        self.settings.active_repo = path
        self.settings.mark_recent(path)
        self.settings.save()
        self.repoChanged.emit(path)
