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

Each row names the branch that repository is on, after the name and in its own
colour. It is the fact you need before you act on a repository and the one this
window otherwise made you select a repository to find out -- and every tab here
acts on the checked-out branch, so a list that does not say which one it is
makes you check somewhere else first.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPalette
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
from git_assistant.config import RepoEntry, RepoNode, Settings, build_repo_tree

FAVORITES_GROUP = "Favorites"
RECENT_GROUP = "Recently Used"
ALL_GROUP = "All"

ADD_FAVORITE = "Add to Favorites"
REMOVE_FAVORITE = "Remove from Favorites"

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
        super().mousePressEvent(event)

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

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings

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
        return item.data(0, Qt.ItemDataRole.UserRole) if item else ""

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
        for item in self._items():
            path = item.data(0, Qt.ItemDataRole.UserRole)
            if path:
                self._label_branch(item, path)
        self.branchesChanged.emit()

    @staticmethod
    def _label_branch(item: QTreeWidgetItem, path: str) -> None:
        """Note which branch ``path`` is on, for the delegate and the tooltip."""
        branch = git_ops.head_branch(path)
        item.setData(0, BRANCH_ROLE, branch)
        # The branch is elided out of a narrow list before the name is, so the
        # tooltip is where it can always be read in full.
        item.setToolTip(0, f"{path}\nOn branch {branch}" if branch else path)

    def refresh(self) -> None:
        """Reload from settings (call after repositories are added or removed)."""
        self.repo_list.blockSignals(True)
        self.repo_list.clear()

        favorites = self._favorites_group()
        if favorites is not None:
            self.repo_list.addTopLevelItem(favorites)
            favorites.setExpanded(True)

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
        target = self._find(everything, self._remembered())
        if target is None and self.SELECTS_FIRST:
            target = self._first_repo(everything)
        if target is not None:
            self.repo_list.setCurrentItem(target)
        self.repo_list.blockSignals(False)
        self._apply_filter(self.filter_edit.text())

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
            group = self._favorites_group()
            if group is not None:
                self.repo_list.insertTopLevelItem(0, group)
                group.setExpanded(True)
            # The selected row may have been one of the favorites just replaced.
            # Qt moves the selection to a neighbour of its own choosing, and the
            # remembered repository is the one that has to stay selected.
            if self.current_path() != self._remembered():
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
        # hidden, and filtering folds every row -- including the ones opened by
        # hand, which this was careful to leave open.
        if self.filter_edit.text().strip():
            self._apply_filter(self.filter_edit.text())

    def _groups(self):
        return [
            self.repo_list.topLevelItem(i)
            for i in range(self.repo_list.topLevelItemCount())
        ]

    def _favorites_group(self) -> QTreeWidgetItem | None:
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
            # Flat, as the recent ones are: a favorite submodule is a row of its
            # own here rather than a reason to show its parent too.
            header.addChild(self._make_item(RepoNode(entry)))
        return header

    def _on_menu(self, point) -> None:
        item = self.repo_list.itemAt(point)
        path = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else ""
        if not path:
            return  # a group's title, or the space below the last row
        menu = QMenu(self)
        if self.settings.is_favorite(path):
            menu.addAction(REMOVE_FAVORITE, lambda: self.set_favorite(path, False))
        else:
            menu.addAction(ADD_FAVORITE, lambda: self.set_favorite(path, True))
        menu.exec(self.repo_list.viewport().mapToGlobal(point))

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
        self._label_branch(item, entry.path)
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

    def _on_selected(self, _current=None, _previous=None) -> None:
        path = self.current_path()
        if not path:
            return
        self.settings.compare_repo = path
        self.settings.save()
        self.repoChanged.emit(path)
