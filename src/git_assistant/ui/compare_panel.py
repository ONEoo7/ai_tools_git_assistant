"""Compare: two repositories' submodules, side by side.

On the left the active repository, from the same list every other tab chooses it
from. On the right any other, chosen under "Compare with" -- a choice of this tab's
own that changes nothing else: comparing a project with another does not make the
other one the project you commit in.

Every submodule of either side is a row on both, beside the same one on the other:
the commit it is at, the tag on that commit, when it was committed and what it says.
See git_assistant.submodule_compare for what "the same one" is.
"""

from __future__ import annotations

import html
from collections import Counter

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import git_ops
from git_assistant import submodule_compare as compare
from git_assistant.config import RepoEntry, Settings
from git_assistant.git_ops import CommitSummary, SubmoduleState
from git_assistant.submodule_compare import Match, Row, Showing
from git_assistant.ui import side_panel as side_panel_mod
from git_assistant.ui.preview_dialog import SECTION_GAP
from git_assistant.ui.repo_pane import RepoPane
from git_assistant.ui.repo_picker import OtherRepoPicker, RepoPicker
from git_assistant.ui.workers import FunctionWorker, run_worker

COMPARE_WITH_TAB = "Compare with"

COLUMNS = ("Submodule", "Commit", "Tag", "Date", "Message")
PATH, COMMIT, TAG, DATE, MESSAGE = range(len(COLUMNS))

#: Where a row keeps the full hash of the commit it compares, for copying.
HASH_ROLE = Qt.ItemDataRole.UserRole

INFO_COLOUR = "color: #8ab;"
WARN_COLOUR = "color: #b36b00;"
#: A difference between the sides, in the colour this window warns in.
DIFFERENT_COLOUR = QColor("#b36b00")
MUTED_COLOUR = QColor("#888888")

READING = "Reading submodules..."


def _name(settings: Settings, path: str) -> str:
    """``path`` as the repository list names it, label and all."""
    entry = next((r for r in settings.repos if r.path == path), None)
    return (entry or RepoEntry(path=path)).display()


def _title(name: str, path: str) -> str:
    if not path:
        return "<i>No repository chosen</i>"
    branch = git_ops.head_branch(path)
    on = f" <span style='color:#888'>on {html.escape(branch)}</span>" if branch else ""
    return f"<b>{html.escape(name)}</b>{on}"


def _commit_text(state: SubmoduleState, showing: Showing) -> str:
    """The commit compared, and the other one where the submodule has moved off it."""
    compared = compare.compared_hash(state, showing)
    commit = compare.shown_commit(state, showing)
    short = commit.short if commit is not None else compared[:7]
    checked_out = state.checked_out
    moved = (
        checked_out is not None
        and bool(state.recorded_hash)
        and checked_out.hash != state.recorded_hash
    )
    if showing is Showing.CHECKED_OUT:
        if moved:
            return f"{short} (recorded: {state.recorded_hash[:7]})"
        return short
    if not state.recorded_hash:
        return "(not recorded)"
    return f"{short} (checked out: {checked_out.short})" if moved else short


def _why_no_details(state: SubmoduleState, showing: Showing) -> str:
    """What stands where a commit's details would, when they cannot be read."""
    if state.problem == git_ops.NOT_CHECKED_OUT:
        return "(not checked out)"
    if showing is Showing.CHECKED_OUT and state.problem:
        return f"(git: {state.problem.splitlines()[0]})"
    if showing is Showing.RECORDED and not state.recorded_hash:
        return "(HEAD records no commit for it)"
    return "(its checkout does not have this commit)"


def _described(label: str, commit: CommitSummary | None, full_hash: str) -> list[str]:
    if commit is None:
        return [f"{label}: {full_hash}"] if full_hash else []
    lines = [
        f"{label}: {commit.hash}",
        f"    {commit.subject}",
        f"    Committed {commit.date}, authored by {commit.author} on {commit.authored}",
    ]
    if commit.tags:
        lines.append(f"    Tagged {', '.join(commit.tags)}")
    return lines


