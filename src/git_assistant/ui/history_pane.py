"""History: the repository's commits as a graph, the way Git Extensions draws them.

A row a commit: its lane in the graph, the names pointing at it and what it says,
who wrote it and when, and its hash. Above them all, two rows for what is not
committed yet -- the working directory and the index -- drawn down to HEAD, which is
where they will be committed onto. Selecting a commit says the rest underneath: the
whole message, its parents and children, and the branches and tags it is part of.

Read on a worker: a history is one git command, and a repository with a long one
takes a moment to order. See git_assistant.commit_graph for how the lanes are laid.
"""

from __future__ import annotations

import html
import re
import time
import zlib
from datetime import datetime

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import commit_graph, git_ops
from git_assistant.commit_graph import GraphRow
from git_assistant.git_ops import LoggedCommit
from git_assistant.ui.workers import FunctionWorker, run_worker

#: The graph's column has no title, as in Git Extensions: it is a column of lines.
COLUMNS = ("", "Message", "Author", "Date", "Commit")
GRAPH, MESSAGE, AUTHOR, DATE, HASH = range(len(COLUMNS))

#: What a row carries: its part of the graph, on the graph column; the commit, or
#: which of the two rows above the commits it is, on the message column.
ROW_ROLE = Qt.ItemDataRole.UserRole
COMMIT_ROLE = Qt.ItemDataRole.UserRole + 1
PENDING_ROLE = Qt.ItemDataRole.UserRole + 2

WORKING_DIRECTORY = "Working directory"
COMMIT_INDEX = "Commit index"
#: What the two rows above the commits stand in the graph as. No commit is called either.
_WORKING = "\0working-directory"
_INDEX = "\0commit-index"

LANE_WIDTH = 14
#: The most lanes drawn. A history with more branches open at once than this is cut
#: off at the right rather than pushing everything else off the screen.
MOST_LANES = 16

#: A lane's colour, by the order lanes were opened in: bright enough to read on a
#: dark list and deep enough on a light one.
LANE_COLOURS = tuple(
    QColor(name)
    for name in ("#3b8eea", "#d19a22", "#2ea043", "#d9534f", "#8e6ad8", "#1f9e96", "#c65e9a", "#7f8f2a")
)
#: The line down from what is not committed yet: dashed, and no branch's colour.
PENDING_COLOUR = QColor("#8a8a8a")

REF_COLOURS = {
    git_ops.REF_BRANCH: QColor("#2ea043"),
    git_ops.REF_REMOTE: QColor("#d19a22"),
    git_ops.REF_TAG: QColor("#3b8eea"),
    git_ops.REF_HEAD: QColor("#d9534f"),
}

AVATAR_COLOURS = tuple(
    QColor(name)
    for name in ("#5b6e8c", "#c2410c", "#4d7c0f", "#7e22ce", "#0e7490", "#a16207", "#be123c", "#475569")
)

#: Rows by the author of the selected commit, as Git Extensions marks them.
AUTHOR_TINT = QColor(255, 196, 0, 38)

MUTED = "#888888"

#: How long the selection has to rest on a commit before the branches and tags it is
#: part of are looked up: three git commands, which holding an arrow key down would
#: otherwise start a dozen times a second.
LOOKUP_DELAY_MS = 250

#: The most branches or tags named as containing a commit before the rest are counted.
MOST_NAMED = 12


