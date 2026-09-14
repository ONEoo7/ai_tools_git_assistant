"""Look at each change and stage the ones you mean: the Commit tab's staging window.

Laid out the way Git Extensions lays out its commit window. What is not staged
yet is on top, what is staged is below it, and the diff of whichever file is
selected sits beside both. A file moves between the lists whole -- with the
buttons, or a double-click -- or a hunk or a few lines at a time, from the diff.

What is not staged is listed under its folders, folded to begin with: a working
tree with changes in forty files is forty rows otherwise, and the folder is
usually the unit being thought about. Selecting a folder stages everything in
it. A new file is marked + and a removed one -, the way a diff marks a line
doing the same; git's own "?" asks a question rather than saying what changed.

**Apply .gitattributes**, on by default, rewrites a file's line endings to what
.gitattributes declares before anything of that file is staged: the Commit
tab's Normalize, one file at a time. Git already cleans what goes into the
index; what this changes is the file left on disk, so that it and the index stop
disagreeing about line endings -- which is otherwise the change that never goes
away. It is safe mid-selection: normalizing does not change what git diffs, so
lines chosen before it are still the same lines after it.

The diff shows its whitespace the way Git Extensions shows it: every line ends
in a quiet ``\\n`` or ``\\r\\n``, and spaces and tabs are drawn as dots and
arrows. It is still line for line as git wrote it -- a line on screen is a line
of the patch -- so the endings are drawn after the text rather than as line
breaks of their own, and the Unicode separators that would split a line in two
are drawn as a symbol.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass

from PyQt6.QtCore import QItemSelectionModel, Qt
from PyQt6.QtGui import (
    QColor,
    QFontDatabase,
    QPalette,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextOption,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import git_ops, staging
from git_assistant.git_ops import StatusEntry

UNSTAGED_TITLE = "Unstaged changes"
STAGED_TITLE = "Staged changes"
APPLY_ATTRIBUTES = "Apply .gitattributes"
NORMALIZE = "Normalize Line Endings"

#: Past this a diff is not drawn. A generated file can change by megabytes, and
#: the window would stall painting it; the file still stages whole.
MAX_SHOWN = 2_000_000

#: How each line of the diff ends, drawn at its end as Git Extensions draws it:
#: the escape a programmer would write. Built from a backslash spelled out, so
#: that no escape in this file can be misread for the thing it draws.
_BACKSLASH = chr(92)
LF_MARK = _BACKSLASH + "n"
CRLF_MARK = _BACKSLASH + "r" + _BACKSLASH + "n"
#: A carriage return that ends no line: inside one, or last in a file with no
#: final newline.
CR_MARK = _BACKSLASH + "r"
_SEPARATORS = (chr(0x2028), chr(0x2029))

#: Spaces and tabs, which Qt draws as dots and arrows once asked to.
_WHITESPACE_RE = re.compile(r"[ \t]+")
SEPARATOR_MARK = "\u2424"  # the symbol for a newline, for U+2028 and U+2029

#: Diff colours for a dark list and a light one: GitHub's, which are chosen to
#: read on exactly those two.
#: "ws" is for what is drawn rather than written: line endings, dots and arrows.
_ON_DARK = {
    "+": "#56d364", "-": "#f85149", "@": "#79c0ff", "head": "#8b949e", "ws": "#6e7681",
}
_ON_LIGHT = {
    "+": "#116329", "-": "#cf222e", "@": "#0550ae", "head": "#57606a", "ws": "#8c959f",
}

#: The lines git writes above a diff's hunks, drawn as header rather than content.
_HEADER_LINES = (
    "diff --git",
    "index ",
    "--- ",
    "+++ ",
    "new file mode",
    "deleted file mode",
    "old mode",
    "new mode",
    "similarity index",
    "rename from",
    "rename to",
    "Binary files",
)

#: Where a list keeps the `StatusEntry` behind a row.
_ENTRY = Qt.ItemDataRole.UserRole
#: Where a folder row in the unstaged list keeps its path from the repository root.
_FOLDER = Qt.ItemDataRole.UserRole + 1

#: git's status letters, except the two for a whole file arriving or leaving,
#: which are drawn the way a diff draws a line doing the same. An untracked file
#: is one arriving too -- "?" is git asking, not describing.
_MARKERS = {"?": "+", "A": "+", "D": "-"}


def marker(entry: StatusEntry, *, staged: bool) -> str:
    """The one-character status shown beside a file in either list."""
    if staged:
        letter = entry.index
    elif entry.untracked:
        letter = "?"
    elif entry.unmerged:
        letter = "U"
    else:
        letter = entry.worktree
    return _MARKERS.get(letter, letter)


#: git's words for how a copy of a file ends its lines, as a person says them.
_EOL_WORDS = {
    "lf": "LF",
    "crlf": "CRLF",
    "mixed": "mixed LF and CRLF",
    "none": "no line breaks",
    "-text": "binary",
}
#: The same, short enough for a badge beside a file name.
_SHORT_WORDS = {"lf": "LF", "crlf": "CRLF", "mixed": "mixed", "none": "none"}
_ARROW = chr(0x2192)  # a rightwards arrow, spelled out so no escape is misread
_EOL_STYLE = "color: #888;"
#: The amber the bar above the tabs warns in.
_OFF_COLOUR = "#b36b00"
_OFF_STYLE = f"color: {_OFF_COLOUR};"


def describe_line_endings(endings: git_ops.LineEndings) -> tuple[str, bool]:
    """One line on how a file ends its lines, and whether that is off.

    Off is the file on disk ending its lines otherwise than .gitattributes says
    outright: what Apply .gitattributes, or the Commit tab's Normalize, changes.
    A rule that leaves the ending to the machine (``text`` with no ``eol``) names
    nothing to be off from.
    """
    disk = _EOL_WORDS.get(endings.worktree, endings.worktree) or "(deleted)"
    index = _EOL_WORDS.get(endings.index, endings.index) or "(new file)"
    rule = endings.attributes or "(no rule)"
    text = (
        f"Line endings - working tree: {disk} | index: {index} | "
        f".gitattributes: {rule}"
    )
    wanted = endings.declared
    off = (
        bool(wanted)
        and endings.worktree in ("lf", "crlf", "mixed")
        and endings.worktree != wanted
    )
    if off:
        text += f" - .gitattributes wants {wanted.upper()}"
    return text, off


@contextmanager
def _waiting():
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    try:
        yield
    finally:
        QApplication.restoreOverrideCursor()


def _whole_file_only(entry: StatusEntry) -> bool:
    """Entries git offers no partial patch for: new, renamed, conflicted, submodules."""
    return entry.untracked or entry.submodule or entry.unmerged or bool(entry.orig_path)


def diff_colours() -> dict[str, str]:
    """The diff colours for the theme in force: the dark set on a dark list."""
    base = QApplication.palette().color(QPalette.ColorRole.Base)
    return _ON_DARK if base.lightness() < 128 else _ON_LIGHT


@dataclass
class ShownDiff:
    """A diff as drawn: its text, and where in each line the endings are drawn."""

    text: str
    #: Per line, the ``(start, length)`` of every ending drawn into it.
    marks: list[list[tuple[int, int]]]


def shown_diff(raw: bytes) -> ShownDiff:
    """A diff with exactly one line on screen per line of it, and its endings drawn.

    A diff's own lines all end in a newline, whatever the file does. So the line
    before git's "\\ No newline at end of file" shows no ending -- that newline is
    git's, not the file's -- and neither does the notice itself.
    """
    lines = raw.split(b"\n")
    ended = bool(lines) and lines[-1] == b""
    if ended:
        lines.pop()
    texts: list[str] = []
    marks: list[list[tuple[int, int]]] = []
    for number, line in enumerate(lines):
        spans: list[tuple[int, int]] = []
        crlf = line.endswith(b"\r")
        text = ""
        for at, piece in enumerate((line[:-1] if crlf else line).split(b"\r")):
            if at:  # a carriage return inside the line, drawn where it is
                spans.append((len(text), len(CR_MARK)))
                text += CR_MARK
            text += piece.decode("utf-8", errors="replace")
        for separator in _SEPARATORS:
            text = text.replace(separator, SEPARATOR_MARK)
        last = number == len(lines) - 1
        notice_follows = not last and lines[number + 1][:1] == b"\\"
        if line[:1] == b"\\":
            ending = ""
        elif (ended or not last) and not notice_follows:
            ending = CRLF_MARK if crlf else LF_MARK
        else:
            ending = CR_MARK if crlf else ""
        if ending:
            spans.append((len(text), len(ending)))
            text += ending
        texts.append(text)
        marks.append(spans)
    return ShownDiff("\n".join(texts), marks)


class _DiffColours(QSyntaxHighlighter):
    """Added, removed and hunk lines, in colours that read on either theme."""

    def __init__(self, document) -> None:
        super().__init__(document)
        #: Where the endings are in each line; set before the text is.
        self.marks: list[list[tuple[int, int]]] = []

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt naming
        colours = diff_colours()
        if text.startswith(_HEADER_LINES):
            key = "head"
        elif text.startswith("@@"):
            key = "@"
        elif text[:1] in ("+", "-"):
            key = text[:1]
        else:
            key = ""
        if key:
            line = QTextCharFormat()
            line.setForeground(QColor(colours[key]))
            self.setFormat(0, len(text), line)
        # Qt draws a visible space or tab in the colour of the character it
        # stands for, so giving those characters the quiet colour is what keeps
        # the dots and arrows in the background. On git's own header lines,
        # which are not the file's content, Git Extensions draws none -- and
        # the flag that asks for them is the whole document's, so there they
        # are drawn in the colour of the page instead.
        quiet = QTextCharFormat()
        quiet.setForeground(QColor(colours["ws"]))
        spaces = QTextCharFormat()
        if key == "head":
            spaces.setForeground(QApplication.palette().color(QPalette.ColorRole.Base))
        else:
            spaces = quiet
        for run in _WHITESPACE_RE.finditer(text):
            self.setFormat(run.start(), run.end() - run.start(), spaces)
        number = self.currentBlock().blockNumber()
        if 0 <= number < len(self.marks):
            for start, length in self.marks[number]:
                self.setFormat(start, length, quiet)


class StagingDialog(QDialog):
    """Stage and unstage a repository's changes: files, hunks or lines."""

    def __init__(self, repo: str, *, name: str = "", parent=None) -> None:
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle(f"Stage changes - {name or repo}")
        self.setMinimumSize(1000, 600)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
        )
        #: The file whose diff is showing, and from which list.
        self._shown: tuple[StatusEntry, bool] | None = None
        #: That diff taken apart, when parts of it can be staged.
        self._diff: staging.FileDiff | None = None
        #: The unstaged files .gitattributes would rewrite the line endings of,
        #: as of the last reload.
        self._eol_changes: dict[str, git_ops.EndingChange] = {}

        # ---- the two lists ----------------------------------------------------
        self.unstaged_title = QLabel(UNSTAGED_TITLE)
        self.unstaged_list = self._make_list(folders=True)
        self.stage_btn = QPushButton("Stage")
        self.stage_btn.setToolTip(
            "Stage the selected files. Double-clicking one does too."
        )
        self.stage_all_btn = QPushButton("Stage all")
        self.stage_btn.clicked.connect(
            lambda: self.stage(self._selected(self.unstaged_list))
        )
        self.stage_all_btn.clicked.connect(
            lambda: self.stage(self._entries(self.unstaged_list))
        )
        self.normalize_btn = QPushButton(NORMALIZE)
        self.normalize_btn.setToolTip(
            "Rewrite the line endings of every unstaged file marked with a change, "
            "to what .gitattributes declares.\n"
            "Nothing is staged, and files .gitattributes says nothing about are "
            "left alone. Asks first."
        )
        self.normalize_btn.clicked.connect(self.normalize_line_endings)

        self.staged_title = QLabel(STAGED_TITLE)
        self.staged_list = self._make_list()
        self.unstage_btn = QPushButton("Unstage")
        self.unstage_btn.setToolTip(
            "Unstage the selected files. Double-clicking one does too."
        )
        self.unstage_all_btn = QPushButton("Unstage all")
        self.unstage_btn.clicked.connect(
            lambda: self.unstage(self._selected(self.staged_list))
        )
        self.unstage_all_btn.clicked.connect(
            lambda: self.unstage(self._entries(self.staged_list))
        )

        for tree, staged in ((self.unstaged_list, False), (self.staged_list, True)):
            tree.currentItemChanged.connect(
                lambda current, _previous, s=staged: self._on_current(current, s)
            )
            tree.itemSelectionChanged.connect(self._enable_buttons)
            tree.itemDoubleClicked.connect(
                lambda item, _column, s=staged: self._on_double_click(item, s)
            )

        lists = QSplitter(Qt.Orientation.Vertical)
        lists.addWidget(
            self._list_pane(self.unstaged_title, self.unstaged_list)
        )
        lists.widget(0).layout().addLayout(
            self._buttons(self.stage_btn, self.stage_all_btn, self.normalize_btn)
        )
        lists.addWidget(self._list_pane(self.staged_title, self.staged_list))
        lists.widget(1).layout().addLayout(
            self._buttons(self.unstage_btn, self.unstage_all_btn)
        )

        # ---- the diff -----------------------------------------------------------
        self.diff_title = QLabel("")
        #: The selected file's line endings. Its own line under the title, which
        #: already runs long with a path in it.
        self.line_endings = QLabel("")
        self.line_endings.setWordWrap(True)
        self.line_endings.setVisible(False)
        self.diff_view = QPlainTextEdit()
        self.diff_view.setReadOnly(True)
        self.diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff_view.setFont(
            QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        )
        self.diff_view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.diff_view.customContextMenuRequested.connect(self._on_diff_menu)
        self.diff_view.setToolTip(
            "Select lines and right-click to stage just those, or right-click in a "
            "hunk to stage all of it."
        )
        self._colours = _DiffColours(self.diff_view.document())
        diff_pane = QWidget()
        diff_box = QVBoxLayout(diff_pane)
        diff_box.setContentsMargins(0, 0, 0, 0)
        diff_box.addWidget(self.diff_title)
        diff_box.addWidget(self.line_endings)
        diff_box.addWidget(self.diff_view, 1)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.addWidget(lists)
        body.addWidget(diff_pane)
        body.setStretchFactor(0, 2)
        body.setStretchFactor(1, 5)

        # ---- the bottom row -----------------------------------------------------
        self.apply_attributes = QCheckBox(APPLY_ATTRIBUTES)
        self.apply_attributes.setChecked(True)
        self.apply_attributes.setToolTip(
            "Before staging a file, rewrite its line endings on disk to what "
            ".gitattributes declares for it. Files it declares nothing about are "
            "left alone."
        )
        self.status = QLabel("")
        self.status.setWordWrap(True)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)

        bottom = QHBoxLayout()
        bottom.addWidget(self.apply_attributes)
        bottom.addWidget(self.status, 1)
        bottom.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addWidget(body, 1)
        layout.addLayout(bottom)

        self.resize(1300, 760)
        self.reload()

    # ---- building ---------------------------------------------------------------
    @staticmethod
    def _make_list(*, folders: bool = False) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setHeaderHidden(True)
        # The unstaged list has a third column: how .gitattributes would rewrite
        # a file's line endings. Staged files are past that.
        tree.setColumnCount(3 if folders else 2)
        tree.setRootIsDecorated(folders)
        # Folders nest in the name column, so the markers stay in a straight one.
        tree.setTreePosition(1)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        header = tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        if folders:
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        tree.setColumnWidth(0, 28)
        return tree

    @staticmethod
    def _list_pane(title: QLabel, tree: QTreeWidget) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(title)
        box.addWidget(tree, 1)
        return pane

    @staticmethod
    def _buttons(*buttons: QPushButton) -> QHBoxLayout:
        row = QHBoxLayout()
        for button in buttons:
            row.addWidget(button)
        row.addStretch(1)
        return row

    # ---- what is in the lists ----------------------------------------------------
    @staticmethod
    def _rows(tree: QTreeWidget, *, visible: bool = False) -> list[QTreeWidgetItem]:
        """Every row, folders before what is in them.

        ``visible`` stops at folded folders: the rows a person can see.
        """
        rows: list[QTreeWidgetItem] = []

        def walk(item: QTreeWidgetItem) -> None:
            rows.append(item)
            if visible and not item.isExpanded():
                return
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(tree.topLevelItemCount()):
            walk(tree.topLevelItem(i))
        return rows

    @classmethod
    def _entries(cls, tree: QTreeWidget) -> list[StatusEntry]:
        """Every file in a list, however deep in folders."""
        return [e for row in cls._rows(tree) if (e := row.data(1, _ENTRY)) is not None]

    @classmethod
    def _visible_files(cls, tree: QTreeWidget) -> list[QTreeWidgetItem]:
        return [
            row
            for row in cls._rows(tree, visible=True)
            if row.data(1, _ENTRY) is not None
        ]

    @staticmethod
    def _selected(tree: QTreeWidget) -> list[StatusEntry]:
        """The selected files, and every file in a selected folder -- each once."""
        picked: list[StatusEntry] = []

        def walk(item: QTreeWidgetItem, inside: bool) -> None:
            chosen = inside or item.isSelected()
            entry = item.data(1, _ENTRY)
            if entry is not None and chosen:
                picked.append(entry)
            for i in range(item.childCount()):
                walk(item.child(i), chosen)

        for i in range(tree.topLevelItemCount()):
            walk(tree.topLevelItem(i), False)
        return picked

    @classmethod
    def _selection(cls, tree: QTreeWidget) -> tuple[set[str], set[str]]:
        """The rows selected as such: ``(file paths, folder paths)``."""
        files = {e.path for row in tree.selectedItems() if (e := row.data(1, _ENTRY))}
        folders = {f for row in tree.selectedItems() if (f := row.data(1, _FOLDER))}
        return files, folders

    @classmethod
    def _find(cls, tree: QTreeWidget, path: str) -> QTreeWidgetItem | None:
        for row in cls._rows(tree):
            entry = row.data(1, _ENTRY)
            if entry is not None and entry.path == path:
                return row
        return None

    def reload(self) -> None:
        """Read the changes again, keeping the selection and open folders by path."""
        unstaged_files, unstaged_folders = self._selection(self.unstaged_list)
        staged_files, _ = self._selection(self.staged_list)
        opened = {
            folder
            for row in self._rows(self.unstaged_list)
            if (folder := row.data(1, _FOLDER)) and row.isExpanded()
        }
        shown = self._shown
        position = 0
        if shown is not None:
            rows = self._visible_files(self._list_for(shown[1]))
            paths = [row.data(1, _ENTRY).path for row in rows]
            if shown[0].path in paths:
                position = paths.index(shown[0].path)
        scroll = self.diff_view.verticalScrollBar().value()
        try:
            entries = git_ops.status_entries(self.repo)
        except git_ops.GitError as exc:
            entries = []
            self.status.setText(str(exc))
        unstaged = [e for e in entries if e.unstaged]
        staged = [e for e in entries if e.staged]
        self._eol_changes = self._line_ending_changes(unstaged)
        self._fill_folders(unstaged, unstaged_files, unstaged_folders, opened)
        self._fill_flat(staged, staged_files)
        self.unstaged_title.setText(f"{UNSTAGED_TITLE} ({len(unstaged)})")
        self.staged_title.setText(f"{STAGED_TITLE} ({len(staged)})")

        # The same file again if it is still where it was -- lines staged a few
        # at a time leave it there -- scrolled where it was. If it moved, the one
        # that took its place, the way a list gets worked through. Never a file
        # inside a folded folder: a selection nobody can see is one that "Stage"
        # would act on without anyone knowing.
        again = None
        if shown is not None:
            tree = self._list_for(shown[1])
            found = self._find(tree, shown[0].path)
            visible = self._visible_files(tree)
            if found is not None and found in visible:
                again = found
            elif visible:
                again = visible[min(position, len(visible) - 1)]
        if again is None:
            visible = self._visible_files(self.unstaged_list)
            if visible:
                again = visible[0]
            elif not unstaged:
                # Nothing left to stage, so what is staged is what to look at.
                staged_rows = self._visible_files(self.staged_list)
                again = staged_rows[0] if staged_rows else None
        if again is None:
            self._show(None, False)
            if entries:
                self.diff_title.setText("Select a file to see its diff.")
        else:
            tree = again.treeWidget()
            entry = again.data(1, _ENTRY)
            tree.blockSignals(True)
            tree.setCurrentItem(again, 1, QItemSelectionModel.SelectionFlag.NoUpdate)
            if not tree.selectedItems():
                again.setSelected(True)
            tree.blockSignals(False)
            self._show(entry, tree is self.staged_list)
            if shown is not None and entry.path == shown[0].path:
                self.diff_view.verticalScrollBar().setValue(scroll)
        self._enable_buttons()

    def _list_for(self, staged: bool) -> QTreeWidget:
        return self.staged_list if staged else self.unstaged_list

    def _line_ending_changes(
        self, entries: list[StatusEntry]
    ) -> dict[str, git_ops.EndingChange]:
        """Which unstaged files applying .gitattributes would rewrite, and how."""
        paths = [e.path for e in entries if not e.submodule and e.worktree != "D"]
        try:
            return git_ops.line_ending_changes(self.repo, paths)
        except git_ops.GitError:
            return {}  # git would not say; the diff says why

    def _mark_change(
        self, row: QTreeWidgetItem, change: git_ops.EndingChange | None, count: int = 0
    ) -> None:
        """Put the line-ending badge on a file's row, or a folder's count on its."""
        if change is None and not count:
            return
        if change is not None:
            before = _SHORT_WORDS.get(change.now, change.now)
            after = _SHORT_WORDS.get(change.becomes, change.becomes)
            row.setText(2, f"{before} {_ARROW} {after}")
            row.setToolTip(
                2,
                f".gitattributes rewrites this file's line endings from {before} to "
                f"{after}: {NORMALIZE} does it now, and {APPLY_ATTRIBUTES} does it "
                "as the file is staged.",
            )
        else:
            row.setText(2, f"{count} to normalize")
            row.setToolTip(
                2,
                f"{count} file(s) in here have line endings .gitattributes rewrites.",
            )
        row.setForeground(2, QColor(_OFF_COLOUR))

    @staticmethod
    def _file_row(entry: StatusEntry, *, staged: bool, name: str) -> QTreeWidgetItem:
        mark = marker(entry, staged=staged)
        row = QTreeWidgetItem([mark, name])
        if mark in ("+", "-"):
            # In the colours the diff beside it gives an added or removed line.
            # Worked out when the list is filled: the window is modal, so the
            # theme cannot change underneath it. Bold and a little larger, because
            # a plus and a minus are the two smallest marks there are, and colour
            # on a hairline is colour nobody sees.
            row.setForeground(0, QColor(diff_colours()[mark]))
            font = row.font(0)
            font.setBold(True)
            font.setPointSizeF(font.pointSizeF() + 2)
            row.setFont(0, font)
        row.setData(1, _ENTRY, entry)
        tip = entry.path
        if entry.orig_path:
            tip += f"\nRenamed from {entry.orig_path}"
        if entry.submodule:
            tip += "\nA submodule: staging it records the commit it is at."
        row.setToolTip(1, tip)
        return row

    def _fill_flat(self, entries: list[StatusEntry], kept: set[str]) -> None:
        tree = self.staged_list
        tree.blockSignals(True)
        tree.clear()
        for entry in entries:
            row = self._file_row(entry, staged=True, name=entry.path)
            tree.addTopLevelItem(row)
            row.setSelected(entry.path in kept)
        tree.blockSignals(False)

    def _fill_folders(
        self,
        entries: list[StatusEntry],
        kept_files: set[str],
        kept_folders: set[str],
        opened: set[str],
    ) -> None:
        """The unstaged files under their folders, folders first, each by name.

        A folder is open only if it was open before: folded is how the list
        starts, and opening one is a choice that has to survive every stage.
        """
        tree = self.unstaged_list
        tree.blockSignals(True)
        tree.clear()
        root: dict = {}  # folder name -> its own dict; the files under None
        for entry in entries:
            *folders, _name = entry.path.split("/")
            node = root
            for folder in folders:
                node = node.setdefault(folder, {})
            node.setdefault(None, []).append(entry)
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon)

        def place(row: QTreeWidgetItem, parent: QTreeWidgetItem | None) -> None:
            if parent is None:
                tree.addTopLevelItem(row)
            else:
                parent.addChild(row)

        def emit(node: dict, parent: QTreeWidgetItem | None, prefix: str) -> int:
            """Add a folder's contents; returns how many of them to normalize."""
            to_normalize = 0
            for name in sorted((k for k in node if k is not None), key=str.casefold):
                path = prefix + name
                row = QTreeWidgetItem(["", name])
                row.setData(1, _FOLDER, path)
                row.setIcon(1, icon)
                row.setToolTip(1, f"{path}/")
                place(row, parent)
                inside = emit(node[name], row, path + "/")
                # Folded is how a folder starts, so what is in it has to show on it.
                self._mark_change(row, None, inside)
                to_normalize += inside
                row.setExpanded(path in opened)
                row.setSelected(path in kept_folders)
            for entry in sorted(node.get(None, []), key=lambda e: e.path.casefold()):
                name = entry.path.rsplit("/", 1)[-1]
                row = self._file_row(entry, staged=False, name=name)
                change = self._eol_changes.get(entry.path)
                self._mark_change(row, change)
                to_normalize += change is not None
                place(row, parent)
                row.setSelected(entry.path in kept_files)
            return to_normalize

        emit(root, None, "")
        tree.blockSignals(False)

    def _enable_buttons(self) -> None:
        self.stage_btn.setEnabled(bool(self._selected(self.unstaged_list)))
        self.stage_all_btn.setEnabled(bool(self._entries(self.unstaged_list)))
        self.unstage_btn.setEnabled(bool(self._selected(self.staged_list)))
        self.unstage_all_btn.setEnabled(bool(self._entries(self.staged_list)))
        self.normalize_btn.setEnabled(bool(self._eol_changes))

    # ---- the diff ----------------------------------------------------------------
    def show_file(self, path: str, *, staged: bool = False) -> None:
        """Select ``path`` in one of the lists and show its diff."""
        tree = self._list_for(staged)
        item = self._find(tree, path)
        if item is None:
            raise KeyError(path)
        # Opening its folders is the point of asking for it by name.
        parent = item.parent()
        while parent is not None:
            parent.setExpanded(True)
            parent = parent.parent()
        tree.setCurrentItem(item)

    def _on_current(self, item: QTreeWidgetItem | None, staged: bool) -> None:
        if item is None:
            return
        # One subject for the diff: choosing a file in one list lets go of the other.
        other = self.unstaged_list if staged else self.staged_list
        other.blockSignals(True)
        other.clearSelection()
        other.setCurrentItem(None)
        other.blockSignals(False)
        entry = item.data(1, _ENTRY)
        self._show(entry, staged)
        if entry is None:  # a folder: nothing to diff, but say what is in it
            inside = len(self._files_under(item))
            self.diff_title.setText(
                f"{item.data(1, _FOLDER)}/ - {inside} file(s). "
                "Select one to see its diff, or stage the folder."
            )
        self._enable_buttons()

    @staticmethod
    def _files_under(folder: QTreeWidgetItem) -> list[StatusEntry]:
        """Every file somewhere inside ``folder``."""
        found: list[StatusEntry] = []

        def walk(item: QTreeWidgetItem) -> None:
            entry = item.data(1, _ENTRY)
            if entry is not None:
                found.append(entry)
            for i in range(item.childCount()):
                walk(item.child(i))

        walk(folder)
        return found

    def _show(self, entry: StatusEntry | None, staged: bool) -> None:
        self._shown = (entry, staged) if entry is not None else None
        self._diff = None
        self._show_line_endings(entry)
        if entry is None:
            self.diff_title.setText("")
            self._display("")
            return
        where = f"{entry.path} ({'staged' if staged else 'not staged'})"
        try:
            raw = git_ops.file_diff(
                self.repo,
                entry.path,
                staged=staged,
                untracked=entry.untracked and not staged,
            )
        except git_ops.GitError as exc:
            self.diff_title.setText(where)
            self._display(str(exc))
            return
        if len(raw) > MAX_SHOWN:
            self.diff_title.setText(where)
            self._display(
                f"This diff is {len(raw) / 1_000_000:.1f} MB, too large to show. "
                "The file can still be staged whole from the list."
            )
            return
        diff = staging.parse(raw)
        partial = diff.partial and not _whole_file_only(entry)
        self._diff = diff if partial else None
        whole = "" if partial else " - stages as a whole file"
        self.diff_title.setText(where + whole)
        shown = shown_diff(raw)
        self._display(shown.text, shown.marks)

    def _display(
        self, text: str, marks: list[list[tuple[int, int]]] | None = None
    ) -> None:
        """Put a diff -- or, without ``marks``, a sentence -- in the diff view.

        Dots and arrows for whitespace only in a diff: in a sentence saying why
        there is no diff, they are only in the way of reading it.
        """
        document = self.diff_view.document()
        option = document.defaultTextOption()
        flag = QTextOption.Flag.ShowTabsAndSpaces
        if marks is None:
            option.setFlags(option.flags() & ~flag)
        else:
            option.setFlags(option.flags() | flag)
        document.setDefaultTextOption(option)
        # Before the text: setting it is what has the highlighter read these.
        self._colours.marks = marks or []
        self.diff_view.setPlainText(text)

    def _show_line_endings(self, entry: StatusEntry | None) -> None:
        """The selected file's line endings, on disk and in the index, beside the rule.

        Shown for a file and not for a folder or a submodule, which have none.
        """
        endings = None
        if entry is not None and not entry.submodule:
            try:
                endings = git_ops.line_endings(self.repo, entry.path)
            except git_ops.GitError:
                endings = None  # the diff below says what git refused
        if endings is None:
            self.line_endings.setText("")
            self.line_endings.setVisible(False)
            return
        text, off = describe_line_endings(endings)
        self.line_endings.setText(text)
        self.line_endings.setStyleSheet(_OFF_STYLE if off else _EOL_STYLE)
        self.line_endings.setVisible(True)

    def _on_diff_menu(self, position) -> None:
        if self._shown is None:
            return
        verb = "Unstage" if self._shown[1] else "Stage"
        menu = QMenu(self)
        hunk = menu.addAction(f"{verb} hunk")
        lines = menu.addAction(f"{verb} selected lines")
        hunk.setEnabled(self._diff is not None)
        lines.setEnabled(self._diff is not None)
        at = self.diff_view.cursorForPosition(position).blockNumber()
        chosen = menu.exec(self.diff_view.mapToGlobal(position))
        if chosen is hunk:
            self.apply_hunk(at)
        elif chosen is lines:
            self.apply_lines(*self._selected_lines())

    def _selected_lines(self) -> tuple[int, int]:
        """The first and last diff lines the selection touches, or the cursor's."""
        cursor = self.diff_view.textCursor()
        document = self.diff_view.document()
        if not cursor.hasSelection():
            return cursor.blockNumber(), cursor.blockNumber()
        first = document.findBlock(cursor.selectionStart()).blockNumber()
        end = cursor.selectionEnd()
        last = document.findBlock(end).blockNumber()
        # A selection dragged to the very start of the next line does not take it.
        if last > first and document.findBlock(end).position() == end:
            last -= 1
        return first, last

    # ---- staging -----------------------------------------------------------------
    def stage(self, entries: list[StatusEntry]) -> None:
        """Stage ``entries`` whole, normalizing line endings first if asked to."""
        if not entries:
            return
        paths = [path for entry in entries for path in entry.paths]
        with _waiting():
            problems = self._normalize(entries)
            result = git_ops.stage_paths(self.repo, paths)
        self._report(result, f"Staged {len(entries)} file(s).", problems)

    def normalize_line_endings(self) -> None:
        """Rewrite every unstaged file .gitattributes wants other line endings for.

        The files marked in the list, and only those: what is marked is worked out
        by the same conversion this runs, so the count asked about is the count
        that changes.
        """
        paths = sorted(self._eol_changes)
        if not paths:
            self.status.setText(
                "Nothing to normalize: every unstaged file already has the line "
                "endings .gitattributes declares."
            )
            return
        answer = QMessageBox.question(
            self,
            NORMALIZE,
            f"Rewrite the line endings of {len(paths)} unstaged file(s) to what "
            f".gitattributes declares?\n\nRepository: {self.repo}\n\n"
            "Only line endings change, and nothing is staged.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            with _waiting():
                done = git_ops.normalize_line_endings(self.repo, paths)
        except git_ops.GitError as exc:
            self.status.setText(f"Could not normalize: {exc}")
            return
        message = (
            f"Normalized line endings in {len(done.changed)} of {len(paths)} file(s)."
        )
        if done.failed:
            message += " Not done: " + "; ".join(
                f"{path} ({why})" for path, why in done.failed
            )
        self.status.setText(message)
        self.reload()

    def unstage(self, entries: list[StatusEntry]) -> None:
        """Take ``entries`` back out of the index, leaving the files on disk alone."""
        if not entries:
            return
        paths = [path for entry in entries for path in entry.paths]
        with _waiting():
            result = git_ops.unstage_paths(self.repo, paths)
        self._report(result, f"Unstaged {len(entries)} file(s).")

    def apply_hunk(self, line: int) -> None:
        """Stage -- or, from the staged list, unstage -- the hunk ``line`` is in."""
        if self._diff is None:
            return
        hunk = self._diff.hunk_at(line)
        if hunk is None:
            self.status.setText("Put the cursor inside a hunk first.")
            return
        self._apply(hunk.changes())

    def apply_lines(self, first: int, last: int) -> None:
        """Stage -- or, from the staged list, unstage -- the changes in a range."""
        if self._diff is None:
            return
        chosen = self._diff.changes_between(first, last)
        if not chosen:
            self.status.setText("The selection has no added or removed lines in it.")
            return
        self._apply(chosen)

    def _apply(self, chosen: set[int]) -> None:
        entry, staged = self._shown
        try:
            patch = staging.build_patch(self._diff, chosen, reverse=staged)
        except staging.PartialPatchError as exc:
            self.status.setText(str(exc))
            return
        if patch is None:
            return
        with _waiting():
            problems = [] if staged else self._normalize([entry])
            result = git_ops.apply_to_index(self.repo, patch, reverse=staged)
        verb = "Unstaged" if staged else "Staged"
        self._report(result, f"{verb} {len(chosen)} line(s) of {entry.path}.", problems)

    def _normalize(self, entries: list[StatusEntry]) -> list[str]:
        """Line endings first, when asked for. Returns what could not be done."""
        if not self.apply_attributes.isChecked():
            return []
        paths = [e.path for e in entries if not e.submodule and e.worktree != "D"]
        try:
            done = git_ops.normalize_line_endings(self.repo, paths)
        except git_ops.GitError as exc:
            return [f"line endings not applied: {exc}"]
        return [
            f"line endings not applied to {path}: {why}" for path, why in done.failed
        ]

    def _report(
        self, result: git_ops.GitResult, done: str, problems: list[str] | None = None
    ) -> None:
        message = done if result.ok else (result.stderr.strip() or "git refused.")
        if problems:
            message += "  " + "; ".join(problems)
        self.status.setText(message)
        self.reload()

    def _on_double_click(self, item: QTreeWidgetItem, staged: bool) -> None:
        entry = item.data(1, _ENTRY)
        if entry is None:
            return  # a folder: the double-click opens or folds it, and no more
        if staged:
            self.unstage([entry])
        else:
            self.stage([entry])