def _tooltip(state: SubmoduleState) -> str:
    """Everything known about a submodule's commits, both of them."""
    lines = [state.path]
    if state.name != state.path:
        lines.append(f"Called {state.name} in .gitmodules")
    if state.url:
        lines.append(f"Fetched from {state.url}")
    if state.checked_out is not None:
        lines += _described("Checked out", state.checked_out, "")
    elif state.problem == git_ops.NOT_CHECKED_OUT:
        lines.append("Not checked out")
    elif state.problem:
        lines.append(f"Checked out: git could not read it - {state.problem}")
    if not state.recorded_hash:
        lines.append("HEAD records no commit for it")
    elif state.checked_out is None or state.checked_out.hash != state.recorded_hash:
        lines += _described("Recorded in HEAD", state.recorded, state.recorded_hash)
    else:
        lines.append("Recorded in HEAD: the same commit")
    return "\n".join(lines)


def _item(
    state: SubmoduleState | None, other: SubmoduleState | None, showing: Showing, match: Match
) -> QTreeWidgetItem:
    """One side of a row: the submodule as that side has it, or a note that it has none.

    A side without it still names it, greyed: each list stays one that can be read
    down by name, and the gap is where the other side's submodule is.
    """
    item = QTreeWidgetItem([""] * len(COLUMNS))
    if state is None:
        item.setText(PATH, other.path if other is not None else "")
        item.setText(MESSAGE, "(not in this repository)")
        font = item.font(PATH)
        font.setItalic(True)
        for column in (PATH, MESSAGE):
            item.setForeground(column, MUTED_COLOUR)
            item.setFont(column, font)
            item.setToolTip(column, "This repository has no such submodule.")
        return item
    item.setText(PATH, state.path)
    item.setText(COMMIT, _commit_text(state, showing))
    item.setData(PATH, HASH_ROLE, compare.compared_hash(state, showing))
    commit = compare.shown_commit(state, showing)
    if commit is not None:
        item.setText(TAG, ", ".join(commit.tags))
        item.setText(DATE, commit.date)
        item.setText(MESSAGE, commit.subject)
    else:
        item.setText(MESSAGE, _why_no_details(state, showing))
        item.setForeground(MESSAGE, MUTED_COLOUR)
    if match is Match.DIFFERENT:
        for column in (COMMIT, TAG, DATE):
            item.setForeground(column, DIFFERENT_COLOUR)
        if commit is not None:
            item.setForeground(MESSAGE, DIFFERENT_COLOUR)
    elif match is not Match.SAME:
        item.setForeground(PATH, DIFFERENT_COLOUR)
    tip = _tooltip(state)
    for column in range(len(COLUMNS)):
        item.setToolTip(column, tip)
    return item


