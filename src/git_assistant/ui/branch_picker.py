"""The selected repository's branches, filterable, to switch between them.

The Commit tab's branch used to be a combo box among its run settings. It is a
list behind a title of its own now, beside the repository list it belongs to and
folded away the same way: which branch is checked out is named in the bar above
the tabs, so the list is only wanted to switch -- and a list with a filter box
finds one branch among fifty where a combo box makes you scroll for it.

A click on a branch switches to it, as choosing one in the combo box did, and
Enter switches to the highlighted one. Arrow keys only move through the list:
walking down it must not check out every branch on the way.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import git_ops
from git_assistant.ui.repo_picker import branch_colour

BRANCH_TAB = "Branch"

#: What a row keeps: the branch it switches to, or None for the row that says
#: the repository is on no branch at all.
_BRANCH = Qt.ItemDataRole.UserRole


class BranchPicker(QWidget):
    """A filter box over the repository's local branches, the current one marked."""

    #: A branch other than the checked-out one was clicked, or entered.
    branchChosen = pyqtSignal(str)  # noqa: N815 - Qt signal naming

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._current = ""

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter branches...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)

        self.branch_list = QListWidget()
        self.branch_list.setToolTip(
            "Click a branch to switch to it ('git switch'). Uncommitted changes "
            "come along, and git refuses a switch that would lose them."
        )
        self.branch_list.itemClicked.connect(self._on_chosen)
        # Enter, and a double-click: the second click of which lands on the
        # branch just switched to, and does nothing.
        self.branch_list.itemActivated.connect(self._on_chosen)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.filter_edit)
        box.addWidget(self.branch_list, 1)

    # ---- what is shown ---------------------------------------------------------
    def set_branches(self, branches: list[str], current: str) -> None:
        """List ``branches`` with ``current`` marked and selected.

        Emits nothing: filling the list is not somebody choosing a branch.
        """
        self._current = current
        self.branch_list.blockSignals(True)
        self.branch_list.clear()
        if current and current not in branches:
            # Detached, or a repository git cannot read: the state is shown, and
            # nothing that looks like a branch is selected in its place.
            label = "(detached HEAD)" if current == git_ops.DETACHED_HEAD else current
            state = QListWidgetItem(label)
            state.setData(_BRANCH, None)
            state.setFlags(Qt.ItemFlag.ItemIsEnabled)  # not selectable
            font = state.font()
            font.setItalic(True)
            state.setFont(font)
            self.branch_list.addItem(state)
        for name in branches:
            item = QListWidgetItem(name)
            item.setData(_BRANCH, name)
            if name == current:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setForeground(branch_colour(self.branch_list.palette()))
                item.setToolTip(f"{name} - checked out")
            self.branch_list.addItem(item)
            if name == current:
                self.branch_list.setCurrentItem(item)
        self.branch_list.blockSignals(False)
        self._apply_filter(self.filter_edit.text())

    def branches(self) -> list[str]:
        """The branches listed, in their order, filtered out or not."""
        return [
            name
            for row in range(self.branch_list.count())
            if (name := self.branch_list.item(row).data(_BRANCH))
        ]

    def current_branch(self) -> str:
        """The checked-out branch as last shown, or "" when there is none."""
        return self._current if self._current in self.branches() else ""

    def selected_branch(self) -> str:
        item = self.branch_list.currentItem()
        return (item.data(_BRANCH) or "") if item is not None else ""

    def item_for(self, name: str) -> QListWidgetItem | None:
        for row in range(self.branch_list.count()):
            item = self.branch_list.item(row)
            if item.data(_BRANCH) == name:
                return item
        return None

    def _apply_filter(self, text: str) -> None:
        """Hide branches whose name does not contain the filter text.

        The checked-out branch stays, as the selected repository does in the
        repository list: a list that seems to have nothing checked out says
        something that is not true.
        """
        needle = (text or "").strip().lower()
        for row in range(self.branch_list.count()):
            item = self.branch_list.item(row)
            name = item.data(_BRANCH)
            keep = (
                not needle
                or needle in item.text().lower()
                or name is None
                or name == self._current
            )
            item.setHidden(not keep)

    # ---- choosing ----------------------------------------------------------------
    def _on_chosen(self, item: QListWidgetItem) -> None:
        name = item.data(_BRANCH)
        if name and name != self._current:
            self.branchChosen.emit(name)
