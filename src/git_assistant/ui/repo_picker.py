"""Filterable repository picker, shared by every tab that acts on one repo.

One implementation so the tabs cannot drift apart in behaviour: selecting a
repository here is what sets the active repository and records it as recent.
Submodules are shown nested under the repository that contains them, and are
selectable in their own right -- a submodule is a repo you commit in.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QLabel,
    QLineEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant.config import RepoEntry, RepoNode, Settings, build_repo_tree


class RepoPicker(QWidget):
    """A filter box above a tree of repositories and their submodules."""

    repoChanged = pyqtSignal(str)  # emitted with the newly selected path

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter repositories...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)

        self.repo_list = QTreeWidget()
        self.repo_list.setHeaderHidden(True)
        self.repo_list.setRootIsDecorated(True)
        self.repo_list.currentItemChanged.connect(self._on_selected)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(QLabel("Repository"))
        box.addWidget(self.filter_edit)
        box.addWidget(self.repo_list, 1)

        self.refresh()

    # ---- state -------------------------------------------------------------
    def _items(self):
        """Every repository row, parents before their submodules."""

        def rec(item: QTreeWidgetItem):
            yield item
            for i in range(item.childCount()):
                yield from rec(item.child(i))

        for i in range(self.repo_list.topLevelItemCount()):
            yield from rec(self.repo_list.topLevelItem(i))

    def count(self) -> int:
        """Number of selectable repositories, submodules included."""
        return sum(1 for _ in self._items())

    def current_path(self) -> str:
        item = self.repo_list.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else ""

    def refresh(self) -> None:
        """Reload from settings (call after repositories are added or removed)."""
        self.repo_list.blockSignals(True)
        self.repo_list.clear()
        for node in build_repo_tree(self.settings.ordered_repos()):
            self.repo_list.addTopLevelItem(self._make_item(node))
        self.repo_list.expandAll()
        # Selected after the rows exist: setCurrentItem does nothing for an item
        # that is not in the tree yet.
        active = self.settings.active_repo
        target = next(
            (
                it
                for it in self._items()
                if it.data(0, Qt.ItemDataRole.UserRole) == active
            ),
            None,
        ) or next(iter(self._items()), None)
        if target is not None:
            self.repo_list.setCurrentItem(target)
        self.repo_list.blockSignals(False)
        self._apply_filter(self.filter_edit.text())

    def _make_item(self, node: RepoNode) -> QTreeWidgetItem:
        entry: RepoEntry = node.entry
        item = QTreeWidgetItem([entry.display()])
        item.setData(0, Qt.ItemDataRole.UserRole, entry.path)
        item.setToolTip(0, entry.path)
        for child in node.children:
            item.addChild(self._make_item(child))
        return item

    def _apply_filter(self, text: str) -> None:
        """Hide repositories whose name does not contain the filter text.

        A submodule that matches keeps its parents visible, so a match is never
        stranded outside the tree it belongs to. The selected repository stays
        visible even when filtered out, so the list never implies that nothing
        is selected.
        """
        needle = (text or "").strip().lower()
        current = self.repo_list.currentItem()

        def apply(item: QTreeWidgetItem) -> bool:
            """Show ``item`` when it, a descendant, or the selection matches."""
            matched = (
                not needle or needle in item.text(0).lower() or item is current
            )
            # Not short-circuited: every descendant must have its state applied.
            kept = [apply(item.child(i)) for i in range(item.childCount())]
            visible = matched or any(kept)
            item.setHidden(not visible)
            if visible and needle:
                item.setExpanded(True)
            return visible

        for i in range(self.repo_list.topLevelItemCount()):
            apply(self.repo_list.topLevelItem(i))

    def _on_selected(self, _current=None, _previous=None) -> None:
        path = self.current_path()
        if not path:
            return
        self.settings.active_repo = path
        self.settings.mark_recent(path)
        self.settings.save()
        self.repoChanged.emit(path)
