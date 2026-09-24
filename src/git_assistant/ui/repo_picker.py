"""Filterable repository picker, shared by every tab that acts on one repo.

One implementation so the tabs cannot drift apart in behaviour: selecting a
repository here is what sets the active repository and records it as recent.
Submodules are shown nested under the repository that contains them, and are
selectable in their own right -- a submodule is a repo you commit in.

Groups rather than one sorted list. Recency used to be handled by ordering
-- the active repository first, then the recently used -- which meant the list
silently rearranged itself under you and never said where the recent ones
stopped. So the recent ones have a group of their own, and **All** is the
stable, alphabetical list you can scan by eye. A repository appears in both:
All means all, and a repository that vanished from its usual place whenever it
was used would be worse than a duplicated row.

Above them both, **Favorites**: the repositories the user chose to keep at hand,
by name, added and taken off from any row's right-click menu. Chosen rather than
worked out, so -- unlike recency -- it stays put until the user moves it.

Under All, a repository is listed inside the folder it sits in -- every repository
in ``C:\toolbox`` under a **toolbox** row. Under every group, a repository's
submodules are inside a **Submodules** row of its own, down through the directories
they are kept in: a favorite project is one click from any of its submodules rather
than a hunt for them under All. The folders and the repositories with submodules
start folded, so a hundred and seventy repositories are a list of their folders;
everything inside a repository starts open, so unfolding one shows all of it. A
folder or a Submodules row is not a repository: clicking one opens or folds it, and
chooses nothing. Right-clicking a Submodules row offers to bring every submodule
listed under it to the latest master -- see `git_assistant.submodule_update`.

Each row names the branch that repository is on, after the name and in its own
colour. It is the fact you need before you act on a repository and the one this
window otherwise made you select a repository to find out -- and every tab here
acts on the checked-out branch, so a list that does not say which one it is
makes you check somewhere else first.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QIcon, QPalette
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QLineEdit,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import git_ops
from git_assistant.config import RepoEntry, RepoNode, Settings, build_repo_tree, norm_path

FAVORITES_GROUP = "Favorites"
RECENT_GROUP = "Recently Used"
ALL_GROUP = "All"

ADD_FAVORITE = "Add to Favorites"
REMOVE_FAVORITE = "Remove from Favorites"
#: On a Submodules row's menu.
UPDATE_SUBMODULES = "Update submodules to latest master"

#: On every group's title, because a right-click is not something a list says
#: it answers to.
_GROUP_TIPS = {
    FAVORITES_GROUP: "Right-click a repository to take it off Favorites.",
    RECENT_GROUP: "Right-click a repository to add it to Favorites.",
    ALL_GROUP: "Right-click a repository to add it to Favorites.",
}

#: Where a row keeps the branch it is on. Beside the text rather than in it, so
#: the branch can be painted in its own colour -- and so the filter box goes on
#: matching repository names and only those.
BRANCH_ROLE = Qt.ItemDataRole.UserRole + 1

#: What a row is: one of the kinds below.
KIND_ROLE = Qt.ItemDataRole.UserRole + 2
#: What a row is called across rebuilds, so a folder opened by hand stays open when
#: the list is built again: every tab rebuilds its list whenever it is shown.
_KEY_ROLE = Qt.ItemDataRole.UserRole + 3
#: Whether a row starts open.
_OPEN_ROLE = Qt.ItemDataRole.UserRole + 4

GROUP_KIND = "group"
FOLDER_KIND = "folder"
REPO_KIND = "repo"
SUBMODULES_KIND = "submodules"
DIRECTORY_KIND = "directory"

#: The row a repository's submodules are listed under.
SUBMODULES = "Submodules"

#: The icons of folder and Submodules rows, fetched once. The style fetches each from
#: the shell -- ten milliseconds a time, measured on Windows 11 -- and every tab builds
#: its list again whenever it is shown.
_ICONS: dict[QStyle.StandardPixmap, QIcon] = {}

#: Space between a repository's name and its branch. Wide enough that the two
#: read as two things; the colour does the rest.
_BRANCH_GAP = 12

#: The branch, in green -- the colour git itself gives a branch name. Two of
#: them because this list is drawn light and dark: the dark green disappears
#: into a dark row and the light one washes out on a light one.
_BRANCH_ON_LIGHT = QColor("#1a7f4b")
_BRANCH_ON_DARK = QColor("#5fd39a")


def branch_colour(
    palette: QPalette, background: QPalette.ColorRole = QPalette.ColorRole.Base
) -> QColor:
    """Whichever green reads on ``background``: a list's base by default.

    Public because a branch is named in green wherever it is named -- the bar
    above the tabs names one on the window's own background.

    The list's own background, and not the selected row's -- which sounds like
    the thing that would catch out a colour chosen for the list, and is not.
    Measured on the style this ships against: the Windows 11 style paints a
    selected row #f5f5f5 on a #ffffff list and #393939 on a #2d2d2d one, never
    the palette's highlight colour. A row's selection moves its background by
    about four percent, so it does not come into this.
    """
    lightness = palette.color(background).lightness()
    return _BRANCH_ON_DARK if lightness < 128 else _BRANCH_ON_LIGHT


class _BranchDelegate(QStyledItemDelegate):
    """Paints a row as its repository name, then the branch it is on.

    A delegate rather than a second column: a column lines every branch up in a
    stripe of its own, stranded from the short names and jammed against the
    long ones, when what the branch belongs beside is the name it annotates.

    A delegate rather than folding the branch into the item's text: one item
    has one colour, and a branch in the same colour as the name is a longer
    name rather than a branch.
    """

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        branch = index.data(BRANCH_ROLE)
        if branch:
            opt = QStyleOptionViewItem(option)
            self.initStyleOption(opt, index)
            width = opt.fontMetrics.horizontalAdvance(branch)
            size.setWidth(size.width() + _BRANCH_GAP + width)
        return size

    def paint(self, painter, option, index):
        # The row itself -- its background, its selection, its focus ring and
        # its name -- stays the style's to draw, so it keeps looking like every
        # other list in the window. Only the branch is drawn here, after it.
        super().paint(painter, option, index)
        branch = index.data(BRANCH_ROLE)
        if not branch:
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        area = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText, opt, widget
        )
        metrics = opt.fontMetrics
        area.setLeft(
            area.left() + metrics.horizontalAdvance(opt.text) + _BRANCH_GAP
        )
        # Elided from the *left*, unlike the name beside it: a branch is named
        # front-to-back from the general to the particular, so its prefix is
        # the part every branch in the repository shares. Cut from the right,
        # a column this narrow shows "dev/re..." against all of them.
        shown = (
            metrics.elidedText(branch, Qt.TextElideMode.ElideLeft, area.width())
            if area.width() > 0
            else ""
        )
        if not shown.strip("…"):
            # The name has taken the row, and the style has already elided it
            # there. The name is the one that has to stay readable, so a row
            # this narrow gets no branch at all rather than an ellipsis
            # standing in for one -- the tooltip still has it in full.
            return
        painter.save()
        painter.setPen(branch_colour(opt.palette))
        painter.drawText(
            area,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            shown,
        )
        painter.restore()


class _RepoTree(QTreeWidget):
    """The list, where a right-click opens a menu and selects nothing.

    Qt's item views move the current row on any mouse button. Here the current
    row is the active repository -- every tab reloads for it -- so a right-click
    to add a repository to Favorites would also switch to it.
    """

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        # A folder, a Submodules row or a group title opens or folds, and that is
        # all: it is not a repository, and letting it become the current row left
        # the list with no repository selected while every tab went on with one.
        item = self.itemAt(event.position().toPoint())
        if (
            event.button() == Qt.MouseButton.LeftButton
            and item is not None
            and not item.data(0, Qt.ItemDataRole.UserRole)
        ):
            item.setExpanded(not item.isExpanded())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt naming
        # The press before it has already opened or folded the row; Qt's own
        # double-click would put it straight back.
        item = self.itemAt(event.position().toPoint())
        if item is not None and not item.data(0, Qt.ItemDataRole.UserRole):
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt naming
        if event.button() == Qt.MouseButton.RightButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)


class RepoPicker(QWidget):
    """A filter box above a tree of repositories and their submodules."""

    repoChanged = pyqtSignal(str)  # emitted with the newly selected path

    #: The branch beside each repository was read again, after a checkout. For
    #: anything else on screen that names a branch: `repoChanged` does not fire,
    #: because which repository is selected has not changed.
    branchesChanged = pyqtSignal()  # noqa: N815 - Qt signal naming

    #: The submodules of the repository named were fetched and switched: whatever a
    #: tab shows of it or of them was read before. Emitted after `branchesChanged`.
    submodulesUpdated = pyqtSignal(str)  # noqa: N815 - Qt signal naming

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        #: The repository last chosen here. What the list answers with while the
        #: row under the keyboard is a folder rather than a repository.
        self._chosen = ""
        #: Rows opened or folded by hand, by `_KEY_ROLE`: kept through rebuilds.
        self._opened: dict[str, bool] = {}
        #: True while the list opens and folds rows itself, which is not the user.
        self._arranging = False
        #: Each repository's branch while rows are made, so one listed three times --
        #: a favorite used recently, with its submodules -- is read once. None between
        #: builds: the next one reads again, for a checkout made in the meantime.
        self._branches: dict[str, str] | None = None

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter repositories...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)

        self.repo_list = _RepoTree()
        self.repo_list.setHeaderHidden(True)
        self.repo_list.setRootIsDecorated(True)
        self.repo_list.setItemDelegate(_BranchDelegate(self.repo_list))
        self.repo_list.currentItemChanged.connect(self._on_selected)
        self.repo_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.repo_list.customContextMenuRequested.connect(self._on_menu)
        self.repo_list.itemExpanded.connect(lambda item: self._remember_open(item, True))
        self.repo_list.itemCollapsed.connect(lambda item: self._remember_open(item, False))

        #: Hidden by a host that already titles the list -- the folding
        #: Repository pane does, on its strip.
        self.title_label = QLabel("Repository")

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.title_label)
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

        Distinct repositories, not rows: favorites and the recently used are
        listed twice on purpose, and a count that grew when one was used would
        be counting the shortcut rather than the repository.
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
        path = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else ""
        return path or self._chosen

    def select(self, path: str) -> bool:
        """Select ``path`` as a click would; False when it is not in the list.

        Everything a click does: it becomes the active repository, is recorded as
        recently used, saved, and `repoChanged` fires -- which is why this goes
        through `_on_selected` rather than around it. A row that is already the
        selected one does not change, so that is called directly.
        """
        groups = (
            self.repo_list.topLevelItem(i)
            for i in range(self.repo_list.topLevelItemCount())
        )
        everything = next((g for g in groups if g.text(0) == ALL_GROUP), None)
        target = self._find(everything, path) if everything is not None else None
        if target is None:
            return False
        if self.repo_list.currentItem() is target:
            self._on_selected()
        else:
            self.repo_list.setCurrentItem(target)
        return True

    def refresh_branches(self) -> None:
        """Re-read the branch beside each repository, leaving the tree alone.

        For after a checkout, which moves one label and nothing else: `refresh`
        would rebuild the list and take the scroll position and whatever the
        user had folded open with it. Every row rather than the one that was
        checked out, because a repository can be listed more than once -- under
        **Favorites** and **Recently Used** as well as under **All** -- and half
        an answer on screen is worse than the stale one it replaced.
        """
        with self._reading_branches():
            for item in self._items():
                path = item.data(0, Qt.ItemDataRole.UserRole)
                if path:
                    self._label_branch(item, path)
        self.branchesChanged.emit()

    @contextmanager
    def _reading_branches(self):
        """While rows are labelled: each repository's branch read once, not once a row."""
        self._branches = {}
        try:
            yield
        finally:
            self._branches = None

    def _label_branch(self, item: QTreeWidgetItem, path: str) -> None:
        """Note which branch ``path`` is on, for the delegate and the tooltip."""
        known = self._branches if self._branches is not None else {}
        if path not in known:
            known[path] = git_ops.head_branch(path)
        branch = known[path]
        item.setData(0, BRANCH_ROLE, branch)
        # The branch is elided out of a narrow list before the name is, so the
        # tooltip is where it can always be read in full.
        item.setToolTip(0, f"{path}\nOn branch {branch}" if branch else path)

    def refresh(self) -> None:
        """Reload from settings (call after repositories are added or removed)."""
        self.repo_list.blockSignals(True)
        self.repo_list.clear()
        roots = build_repo_tree(self._all_entries())
        nodes = _by_path(roots)

        with self._reading_branches():
            favorites = self._favorites_group(nodes)
            if favorites is not None:
                self.repo_list.addTopLevelItem(favorites)
                favorites.setExpanded(True)

            recent = self._recent_entries()
            if recent:
                header = self._make_header(RECENT_GROUP)
                for entry in recent:
                    # A ranking, not a hierarchy: a submodule used recently is a row
                    # in its own right here, not a reason to list its parent too.
                    header.addChild(self._make_shortcut(entry, nodes, RECENT_GROUP))
                self.repo_list.addTopLevelItem(header)
                header.setExpanded(True)

            everything = self._make_header(ALL_GROUP)
            self._fill_all(everything, roots)
            self.repo_list.addTopLevelItem(everything)
            everything.setExpanded(True)

        # Selected after the rows exist: setCurrentItem does nothing for an item
        # that is not in the tree yet. Under All rather than the shortcut, so
        # the selection does not move about as recency changes.
        target = self._find(everything, self._remembered())
        if target is None and self.SELECTS_FIRST:
            target = self._first_repo(everything)
        if target is not None:
            self.repo_list.setCurrentItem(target)
        self._chosen = target.data(0, Qt.ItemDataRole.UserRole) if target is not None else ""
        self.repo_list.blockSignals(False)
        # Opens and folds every row as it starts, or as it was left by hand.
        self._apply_filter(self.filter_edit.text())

    def _fill_all(self, header: QTreeWidgetItem, roots: list[RepoNode]) -> None:
        """Every repository, inside the folder it sits in, by folder name and then name."""
        by_folder: dict[str, tuple[str, list[RepoNode]]] = {}
        for node in roots:
            folder = os.path.dirname(os.path.normpath(node.entry.path))
            by_folder.setdefault(norm_path(folder), (folder, []))[1].append(node)
        labels = _folder_labels([folder for folder, _nodes in by_folder.values()])
        for key, (folder, nodes) in sorted(
            by_folder.items(), key=lambda item: labels[item[1][0]].casefold()
        ):
            row = self._make_container(
                labels[folder], FOLDER_KIND, f"{ALL_GROUP}|folder:{key}", opened=False, tip=folder
            )
            for node in sorted(nodes, key=lambda one: _repo_name(one.entry).casefold()):
                row.addChild(
                    self._make_repo_item(node, _repo_name(node.entry), root=True, group=ALL_GROUP)
                )
            header.addChild(row)

    def _make_container(
        self, title: str, kind: str, key: str, *, opened: bool, tip: str = ""
    ) -> QTreeWidgetItem:
        """A folder, a Submodules row or a directory: a label that opens and folds."""
        item = QTreeWidgetItem([title])
        item.setData(0, Qt.ItemDataRole.UserRole, "")
        item.setData(0, KIND_ROLE, kind)
        item.setData(0, _KEY_ROLE, key)
        item.setData(0, _OPEN_ROLE, opened)
        item.setToolTip(0, tip)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        pixmap = (
            QStyle.StandardPixmap.SP_DirLinkIcon
            if kind == SUBMODULES_KIND
            else QStyle.StandardPixmap.SP_DirIcon
        )
        if pixmap not in _ICONS:
            _ICONS[pixmap] = self.style().standardIcon(pixmap)
        item.setIcon(0, _ICONS[pixmap])
        return item

    def _make_repo_item(
        self, node: RepoNode, name: str, *, root: bool, group: str
    ) -> QTreeWidgetItem:
        """A repository, and its submodules in a row of their own beneath it.

        Folded when it is a repository of its own with submodules to show; a
        submodule's submodules open, like everything else inside a repository.
        Remembered open or folded under ``group`` alone: a favorite opened is not
        its copy under All opened too.
        """
        entry: RepoEntry = node.entry
        item = QTreeWidgetItem([name])
        item.setData(0, Qt.ItemDataRole.UserRole, entry.path)
        item.setData(0, KIND_ROLE, REPO_KIND)
        item.setData(0, _KEY_ROLE, f"{group}|repo:{norm_path(entry.path)}")
        item.setData(0, _OPEN_ROLE, not root)
        self._label_branch(item, entry.path)
        if node.children:
            item.addChild(self._make_submodules(node, group))
        return item

    def _make_shortcut(
        self, entry: RepoEntry, nodes: dict[str, RepoNode], group: str
    ) -> QTreeWidgetItem:
        """A row of Favorites or Recently Used: named with its folder, submodules beneath it."""
        node = nodes.get(norm_path(entry.path))
        children = node.children if node is not None else []
        return self._make_repo_item(
            RepoNode(entry, children), entry.display(), root=True, group=group
        )

    def _make_submodules(self, node: RepoNode, group: str) -> QTreeWidgetItem:
        """``Submodules``, and in it each submodule down the directories it is kept in.

        Every directory on the way a row of its own, directories before the
        submodules beside them and each by name: ``libs/can`` and ``libs/ccp`` are
        two rows under **libs**, as a file browser would show them.
        """
        base = os.path.normpath(node.entry.path)
        holder = self._make_container(
            SUBMODULES,
            SUBMODULES_KIND,
            f"{group}|submodules:{norm_path(base)}",
            opened=True,
            tip=f"{len(node.children)} submodule(s) of {_repo_name(node.entry)}.\n"
            f"Right-click to update them to the latest master.",
        )
        # A directory: its own directories by name, and the submodules in it.
        tree: dict = {"dirs": {}, "repos": []}
        for child in node.children:
            parts = os.path.relpath(os.path.normpath(child.entry.path), base).split(os.sep)
            place = tree
            for part in parts[:-1]:
                place = place["dirs"].setdefault(part.casefold(), (part, {"dirs": {}, "repos": []}))[1]
            place["repos"].append((child.entry.label or parts[-1], child))

        def fill(item: QTreeWidgetItem, place: dict, where: str) -> None:
            for _key, (name, inner) in sorted(place["dirs"].items()):
                path = os.path.join(where, name)
                directory = self._make_container(
                    name, DIRECTORY_KIND, f"{group}|dir:{norm_path(path)}", opened=True, tip=path
                )
                fill(directory, inner, path)
                item.addChild(directory)
            for name, child in sorted(place["repos"], key=lambda pair: pair[0].casefold()):
                item.addChild(self._make_repo_item(child, name, root=False, group=group))

        fill(holder, tree, base)
        return holder

    #: With nothing remembered, or the remembered one gone from the list, select
    #: the first repository rather than none.
    SELECTS_FIRST = True

    def _remembered(self) -> str:
        """The repository this list selects when it is built: the active one."""
        return self.settings.active_repo

    def _all_entries(self) -> list[RepoEntry]:
        """Every repository, by name. `build_repo_tree` sorts the nested ones."""
        return sorted(self.settings.repos, key=lambda e: e.display().casefold())

    # ---- favorites ---------------------------------------------------------
    def set_favorite(self, path: str, favorite: bool) -> None:
        """Add ``path`` to Favorites or take it off, saved, and on screen at once.

        Only the Favorites group is rebuilt. The rest of the list keeps its rows
        and whatever was folded open, as after a checkout.
        """
        self.settings.set_favorite(path, favorite)
        self.settings.save()
        self.repo_list.blockSignals(True)
        try:
            first = self.repo_list.topLevelItem(0)
            if first is not None and first.text(0) == FAVORITES_GROUP:
                self.repo_list.takeTopLevelItem(0)
            with self._reading_branches():
                group = self._favorites_group(_by_path(build_repo_tree(self._all_entries())))
            if group is not None:
                self.repo_list.insertTopLevelItem(0, group)
                group.setExpanded(True)
                # Rows open or fold once they are in the list, not before: each
                # favorite as it starts, or as it was left before this rebuilt it.
                self._arrange_as_left(self._under(group))
            # The selected row may have been one of the favorites just replaced.
            # Qt moves the selection to a neighbour of its own choosing, and the
            # remembered repository is the one that has to stay selected. The row
            # itself, not `current_path`, which would answer with the last one
            # chosen whatever row Qt moved to.
            current = self.repo_list.currentItem()
            if (
                current is None
                or current.data(0, Qt.ItemDataRole.UserRole) != self._remembered()
            ):
                everything = next(
                    (g for g in self._groups() if g.text(0) == ALL_GROUP), None
                )
                if everything is not None:
                    target = self._find(everything, self._remembered())
                    if target is not None:
                        self.repo_list.setCurrentItem(target)
        finally:
            self.repo_list.blockSignals(False)
        # Only while there is something typed. With the box empty nothing is
        # hidden, and filtering lays every row out afresh -- folding any the list
        # opened itself to show a selection, which this was careful to leave open.
        if self.filter_edit.text().strip():
            self._apply_filter(self.filter_edit.text())

    def _groups(self):
        return [
            self.repo_list.topLevelItem(i)
            for i in range(self.repo_list.topLevelItemCount())
        ]

    def _favorites_group(self, nodes: dict[str, RepoNode]) -> QTreeWidgetItem | None:
        """The Favorites group, by name, or None while there are none."""
        by_path = {r.path: r for r in self.settings.repos}
        entries = sorted(
            (by_path[p] for p in self.settings.favorite_repos if p in by_path),
            key=lambda e: e.display().casefold(),
        )
        if not entries:
            return None
        header = self._make_header(FAVORITES_GROUP)
        for entry in entries:
            # As the recent ones are: a favorite submodule is a row of its own here
            # rather than a reason to show its parent too.
            header.addChild(self._make_shortcut(entry, nodes, FAVORITES_GROUP))
        return header

    def _on_menu(self, point) -> None:
        item = self.repo_list.itemAt(point)
        if item is not None and item.data(0, KIND_ROLE) == SUBMODULES_KIND:
            holder = item
            menu = QMenu(self)
            menu.addAction(UPDATE_SUBMODULES, lambda: self._update_submodules(holder))
            menu.exec(self.repo_list.viewport().mapToGlobal(point))
            return
        path = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else ""
        if not path:
            return  # a group's title, a folder, or the space below the last row
        menu = QMenu(self)
        if self.settings.is_favorite(path):
            menu.addAction(REMOVE_FAVORITE, lambda: self.set_favorite(path, False))
        else:
            menu.addAction(ADD_FAVORITE, lambda: self.set_favorite(path, True))
        menu.exec(self.repo_list.viewport().mapToGlobal(point))

    # ---- submodules ----------------------------------------------------------
    def _update_submodules(self, holder: QTreeWidgetItem) -> None:
        """Offer to bring every submodule under ``holder`` to the latest master.

        What is needed of the row is read before the window opens: a list rebuilt
        behind a modal window -- by a rescan, say -- takes its rows with it.
        """
        # Imported here: every tab has this list, and few of them ever open this.
        from git_assistant import submodule_update
        from git_assistant.ui import submodule_update_dialog

        owner = holder.parent()
        repo = owner.data(0, Qt.ItemDataRole.UserRole) if owner is not None else ""
        chains = _submodule_chains(holder, repo)
        if not repo or not chains:
            return
        dialog = submodule_update_dialog.SubmoduleUpdateDialog(
            repo,
            chains,
            rules_for=submodule_update.fetch_rules(self.settings),
            parent=self,
        )
        dialog.exec()
        ran = dialog.ran
        dialog.deleteLater()
        if ran:
            # Most of them are on another branch now, which every row names.
            self.refresh_branches()
            self.submodulesUpdated.emit(repo)

    def _recent_entries(self) -> list[RepoEntry]:
        """Every repository used, most recent first, skipping any since removed.

        All of them rather than the last few: settings keeps the whole history,
        and a repository used last week is no less worth reaching for because
        five others were opened since.
        """
        by_path = {r.path: r for r in self.settings.repos}
        recent: list[RepoEntry] = []
        seen: set[str] = set()  # by path: a list scan per row is quadratic now
        for path in self.settings.recent_repos:
            entry = by_path.get(path)
            if entry is not None and path not in seen:
                seen.add(path)
                recent.append(entry)
        return recent

    def _make_header(self, title: str) -> QTreeWidgetItem:
        """A group row: a label, and nothing that can be selected or acted on."""
        item = QTreeWidgetItem([title])
        item.setData(0, Qt.ItemDataRole.UserRole, "")
        item.setToolTip(0, _GROUP_TIPS.get(title, ""))
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
        return next(
            (item for item in self._under(parent) if item.data(0, Qt.ItemDataRole.UserRole)),
            None,
        )

    @staticmethod
    def _under(parent: QTreeWidgetItem):
        """Every repository row beneath a group, parents before submodules."""

        def rec(item: QTreeWidgetItem):
            for i in range(item.childCount()):
                child = item.child(i)
                yield child
                yield from rec(child)

        return list(rec(parent))

    def _apply_filter(self, text: str) -> None:
        """Hide repositories whose name does not contain the filter text.

        A submodule that matches keeps its parents visible, so a match is never
        stranded outside the tree it belongs to, and they open far enough to show
        it. A folder or a directory whose own name matches shows everything in it.
        The selected repository stays visible even when filtered out, so the list
        never implies that nothing is selected.

        With the box empty every row is shown, open or folded as it starts -- or
        as it was left by hand -- and the rows above the selected repository open.
        """
        needle = (text or "").strip().lower()
        chosen = self._chosen_item()

        def apply(item: QTreeWidgetItem, inherited: bool) -> tuple[bool, bool]:
            """``(visible, a match or the selection at or below it)``."""
            kind = item.data(0, KIND_ROLE)
            # "Submodules" is a heading: "sub" is not a filter for every repository with one.
            own = bool(needle) and kind != SUBMODULES_KIND and needle in item.text(0).lower()
            passes_on = own and kind in (FOLDER_KIND, DIRECTORY_KIND)
            # Not short-circuited: every descendant must have its state applied.
            below = [apply(item.child(i), inherited or passes_on) for i in range(item.childCount())]
            found = own or item is chosen or any(f for _v, f in below)
            visible = not needle or inherited or found or any(v for v, _f in below)
            item.setHidden(not visible)
            if needle:
                # Opened only to reveal a match, so clearing the box folds the
                # submodules back rather than leaving the tree wide open.
                self._arrange(item, any(f for _v, f in below) or (passes_on and visible))
            return visible, found

        for i in range(self.repo_list.topLevelItemCount()):
            group = self.repo_list.topLevelItem(i)
            # A group's own title is not a repository, so it must not count as
            # a match: "All" would otherwise answer to a filter of "al".
            shown = [apply(group.child(j), False)[0] for j in range(group.childCount())]
            group.setHidden(not any(shown))
            self._arrange(group, True)
        if not needle:
            self._arrange_as_left(self._items())
            self._reveal(chosen)

    def _arrange_as_left(self, items) -> None:
        """Open or fold each of ``items`` as it starts, or as it was left by hand."""
        for item in items:
            key = item.data(0, _KEY_ROLE)
            if key and item.childCount():
                self._arrange(item, self._opened.get(key, bool(item.data(0, _OPEN_ROLE))))

    def _chosen_item(self) -> QTreeWidgetItem | None:
        """The selected repository's row: the tree's current one, or its copy under All."""
        current = self.repo_list.currentItem()
        if current is not None and current.data(0, Qt.ItemDataRole.UserRole):
            return current
        everything = next((g for g in self._groups() if g.text(0) == ALL_GROUP), None)
        return self._find(everything, self._chosen) if everything is not None else None

    def _reveal(self, item: QTreeWidgetItem | None) -> None:
        """Open the rows above ``item`` -- all but those folded by hand."""
        parent = item.parent() if item is not None else None
        while parent is not None:
            key = parent.data(0, _KEY_ROLE)
            if not key or self._opened.get(key, True):
                self._arrange(parent, True)
            parent = parent.parent()

    def _arrange(self, item: QTreeWidgetItem, opened: bool) -> None:
        """Open or fold ``item`` on the list's own account, not as the user's choice."""
        self._arranging = True
        try:
            item.setExpanded(opened)
        finally:
            self._arranging = False

    def _remember_open(self, item: QTreeWidgetItem, opened: bool) -> None:
        """A row opened or folded by hand, to be left so when the list is rebuilt."""
        key = item.data(0, _KEY_ROLE)
        if self._arranging or not key or self.filter_edit.text().strip():
            return
        self._opened[key] = opened

    def _on_selected(self, _current=None, _previous=None) -> None:
        item = self.repo_list.currentItem()
        path = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else ""
        if not path:
            return  # a folder, a Submodules row, a group: not a repository to choose
        self._chosen = path
        self._choose(path)

    def _choose(self, path: str) -> None:
        self.settings.active_repo = path
        self.settings.mark_recent(path)
        self.settings.save()
        self.repoChanged.emit(path)