def relative_date(then: int, now: float | None = None) -> str:
    """``then``, seconds since the epoch, as how long ago: "15 days ago"."""
    seconds = max(0, int((time.time() if now is None else now) - then))
    days = seconds // 86400

    def ago(count: int, unit: str) -> str:
        return f"{count} {unit}{'' if count == 1 else 's'} ago"

    if seconds < 60:
        return ago(seconds, "second")
    if seconds < 3600:
        return ago(seconds // 60, "minute")
    if seconds < 86400:
        return ago(seconds // 3600, "hour")
    if days < 30:
        return ago(days, "day")
    if days < 365:
        return ago(min(11, days // 30), "month")
    return ago(days // 365, "year")


def absolute_date(then: int) -> str:
    return datetime.fromtimestamp(then).strftime("%Y-%m-%d %H:%M:%S")


def initials(name: str) -> str:
    """"bogdan.taloi" and "Bogdan Taloi" are both BT; a single name its first letter."""
    parts = [part for part in re.split(r"[\s._\-]+", name.strip()) if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def avatar_colour(email: str) -> QColor:
    """The same colour for the same address, every time and on every machine."""
    return AVATAR_COLOURS[zlib.crc32(email.strip().casefold().encode("utf-8")) % len(AVATAR_COLOURS)]


def avatar_pixmap(name: str, email: str, size: int) -> QPixmap:
    """A square of the author's colour with their initials on it."""
    ratio = QGuiApplication.primaryScreen().devicePixelRatio() if QGuiApplication.primaryScreen() else 1.0
    pixmap = QPixmap(int(size * ratio), int(size * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        _draw_avatar(painter, QRectF(0, 0, size, size), name, email)
    finally:
        painter.end()
    return pixmap


def _draw_avatar(painter: QPainter, square: QRectF, name: str, email: str) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(avatar_colour(email))
    radius = max(2.0, square.height() * 0.12)
    painter.drawRoundedRect(square, radius, radius)
    font = QFont(QApplication.font())
    font.setPixelSize(max(6, int(square.height() * 0.5)))
    painter.setFont(font)
    painter.setPen(QColor("white"))
    painter.drawText(square, Qt.AlignmentFlag.AlignCenter, initials(name))
    painter.restore()


def _pending_text(kind: str, count: int | None) -> str:
    if count is None:
        return ""
    if kind == _WORKING:
        return "no unstaged changes" if not count else f"{count} file(s) with unstaged changes"
    return "nothing staged" if not count else f"{count} file(s) staged"


# ---- drawing a row --------------------------------------------------------------------
class _RowDelegate(QStyledItemDelegate):
    """A column's cell, tinted when the row is by the selected commit's author."""

    def __init__(self, tree: _HistoryTree) -> None:
        super().__init__(tree)
        self.tree = tree

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt naming
        hint = super().sizeHint(option, index)
        return QSize(hint.width(), max(hint.height(), option.fontMetrics.height() + 10))

    def _tint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        if self.tree.by_highlighted_author(index) and not (
            option.state & QStyle.StateFlag.State_Selected
        ):
            painter.fillRect(option.rect, AUTHOR_TINT)

    def paint(self, painter, option, index) -> None:
        self._tint(painter, option, index)
        super().paint(painter, option, index)

    def _panel(self, painter, option, index) -> QStyleOptionViewItem:
        """The cell's background and selection, with nothing written in it."""
        self._tint(painter, option, index)
        panel = QStyleOptionViewItem(option)
        self.initStyleOption(panel, index)
        panel.text = ""
        widget = panel.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, panel, painter, widget)
        return panel

    @staticmethod
    def _text_colour(option: QStyleOptionViewItem) -> QColor:
        """The colour the style writes a row's text in, selected or not.

        The text colour, on the Windows 11 style this ships against -- and under
        the pink theme's stylesheet, which draws with it: a selected row is a
        shade of the list's own background there (#f5f5f5 on #ffffff), and the
        palette's highlighted text, white, vanishes into it. A style that fills a
        selected row with the highlight writes on it in highlighted text.
        """
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        native = style.name() == "windows11" or (
            style.metaObject().className() == "QStyleSheetStyle"
        )
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        role = (
            QPalette.ColorRole.HighlightedText
            if selected and not native
            else QPalette.ColorRole.Text
        )
        return option.palette.color(role)


class _GraphDelegate(_RowDelegate):
    def paint(self, painter, option, index) -> None:
        self._panel(painter, option, index)
        row: GraphRow | None = index.data(ROW_ROLE)
        if row is None:
            return
        rect = QRectF(option.rect)
        top, middle, bottom = rect.top(), rect.center().y(), rect.bottom() + 1

        def x(lane: int) -> float:
            return rect.left() + 3 + LANE_WIDTH / 2 + lane * LANE_WIDTH

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setClipRect(option.rect)
        for lines, start_y, end_y in ((row.above, top, middle), (row.below, middle, bottom)):
            for line in lines:
                if max(line.start, line.end) >= MOST_LANES:
                    continue
                path = QPainterPath(QPointF(x(line.start), start_y))
                if line.start == line.end:
                    path.lineTo(x(line.end), end_y)
                else:
                    half = (start_y + end_y) / 2
                    path.cubicTo(
                        QPointF(x(line.start), half),
                        QPointF(x(line.end), half),
                        QPointF(x(line.end), end_y),
                    )
                pen = QPen(self.tree.lane_colour(line.colour), 2)
                if self.tree.is_pending_colour(line.colour):
                    pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawPath(path)

        if row.lane < MOST_LANES:
            centre = QPointF(x(row.lane), middle)
            commit: LoggedCommit | None = index.sibling(index.row(), MESSAGE).data(COMMIT_ROLE)
            colour = self.tree.lane_colour(row.colour)
            if commit is None:  # what is not committed yet: a hollow node
                painter.setPen(QPen(PENDING_COLOUR, 2))
                painter.setBrush(option.palette.color(QPalette.ColorRole.Base))
                painter.drawEllipse(centre, 4, 4)
            elif commit.is_head():
                painter.setPen(QPen(self._text_colour(option), 2))
                painter.setBrush(colour)
                painter.drawEllipse(centre, 5.5, 5.5)
            else:
                painter.setPen(QPen(colour.darker(130), 1))
                painter.setBrush(colour)
                painter.drawEllipse(centre, 4, 4)
        painter.restore()


class _MessageDelegate(_RowDelegate):
    """The names pointing at a commit, boxed, and then what it says."""

    def paint(self, painter, option, index) -> None:
        panel = self._panel(painter, option, index)
        rect = option.rect.adjusted(4, 0, -4, 0)
        font = QFont(panel.font)
        metrics = QFontMetrics(font)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        pending = index.data(PENDING_ROLE)
        commit: LoggedCommit | None = index.data(COMMIT_ROLE)
        labels: list[tuple[str, QColor, bool]] = []
        if pending is not None:
            labels.append((WORKING_DIRECTORY if pending == _WORKING else COMMIT_INDEX, PENDING_COLOUR, False))
        elif commit is not None:
            labels += [(ref.name, REF_COLOURS.get(ref.kind, PENDING_COLOUR), ref.current) for ref in commit.refs]

        x = rect.left()
        height = metrics.height() + 2
        box_top = rect.center().y() - height / 2 + 1
        for name, colour, current in labels:
            label_font = QFont(font)
            label_font.setBold(current)
            label_metrics = QFontMetrics(label_font)
            width = label_metrics.horizontalAdvance(name) + 10
            if x + width > rect.right() - 24:
                painter.setPen(self._text_colour(option))
                painter.drawText(QRectF(x, rect.top(), 20, rect.height()), Qt.AlignmentFlag.AlignVCenter, "...")
                x += 20
                break
            box = QRectF(x, box_top, width, height)
            fill = QColor(colour)
            fill.setAlpha(45)
            painter.setBrush(fill)
            painter.setPen(QPen(colour, 2 if current else 1))
            painter.drawRoundedRect(box.adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
            painter.setFont(label_font)
            painter.setPen(self._text_colour(option))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, name)
            x += width + 4

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        remaining = QRectF(x, rect.top(), max(0, rect.right() - x), rect.height())
        painter.setFont(font)
        colour = QColor(MUTED) if pending is not None else self._text_colour(option)
        if option.state & QStyle.StateFlag.State_Selected:
            colour = self._text_colour(option)
        painter.setPen(colour)
        painter.drawText(
            remaining,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(remaining.width())),
        )
        painter.restore()


class _AuthorDelegate(_RowDelegate):
    """Initials in the author's colour, then the name: bold for the selected author."""

    def paint(self, painter, option, index) -> None:
        commit: LoggedCommit | None = index.sibling(index.row(), MESSAGE).data(COMMIT_ROLE)
        if commit is None:
            self._panel(painter, option, index)
            return
        panel = self._panel(painter, option, index)
        rect = option.rect.adjusted(4, 0, -4, 0)
        side = min(rect.height() - 6, 20)
        square = QRectF(rect.left(), rect.center().y() - side / 2 + 1, side, side)
        _draw_avatar(painter, square, commit.author, commit.email)
        painter.save()
        font = QFont(panel.font)
        font.setBold(self.tree.by_highlighted_author(index))
        painter.setFont(font)
        painter.setPen(self._text_colour(option))
        text_rect = QRectF(square.right() + 6, rect.top(), rect.right() - square.right() - 6, rect.height())
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            QFontMetrics(font).elidedText(commit.author, Qt.TextElideMode.ElideRight, int(text_rect.width())),
        )
        painter.restore()


class _HistoryTree(QTreeWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setHeaderLabels(list(COLUMNS))
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAllColumnsShowFocus(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        #: The selected commit's author, whose rows are tinted; "" for nobody's.
        self.highlighted_email = ""
        #: Whether the first lane is the dashed one down from the work in progress.
        self.pending_lane = False
        header = self.header()
        header.setStretchLastSection(False)
        for column in range(len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(MESSAGE, QHeaderView.ResizeMode.Stretch)
        for column, width in ((GRAPH, LANE_WIDTH * 2 + 6), (AUTHOR, 150), (DATE, 110), (HASH, 72)):
            self.setColumnWidth(column, width)
        self.setItemDelegateForColumn(GRAPH, _GraphDelegate(self))
        self.setItemDelegateForColumn(MESSAGE, _MessageDelegate(self))
        self.setItemDelegateForColumn(AUTHOR, _AuthorDelegate(self))
        self.setItemDelegateForColumn(DATE, _RowDelegate(self))
        self.setItemDelegateForColumn(HASH, _RowDelegate(self))

    def by_highlighted_author(self, index) -> bool:
        commit: LoggedCommit | None = index.sibling(index.row(), MESSAGE).data(COMMIT_ROLE)
        return bool(self.highlighted_email) and commit is not None and (
            commit.email.casefold() == self.highlighted_email
        )

    def is_pending_colour(self, colour: int) -> bool:
        return self.pending_lane and colour == 0

    def lane_colour(self, colour: int) -> QColor:
        if self.is_pending_colour(colour):
            return PENDING_COLOUR
        return LANE_COLOURS[(colour - 1 if self.pending_lane else colour) % len(LANE_COLOURS)]


# ---- what the selected commit is -------------------------------------------------------
class CommitDetails(QWidget):
    """The selected commit in full, with its parents and children to follow."""

    #: A parent or child was clicked: the hash of the commit to go to.
    commitChosen = pyqtSignal(str)  # noqa: N815 - Qt signal naming

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.avatar = QLabel()
        self.avatar.setFixedSize(52, 52)
        self.text = QTextBrowser()
        self.text.setOpenLinks(False)
        self.text.anchorClicked.connect(self._on_link)
        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.avatar, 0, Qt.AlignmentFlag.AlignTop)
        box.addWidget(self.text, 1)
        self._about = ""
        self._containing = ""
        self.clear()

    def clear(self) -> None:
        self.avatar.clear()
        self.avatar.setVisible(False)
        self._about = f"<p style='color:{MUTED}'>Select a commit to see it in full.</p>"
        self._containing = ""
        self._show()

    def show_pending(self, title: str, detail: str) -> None:
        self.avatar.clear()
        self.avatar.setVisible(False)
        self._about = f"<p><b>{html.escape(title)}</b></p><p>{html.escape(detail or '')}</p>"
        self._containing = ""
        self._show()

    def show_commit(
        self, commit: LoggedCommit, children: list[str], known: set[str]
    ) -> None:
        self.avatar.setPixmap(avatar_pixmap(commit.author, commit.email, 52))
        self.avatar.setVisible(True)

        def link(full: str) -> str:
            short = html.escape(full[:10])
            return f"<a href='commit:{full}'>{short}</a>" if full in known else short

        facts = [
            ("Author", f"{html.escape(commit.author)} &lt;{html.escape(commit.email)}&gt;"),
            ("Date", f"{relative_date(commit.authored)} ({absolute_date(commit.authored)})"),
            ("Commit hash", html.escape(commit.hash)),
            ("Parents", ", ".join(link(p) for p in commit.parents) or "none: the first commit"),
            ("Children", ", ".join(link(c) for c in children) or "none"),
        ]
        branches = [ref.name for ref in commit.refs if ref.kind in (git_ops.REF_BRANCH, git_ops.REF_REMOTE)]
        tags = [ref.name for ref in commit.refs if ref.kind == git_ops.REF_TAG]
        if branches:
            facts.append(("Branches here", html.escape(", ".join(branches))))
        if tags:
            facts.append(("Tags here", html.escape(", ".join(tags))))
        table = "".join(
            f"<tr><td style='padding-right:12px; color:{MUTED}'>{label}:</td><td>{value}</td></tr>"
            for label, value in facts
        )
        message = html.escape(commit.message)
        self._about = (
            f"<table cellspacing='0' cellpadding='1'>{table}</table>"
            f"<p style='white-space: pre-wrap'><b>{message.split(chr(10), 1)[0]}</b>"
            f"{message[len(message.split(chr(10), 1)[0]):]}</p>"
        )
        self._containing = (
            f"<p style='color:{MUTED}'>Looking up the branches and tags it is part of...</p>"
        )
        self._show()

    def show_containment(self, branches: list[str], tags: list[str], nearest: str) -> None:
        def named(names: list[str]) -> str:
            if not names:
                return "none"
            shown = ", ".join(html.escape(name) for name in names[:MOST_NAMED])
            more = len(names) - MOST_NAMED
            return f"{shown} and {more} more" if more > 0 else shown

        self._containing = (
            f"<p><span style='color:{MUTED}'>Contained in branches:</span> {named(branches)}<br>"
            f"<span style='color:{MUTED}'>Contained in tags:</span> {named(tags)}<br>"
            f"<span style='color:{MUTED}'>Derives from tag:</span> "
            f"{html.escape(nearest) if nearest else 'none'}</p>"
        )
        self._show()

    def _show(self) -> None:
        self.text.setHtml(self._about + self._containing)

    def _on_link(self, url) -> None:
        if url.scheme() == "commit":
            self.commitChosen.emit(url.path())


# ---- the pane ----------------------------------------------------------------------------
class HistoryPane(QWidget):
    """The history graph over the selected commit, for one repository at a time."""

    def __init__(self, gap: int = 12, parent=None) -> None:
        super().__init__(parent)
        self._repo = ""
        #: A repository was given while the pane was off screen, and is still unread.
        self._stale = False
        #: Reads asked for, of the history and of what contains a commit. An answer
        #: to any but the last is about something no longer on screen.
        self._asked = 0
        self._looked_up = 0
        self._commits: dict[str, LoggedCommit] = {}
        self._children: dict[str, list[str]] = {}
        self._counts: dict[str, int | None] = {_WORKING: None, _INDEX: None}
        #: Where HEAD was when the history was last drawn.
        self._head = ""
        self._lookup_timer = QTimer(self)
        self._lookup_timer.setSingleShot(True)
        self._lookup_timer.setInterval(LOOKUP_DELAY_MS)
        self._lookup_timer.timeout.connect(self._start_lookup)

        heading = QHBoxLayout()
        heading.addWidget(QLabel("History"))
        self.note = QLabel("")
        self.note.setStyleSheet(f"color: {MUTED};")
        heading.addWidget(self.note, 1)
        self.all_branches = QCheckBox("All branches")
        self.all_branches.setChecked(True)
        self.all_branches.setToolTip(
            "Every local branch, remote branch and tag. Untick for the history of "
            "the checked-out branch alone."
        )
        self.all_branches.toggled.connect(self._reload)
        heading.addWidget(self.all_branches)

        self.tree = _HistoryTree()
        self.tree.currentItemChanged.connect(self._on_current)
        self.tree.customContextMenuRequested.connect(self._on_menu)
        self.details = CommitDetails()
        self.details.commitChosen.connect(self.select_commit)

        top = QWidget()
        top_box = QVBoxLayout(top)
        top_box.setContentsMargins(0, 0, 0, gap)
        top_box.addLayout(heading)
        top_box.addWidget(self.tree, 1)
        bottom = QWidget()
        bottom_box = QVBoxLayout(bottom)
        bottom_box.setContentsMargins(0, gap, 0, 0)
        bottom_box.addWidget(self.details)
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(top)
        self.splitter.addWidget(bottom)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([420, 160])

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.splitter)

    # ---- reading -----------------------------------------------------------------
    def show_repo(self, repo: str) -> None:
        """Read ``repo``'s history, and draw it once it has been read.

        At once while the pane is on screen, and otherwise when it next is: a pane
        nobody is looking at has no use for a git command every time the
        repository, the branch or what is staged changes under it.
        """
        self._repo = repo or ""
        self._stale = True
        if self.isVisible():
            self._reload()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        # Once the events in hand are handled: a tab is shown before its window
        # hears it was switched to, and the window's refresh asks for this too.
        if self._stale:
            QTimer.singleShot(0, self._reload_if_stale)

    def _reload_if_stale(self) -> None:
        if self._stale and self.isVisible():
            self._reload()

    def _reload(self, *_args) -> None:
        self._stale = False
        self._asked += 1
        asked, repo = self._asked, self._repo
        if not repo:
            self._draw([])
            return
        all_branches = self.all_branches.isChecked()

        def read():
            try:
                return asked, git_ops.commit_log(repo, all_branches=all_branches), ""
            except Exception as exc:  # noqa: BLE001 - said on screen, not lost in a thread
                return asked, None, str(exc) or type(exc).__name__

        self.note.setText("reading...")
        worker = FunctionWorker(read)
        worker.finished.connect(self._on_read)
        run_worker(worker)

    def _on_read(self, outcome) -> None:
        asked, commits, problem = outcome
        if asked != self._asked:
            return
        if commits is None:
            self.note.setText(f"could not read the history: {problem}")
            return
        self._draw(commits)

    def show_worktree(self, unstaged: int | None, staged: int | None) -> None:
        """What is not committed yet, for the two rows above the commits."""
        self._counts = {_WORKING: unstaged, _INDEX: staged}
        for index in range(min(2, self.tree.topLevelItemCount())):
            item = self.tree.topLevelItem(index)
            kind = item.data(MESSAGE, PENDING_ROLE)
            if kind is not None:
                item.setText(MESSAGE, _pending_text(kind, self._counts[kind]))
        current = self.tree.currentItem()
        if current is not None and current.data(MESSAGE, PENDING_ROLE) is not None:
            self._on_current(current, None)

    # ---- drawing -----------------------------------------------------------------
    def _draw(self, commits: list[LoggedCommit]) -> None:
        kept = self.current_hash()
        # A selection on HEAD stays on HEAD: after a commit, a checkout or a pull,
        # the commit wanted is the one HEAD has moved to, not the one it left.
        follows_head = bool(kept) and kept == self._head
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            self._commits = {commit.hash: commit for commit in commits}
            self._children = {}
            for commit in commits:
                for parent in commit.parents:
                    self._children.setdefault(parent, []).append(commit.hash)
            if not self._repo:
                self._head = ""
                self.note.setText("")
                self.details.clear()
                return

            head = next((commit.hash for commit in commits if commit.is_head()), "")
            self._head = head
            if follows_head:
                kept = head
            nodes: list[tuple[str, tuple[str, ...]]] = [
                (_WORKING, (_INDEX,)),
                (_INDEX, (head,) if head else ()),
                *((commit.hash, commit.parents) for commit in commits),
            ]
            rows = commit_graph.lay_out(nodes, fresh={head} if head else ())
            self.tree.pending_lane = True
            fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
            items = []
            for (name, _parents), row in zip(nodes, rows):
                item = QTreeWidgetItem([""] * len(COLUMNS))
                item.setData(GRAPH, ROW_ROLE, row)
                commit = self._commits.get(name)
                if commit is None:
                    item.setData(MESSAGE, PENDING_ROLE, name)
                    item.setText(MESSAGE, _pending_text(name, self._counts[name]))
                else:
                    item.setData(MESSAGE, COMMIT_ROLE, commit)
                    item.setText(MESSAGE, commit.subject)
                    item.setToolTip(MESSAGE, commit.message)
                    item.setText(AUTHOR, commit.author)
                    item.setToolTip(AUTHOR, f"{commit.author} <{commit.email}>")
                    item.setText(DATE, relative_date(commit.authored))
                    item.setToolTip(DATE, absolute_date(commit.authored))
                    item.setText(HASH, commit.hash[:7])
                    item.setToolTip(HASH, commit.hash)
                    item.setFont(HASH, fixed)
                items.append(item)
            self.tree.addTopLevelItems(items)
            widest = max((row.lanes for row in rows), default=1)
            self.tree.setColumnWidth(GRAPH, LANE_WIDTH * min(widest, MOST_LANES) + 10)
        finally:
            self.tree.blockSignals(False)

        shown = f"{len(commits)} commit{'' if len(commits) == 1 else 's'}"
        if len(commits) >= git_ops.HISTORY_LIMIT:
            shown = f"the newest {len(commits)} commits"
        self.note.setText(shown)
        target = self._item_for(kept) or self._item_for(head) or self.tree.topLevelItem(0)
        if target is not None:
            self.tree.setCurrentItem(target)
            self.tree.scrollToItem(target)

    def _item_for(self, full: str) -> QTreeWidgetItem | None:
        if not full:
            return None
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            commit = item.data(MESSAGE, COMMIT_ROLE)
            if commit is not None and commit.hash == full:
                return item
        return None

    def current_hash(self) -> str:
        item = self.tree.currentItem()
        commit = item.data(MESSAGE, COMMIT_ROLE) if item is not None else None
        return commit.hash if commit is not None else ""

    def select_commit(self, full: str) -> None:
        item = self._item_for(full)
        if item is not None:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)

    # ---- the selected row --------------------------------------------------------
    def _on_current(self, current: QTreeWidgetItem | None, _previous) -> None:
        self._lookup_timer.stop()
        self._looked_up += 1  # whatever was being looked up is not selected any more
        commit = current.data(MESSAGE, COMMIT_ROLE) if current is not None else None
        self.tree.highlighted_email = commit.email.casefold() if commit is not None else ""
        self.tree.viewport().update()
        if current is None:
            self.details.clear()
            return
        if commit is None:
            kind = current.data(MESSAGE, PENDING_ROLE)
            title = WORKING_DIRECTORY if kind == _WORKING else COMMIT_INDEX
            self.details.show_pending(title, current.text(MESSAGE))
            return
        self.details.show_commit(commit, self._children.get(commit.hash, []), set(self._commits))
        self._lookup_timer.start()

    def _start_lookup(self) -> None:
        commit, repo = self.current_hash(), self._repo
        if not commit or not repo:
            return
        self._looked_up += 1
        asked = self._looked_up

        def read():
            try:
                return (
                    asked,
                    git_ops.branches_containing(repo, commit),
                    git_ops.tags_containing(repo, commit),
                    git_ops.nearest_tag(repo, commit),
                )
            except Exception:  # noqa: BLE001 - nothing to say it with but "none"
                return asked, [], [], ""

        worker = FunctionWorker(read)
        worker.finished.connect(self._on_looked_up)
        run_worker(worker)

    def _on_looked_up(self, outcome) -> None:
        asked, branches, tags, nearest = outcome
        if asked != self._looked_up:
            return
        self.details.show_containment(branches, tags, nearest)

    def _on_menu(self, point) -> None:
        item = self.tree.itemAt(point)
        commit = item.data(MESSAGE, COMMIT_ROLE) if item is not None else None
        if commit is None:
            return
        menu = QMenu(self.tree)
        copy_hash = menu.addAction("Copy commit hash")
        copy_message = menu.addAction("Copy commit message")
        chosen = menu.exec(self.tree.viewport().mapToGlobal(point))
        if chosen is copy_hash:
            QGuiApplication.clipboard().setText(commit.hash)
        elif chosen is copy_message:
            QGuiApplication.clipboard().setText(commit.message)
