"""The Leaderboard tab: which models are actually any good at reviewing.

A small local model is cheap and fast and may or may not be worth reading. It
cannot answer that about itself, so a stronger model is shown the same exchange
and scores it out of ten; this is where those scores add up.

One row per **reviewed model and judge together**, never per reviewed model
alone. A 7 from Opus and a 7 from a 4B local model are not the same
measurement, and a table that averaged them would rank models by which judge
happened to be configured that week. Changing judge starts a fresh row, which is
what makes the comparison mean something.

Read-only. The numbers come from runs; the only thing to do here is read them,
or throw them away and start again.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant.review import leaderboard

MUTED = "color: #888;"

EMPTY = (
    "Nothing scored yet. Turn on Use LLM-as-a-Judge beside the repository, "
    "configure a judge under Connection & Model, and run a review."
)

#: Selectable and no more; see the Languages tab for why this is said rather
#: than left to Qt's defaults.
_READ_ONLY = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable


class LeaderboardTab(QWidget):
    """Every reviewed model, as scored by every judge that has scored it."""

    #: The board was thrown away, so a host can say so in its status line.
    cleared = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        box = QVBoxLayout(self)
        box.addWidget(
            QLabel(
                "How each model has scored on this repository's reviews, out of "
                "10. Scores from different judges are kept apart."
            )
        )

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            [
                "Model",
                "Judge",
                "Runs",
                "Files",
                "Mean score",
                "Time / file",
                "Last scored",
            ]
        )
        self.tree.setRootIsDecorated(False)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.tree, 1)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(MUTED)
        box.addWidget(self.note)

        row = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        self.clear_btn = QPushButton("Clear leaderboard...")
        self.clear_btn.setToolTip("Throw every score away and start measuring again.")
        self.clear_btn.clicked.connect(self._on_clear)
        row.addWidget(self.refresh_btn)
        row.addStretch(1)
        row.addWidget(self.clear_btn)
        box.addLayout(row)

        self.refresh()

    def _on_clear(self) -> None:
        """Throw the scores away, once somebody says so out loud.

        Confirmed because it is not recoverable: the runs behind these numbers
        are pruned on their own schedule, so the board cannot be rebuilt from
        history.
        """
        board = leaderboard.load()
        if not board.rows:
            return
        if (
            QMessageBox.question(
                self,
                "Clear leaderboard",
                f"Throw away {len(board.rows)} row(s) of scores?\n\n"
                "They cannot be rebuilt: reviews are pruned as they age, so the "
                "runs behind these numbers may already be gone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        leaderboard.clear()
        self.refresh()
        self.cleared.emit()

    def refresh(self) -> None:
        """Re-read the board. One small file; cheap enough to do on every run."""
        self.tree.clear()
        board = leaderboard.load()
        for row in board.ranked():
            self.tree.addTopLevelItem(_row(row))
        self.clear_btn.setEnabled(bool(board.rows))
        self.note.setText(EMPTY if not board.rows else _note(board))


def _row(row: leaderboard.Row) -> QTreeWidgetItem:
    item = QTreeWidgetItem(
        [
            row.label(),
            row.judge_label(),
            str(row.runs),
            str(row.files),
            f"{row.mean:.2f}",
            _duration(row.secs_per_file),
            _when(row.last),
        ]
    )
    item.setFlags(_READ_ONLY)
    item.setToolTip(
        4,
        f"{row.files} file(s) scored across {row.runs} run(s), "
        f"{row.total:.1f} points in total.",
    )
    item.setToolTip(
        5,
        f"{_duration(row.seconds)} of model time across {row.files} file(s). "
        "Time per call, added up -- not how long the run took, which depends "
        "on how many ran at once.",
    )
    return item


def _duration(seconds: float) -> str:
    """A duration a person reads, not a float.

    Sub-second answers are what a small local model gives, and `0.42s` says
    more there than `0s` would; anything over a minute is minutes.
    """
    if seconds <= 0:
        return ""
    if seconds < 10:
        return f"{seconds:.2f}s"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}m {rest}s"


def _when(stamp: str) -> str:
    """The timestamp as a date, or whatever it was if it will not parse."""
    from datetime import datetime

    try:
        moment = datetime.fromisoformat((stamp or "").replace("Z", "+00:00"))
    except ValueError:
        return stamp or ""
    return moment.astimezone().strftime("%d %b %H:%M")


def _note(board: leaderboard.Board) -> str:
    judges = {one.judge_label() for one in board.rows}
    if len(judges) > 1:
        return (
            f"{len(board.rows)} row(s), scored by {len(judges)} different "
            "judges. Only rows with the same judge are comparable."
        )
    return f"{len(board.rows)} row(s). {leaderboard.path()}"