def _repo_name(entry: RepoEntry) -> str:
    """A repository as a folder row lists it: its label, or the folder it is."""
    return entry.label or Path(entry.path).name or entry.path


def _submodule_chains(holder: QTreeWidgetItem, repo: str):
    """Each submodule listed under a Submodules row, and the ones inside it after it.

    Every repository row beneath the row, down through the directories and the
    Submodules rows of submodules: what is listed under it is what it stands for.
    One chain per submodule of ``repo``, in the order listed -- the one it is in
    before each submodule inside it, so none is brought up ahead of its container.
    Each with the repository it is a submodule of.
    """
    from git_assistant.submodule_update import Target

    chains: list[list[Target]] = []

    def walk(item: QTreeWidgetItem, superproject: str, chain: list | None) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            path = child.data(0, Qt.ItemDataRole.UserRole)
            if not path:  # a directory, or a submodule's own Submodules row
                walk(child, superproject, chain)
                continue
            target = Target(path, superproject)
            if chain is None:
                chains.append([target])
                walk(child, path, chains[-1])
            else:
                chain.append(target)
                walk(child, path, chain)

    walk(holder, repo, None)
    return chains


def _by_path(roots: list[RepoNode]) -> dict[str, RepoNode]:
    """Every repository in the tree by `norm_path`: where a shortcut finds its submodules."""
    found: dict[str, RepoNode] = {}
    waiting = list(roots)
    while waiting:
        node = waiting.pop()
        found[norm_path(node.entry.path)] = node
        waiting.extend(node.children)
    return found


def _folder_labels(folders: list[str]) -> dict[str, str]:
    """Each folder's name -- and where it is, for two folders of one name.

    ``D:\\workspace\\ONEoo7`` and ``F:\\backup\\ONEoo7`` are two rows that would read
    the same. A folder at the root of a drive has no name, and is called by its path.
    """
    named: dict[str, list[str]] = {}
    for folder in folders:
        named.setdefault((Path(folder).name or folder).casefold(), []).append(folder)
    labels: dict[str, str] = {}
    for same in named.values():
        for folder in same:
            name = Path(folder).name or folder
            labels[folder] = name if len(same) == 1 else f"{name} ({Path(folder).parent})"
    return labels


class OtherRepoPicker(RepoPicker):
    """The same list, for a second repository set beside the active one.

    Choosing here makes nothing active, puts nothing among the recently used and
    changes no other tab: only ``settings.compare_repo`` remembers it. With nothing
    remembered nothing is selected, since the active repository set beside itself
    would be a comparison of nothing.
    """

    SELECTS_FIRST = False

    def _remembered(self) -> str:
        return self.settings.compare_repo

    def _choose(self, path: str) -> None:
        self.settings.compare_repo = path
        self.settings.save()
        self.repoChanged.emit(path)