class _SideTree(QTreeWidget):
    """One side's submodules.

    The two sides scroll as one list, so each keeps a scroll bar across its foot
    whether it needs one or not: a bar on one side only would leave that side a
    row shorter than the other, and the rows out of line at the bottom.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setHeaderLabels(list(COLUMNS))
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        for column, width in ((PATH, 200), (COMMIT, 90), (TAG, 110), (DATE, 115)):
            self.setColumnWidth(column, width)


class ComparePanel(QWidget):
    """Two repositories' submodules, a list each, row beside row."""

    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        #: The window's shared progress bar, set by the window that owns this.
        self.busy = None
        #: How many reads have been asked for. An answer to any but the last is
        #: about repositories no longer on screen, and is dropped.
        self._asked = 0
        self._reading = False
        self._rows: list[Row] = []
        #: The two repositories the rows are of, which the rows and titles name.
        self._shown: tuple[str, str] = ("", "")
        self._following = False

        # The active repository, chosen as on every other tab, and the other one.
        self.repo_picker = RepoPicker(settings)
        self.repo_picker.repoChanged.connect(self._reload)
        self.other_picker = OtherRepoPicker(settings)
        self.other_picker.repoChanged.connect(self._reload)

        self.repo_pane = RepoPane(self.repo_picker, margins=(0, 0, SECTION_GAP, 0))
        self.other_picker.title_label.setVisible(False)
        self.other_page = self.repo_pane.add_page(self.other_picker, COMPARE_WITH_TAB)
        if not self.other_picker.current_path():
            # Nothing to compare with yet, and the list to choose it from is the
            # one thing on this tab worth opening.
            self.repo_pane.tabs.setCurrentIndex(self.other_page)
            self.repo_pane.set_open(True)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(SECTION_GAP, 0, 0, 0)

        top = QHBoxLayout()
        heading = QLabel("Submodules")
        font = heading.font()
        font.setBold(True)
        heading.setFont(font)
        top.addWidget(heading)
        top.addStretch(1)
        top.addWidget(QLabel("Compare:"))
        self.showing_combo = QComboBox()
        self.showing_combo.addItem("Checked-out commits", Showing.CHECKED_OUT)
        self.showing_combo.setItemData(
            0,
            "The commit each submodule has checked out on disk, which is what "
            "git submodule status names.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.showing_combo.addItem("Commits recorded in HEAD", Showing.RECORDED)
        self.showing_combo.setItemData(
            1,
            "The commit each repository's last commit records for each submodule, "
            "whatever is checked out.",
            Qt.ItemDataRole.ToolTipRole,
        )
        self.showing_combo.currentIndexChanged.connect(self._render)
        top.addWidget(self.showing_combo)
        self.differences_check = QCheckBox("Only differences")
        self.differences_check.setToolTip(
            "Hide the submodules both repositories have at the same commit."
        )
        self.differences_check.toggled.connect(self._render)
        top.addWidget(self.differences_check)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setToolTip("Read both repositories' submodules again.")
        self.refresh_btn.clicked.connect(self._reload)
        top.addWidget(self.refresh_btn)
        layout.addLayout(top)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.left_title, self.left_tree = QLabel(), _SideTree()
        self.right_title, self.right_tree = QLabel(), _SideTree()
        sides = QSplitter(Qt.Orientation.Horizontal)
        for title, tree in (
            (self.left_title, self.left_tree),
            (self.right_title, self.right_tree),
        ):
            title.setTextFormat(Qt.TextFormat.RichText)
            side = QWidget()
            box = QVBoxLayout(side)
            box.setContentsMargins(0, 0, 0, 0)
            box.addWidget(title)
            box.addWidget(tree, 1)
            sides.addWidget(side)
        layout.addWidget(sides, 1)

        # One list in two halves: scrolling or selecting in either moves both.
        for tree, other in (
            (self.left_tree, self.right_tree),
            (self.right_tree, self.left_tree),
        ):
            tree.verticalScrollBar().valueChanged.connect(other.verticalScrollBar().setValue)
            tree.currentItemChanged.connect(
                lambda current, _previous, other=other: self._follow(current, other)
            )
            tree.customContextMenuRequested.connect(
                lambda point, tree=tree: self._on_menu(tree, point)
            )

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(INFO_COLOUR)
        layout.addWidget(self.status)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.repo_pane)
        splitter.addWidget(content)
        splitter.setStretchFactor(1, 1)
        side_panel_mod.attach(splitter, self.repo_pane, open_sizes=[240, 960])

        outer = QVBoxLayout(self)
        outer.addWidget(splitter)
        self._render()

    # ---- reading ----------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # Read when first looked at, not when the window is built: that costs a
        # git command a submodule, and the tab may never be opened. Once the
        # events in hand are handled, because a tab is shown before the window
        # hears it was switched to -- and the window's refresh reads it too.
        if not self._asked:
            QTimer.singleShot(0, self._read_if_never_read)

    def _read_if_never_read(self) -> None:
        if not self._asked:
            self._reload()

    def refresh_repos(self) -> None:
        """Reload both lists -- another tab may have added to them -- and read again."""
        self.repo_picker.refresh()
        self.other_picker.refresh()
        self._reload()

    def _reload(self, *_args) -> None:
        left = self.repo_picker.current_path()
        right = self.other_picker.current_path()
        self._asked += 1
        asked = self._asked
        if not left or not right:
            self._reading = False
            self._busy_stop()
            self._rows = []
            self._shown = (left, right)
            self.status.setText("")
            self._render()
            return

        def read():
            try:
                return asked, left, right, compare.read_both(left, right), ""
            except Exception as exc:  # noqa: BLE001 - said on screen, not lost in a thread
                return asked, left, right, None, str(exc) or type(exc).__name__

        self._reading = True
        self.status.setStyleSheet(INFO_COLOUR)
        self.status.setText(READING)
        self._busy_start()
        worker = FunctionWorker(read)
        worker.finished.connect(self._on_read)
        run_worker(worker)

    def _on_read(self, outcome) -> None:
        asked, left, right, sides, problem = outcome
        if asked != self._asked:
            return
        self._reading = False
        self._busy_stop()
        if sides is None:
            self.status.setStyleSheet(WARN_COLOUR)
            self.status.setText(f"Could not read the submodules: {problem}")
            return
        self.status.setText("")
        self._rows = compare.pair(*sides)
        self._shown = (left, right)
        self._render()
        self._fit_columns()

    # ---- what is on screen -------------------------------------------------------------
    def _showing(self) -> Showing:
        return self.showing_combo.currentData() or Showing.CHECKED_OUT

    def _render(self, *_args) -> None:
        showing = self._showing()
        left, right = self._shown
        left_name, right_name = _name(self.settings, left), _name(self.settings, right)
        self.left_title.setText(_title(left_name, left))
        self.left_title.setToolTip(left)
        self.right_title.setText(_title(right_name, right))
        self.right_title.setToolTip(right)

        hide_same = self.differences_check.isChecked()
        for tree in (self.left_tree, self.right_tree):
            tree.clear()
        for row in self._rows:
            match = row.match(showing)
            for tree, state, other in (
                (self.left_tree, row.left, row.right),
                (self.right_tree, row.right, row.left),
            ):
                item = _item(state, other, showing, match)
                tree.addTopLevelItem(item)
                item.setHidden(hide_same and match is Match.SAME)
        self.summary.setText(self._summary(showing, left_name, right_name))

    def _summary(self, showing: Showing, left_name: str, right_name: str) -> str:
        left, right = self._shown
        if self._reading and not self._rows:
            return ""
        if not self.repo_picker.current_path():
            return "Choose a repository under Repository, on the left."
        if not self.other_picker.current_path():
            return "Choose a repository to compare it with, under Compare with on the left."
        same_repository = left == right
        if not self._rows:
            if same_repository:
                return f"{left_name} has no submodules."
            return f"Neither {left_name} nor {right_name} has submodules."
        counts = Counter(row.match(showing) for row in self._rows)
        parts = [
            f"{counts[Match.SAME]} the same" if counts[Match.SAME] else "",
            f"{counts[Match.DIFFERENT]} different" if counts[Match.DIFFERENT] else "",
            f"{counts[Match.ONLY_LEFT]} only in {left_name}" if counts[Match.ONLY_LEFT] else "",
            f"{counts[Match.ONLY_RIGHT]} only in {right_name}" if counts[Match.ONLY_RIGHT] else "",
        ]
        total = len(self._rows)
        text = f"{total} submodule{'' if total == 1 else 's'}: {', '.join(p for p in parts if p)}."
        if same_repository:
            text += " Both sides are the same repository."
        return text

    def _fit_columns(self) -> None:
        """Each column as wide as its widest cell on either side, and the same on both.

        The same, so that the two lists line up as one; fitted when rows are read
        rather than on every redraw, so a column somebody widened stays widened
        while they filter.
        """
        trees = (self.left_tree, self.right_tree)
        for column, widest in ((PATH, 320), (COMMIT, 280), (TAG, 240), (DATE, 140)):
            wanted = max(
                max(tree.sizeHintForColumn(column), tree.header().sectionSizeHint(column))
                for tree in trees
            )
            for tree in trees:
                tree.setColumnWidth(column, min(widest, wanted + 12))

    def _follow(self, current: QTreeWidgetItem | None, other: QTreeWidget) -> None:
        if self._following or current is None:
            return
        index = current.treeWidget().indexOfTopLevelItem(current)
        self._following = True
        try:
            other.setCurrentItem(other.topLevelItem(index))
        finally:
            self._following = False

    def _on_menu(self, tree: QTreeWidget, point) -> None:
        item = tree.itemAt(point)
        commit = item.data(PATH, HASH_ROLE) if item is not None else ""
        if not commit:
            return
        menu = QMenu(tree)
        copy = menu.addAction("Copy commit hash")
        if menu.exec(tree.viewport().mapToGlobal(point)) is copy:
            QGuiApplication.clipboard().setText(commit)

    def _busy_start(self) -> None:
        if self.busy is not None:
            self.busy.start(self, "Reading submodules")

    def _busy_stop(self) -> None:
        if self.busy is not None:
            self.busy.stop(self)
