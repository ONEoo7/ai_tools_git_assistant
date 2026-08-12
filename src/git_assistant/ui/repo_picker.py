"""Filterable repository picker, shared by every tab that acts on one repo.

One implementation so the tabs cannot drift apart in behaviour: selecting a
repository here is what sets the active repository and records it as recent.
Submodules are shown nested under the repository that contains them, and are
selectable in their own right -- a submodule is a repo you commit in.

Two groups rather than one sorted list. Recency used to be handled by ordering
-- the active repository first, then the recently used -- which meant the list
silently rearranged itself under you and never said where the recent ones
stopped. So the recent ones have a group of their own, and **All** is the
stable, alphabetical list you can scan by eye. A repository appears in both:
All means all, and a repository that vanished from its usual place whenever it
was used would be worse than a duplicated row.
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

#: How many of the recently used are worth a shortcut. Past a handful it stops
#: being a shortcut and becomes a second copy of the list below it.
RECENT_SHOWN = 5

RECENT_GROUP = "Recently Used"
ALL_GROUP = "All"


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
        """Number of selectable repositories, submodules included.

        Distinct repositories, not rows: the recently used are listed twice on
        purpose, and a count that grew when one was used would be counting the
        shortcut rather than the repository.
        """
        return len(
            {
                path
                for it in self._items()
                if (path := it.data(0, Qt.ItemDataRole.UserRole))
            }
        )

    def current_path(self) -> str:
        item = self.repo_list.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) if item else ""

    def refresh(self) -> None:
        """Reload from settings (call after repositories are added or removed)."""
        self.repo_list.blockSignals(True)
        self.repo_list.clear()

        recent = self._recent_entries()
        if recent:
            header = self._make_header(RECENT_GROUP)
            for entry in recent:
                # Flat: this is a ranking, not a hierarchy, and a submodule that
                # was used recently is a row in its own right here.
                header.addChild(self._make_item(RepoNode(entry)))
            self.repo_list.addTopLevelItem(header)
            header.setExpanded(True)

        everything = self._make_header(ALL_GROUP)
        for node in build_repo_tree(self._all_entries()):
            everything.addChild(self._make_item(node))
        self.repo_list.addTopLevelItem(everything)
        everything.setExpanded(True)

        # Selected after the rows exist: setCurrentItem does nothing for an item
        # that is not in the tree yet. Under All rather than the shortcut, so
        # the selection does not move about as recency changes.
        active = self.settings.active_repo
        target = self._find(everything, active) or self._first_repo(everything)
        if target is not None:
            self.repo_list.setCurrentItem(target)
        self.repo_list.blockSignals(False)
        self._apply_filter(self.filter_edit.text())

    def _all_entries(self) -> list[RepoEntry]:
        """Every repository, by name. `build_repo_tree` sorts the nested ones."""
        return sorted(self.settings.repos, key=lambda e: e.display().casefold())

    def _recent_entries(self) -> list[RepoEntry]:
        """The last few used, most recent first, skipping any since removed."""
        by_path = {r.path: r for r in self.settings.repos}
        seen: list[RepoEntry] = []
        for path in self.settings.recent_repos:
            entry = by_path.get(path)
            if entry is not None and entry not in seen:
                seen.append(entry)
            if len(seen) == RECENT_SHOWN:
                break
        return seen

    def _make_header(self, title: str) -> QTreeWidgetItem:
        """A group row: a label, and nothing that can be selected or acted on."""
        item = QTreeWidgetItem([title])
        item.setData(0, Qt.ItemDataRole.UserRole, "")
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        return item

    def _find(self, parent: QTreeWidgetItem, path: str) -> QTreeWidgetItem | None:
        if not path:
            return None
        for item in self._under(parent):
            if item.data(0, Qt.ItemDataRole.UserRole) == path:
                return item
        return None

    def _first_repo(self, parent: QTreeWidgetItem) -> QTreeWidgetItem | None:
        return next(iter(self._under(parent)), None)

    @staticmethod
    def _under(parent: QTreeWidgetItem):
        """Every repository row beneath a group, parents before submodules."""

        def rec(item: QTreeWidgetItem):
            for i in range(item.childCount()):
                child = item.child(i)
                yield child
                yield from rec(child)

        return list(rec(parent))

    def _make_item(self, node: RepoNode) -> QTreeWidgetItem:
        entry: RepoEntry = node.entry
        item = QTreeWidgetItem([entry.display()])
        item.setData(0, Qt.ItemDataRole.UserRole, entry.path)
        item.setToolTip(0, entry.path)
        for child in node.children:
            item.addChild(self._make_item(child))
        # Folded: one repository with forty submodules is otherwise forty-one
        # rows before the second repository.
        item.setExpanded(False)
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
            # Opened only to reveal a match, so clearing the box folds the
            # submodules back rather than leaving the tree wide open.
            item.setExpanded(visible and bool(needle))
            return visible

        for i in range(self.repo_list.topLevelItemCount()):
            group = self.repo_list.topLevelItem(i)
            # A group's own title is not a repository, so it must not count as
            # a match: "All" would otherwise answer to a filter of "al".
            shown = [apply(group.child(j)) for j in range(group.childCount())]
            group.setHidden(not any(shown))
            group.setExpanded(True)

    def _on_selected(self, _current=None, _previous=None) -> None:
        path = self.current_path()
        if not path:
            return
        self.settings.active_repo = path
        self.settings.mark_recent(path)
        self.settings.save()
        self.repoChanged.emit(path)
