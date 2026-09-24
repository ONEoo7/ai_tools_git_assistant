"""A repository's submodules brought to the latest master, and what came of each.

One window for all of it. It opens on the submodules about to be brought up, before
anything is done -- that list is the question, and **Update** the answer -- and then
fills in each one's result as it comes back: fifty fetches take a minute, and a
window that fills in row by row can be watched rather than waited out.

What is done to each is `git_assistant.submodule_update`'s to say. This is where it
is shown: what each one was on, what it is on now, and why any was left alone.

**Retry failed** brings up again the ones whose fetch failed after all its tries,
or that never had their turn, and only those -- at the pace the server has been
found to take, which the window keeps for as long as it is open.
"""

from __future__ import annotations

import os
from collections import Counter
from functools import partial
from pathlib import Path

from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QProgressBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from git_assistant import git_ops
from git_assistant.submodule_update import (
    BRANCH,
    STASH_MESSAGE,
    Outcome,
    Pace,
    Result,
    Target,
)
from git_assistant.ui.repo_picker import branch_colour
from git_assistant.ui.workers import SubmoduleUpdateWorker, run_worker

SUBMODULE, WAS, NOW, RESULT = range(4)

#: The colours the Compare tab marks a difference and a quiet row with, and a red:
#: each reads on a light list and a dark one alike. Updated is the branch green.
SKIPPED_COLOUR = QColor("#b36b00")
FAILED_COLOUR = QColor("#d9534f")
MUTED_COLOUR = QColor("#888888")

#: The results, in the order a summary names them.
_ORDER = (Result.UPDATED, Result.UP_TO_DATE, Result.SKIPPED, Result.FAILED, Result.NOT_RUN)

#: The results worth another go: one that failed, and one that never had its turn.
#: Not one that was skipped, which was a decision, and would be decided the same.
_AGAIN = (Result.FAILED, Result.NOT_RUN)

#: What git adds to every fetch over ssh that fails, whatever went wrong: true, and
#: no help. The line that says what went wrong is the one before these.
_EVERY_TIME = (
    "could not read from remote repository",
    "please make sure you have the correct access rights",
    "and the repository exists",
)


class SubmoduleUpdateDialog(QDialog):
    """The submodules under one Submodules row, and a button to bring them all up."""

    def __init__(
        self, repo: str, chains: list[list[Target]], *, rules_for=None, parent=None
    ) -> None:
        super().__init__(parent)
        self.repo = repo
        self.chains = chains
        self._rules_for = rules_for
        self.targets = [target for chain in chains for target in chain]
        #: Each target's outcome once it has one. Filled in whatever order the
        #: results arrive in, which is not the order they were given.
        self.outcomes: list[Outcome | None] = [None] * len(self.targets)
        #: Update was pressed, so something on disk may have changed.
        self.ran = False
        self._running = False
        self._stopping = False
        self._worker: SubmoduleUpdateWorker | None = None
        #: How fast every run from this window fetches: as fast as it may, until a
        #: server says otherwise -- and then as it said, for the runs after too.
        self._pace = Pace()
        #: The row of each target the run going now was given, in the order given.
        self._rows = list(range(len(self.targets)))
        #: Which run is going now, counted from the first.
        self._run_id = 0

        self.setWindowTitle(f"Update submodules to latest {BRANCH}")
        self.resize(860, 520)
        count = len(self.targets)
        name = Path(repo).name or repo
        self.intro = QLabel(
            f"Fetch {_submodules(count)} of {name} and switch each to the newest {BRANCH} "
            "on its remote. Anything changed or untracked in one is put in a stash "
            f"there first. One whose own {BRANCH} has commits its remote lacks while the "
            "remote has new ones, or whose checked-out commit is on no branch, is left "
            "exactly as it is. A fetch the server turns away is tried again after a "
            "pause, and the fetches after it go one at a time."
        )
        self.intro.setWordWrap(True)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Submodule", "Was", "Now", "Result"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        for target in self.targets:
            item = QTreeWidgetItem([_inside(target.path, repo), _on(target.path), "", ""])
            item.setToolTip(SUBMODULE, target.path)
            self.tree.addTopLevelItem(item)
        header = self.tree.header()
        # Wide enough for what the run will write there, not only for what is there
        # now: "detached" becomes "detached at 1a2b3c4d", and Now starts out empty.
        room = self.fontMetrics().horizontalAdvance
        widest = {
            WAS: room("detached at 0123456789") + 24,
            NOW: room(f"{BRANCH} at 0123456789") + 24,
        }
        for column in (SUBMODULE, WAS, NOW):
            self.tree.resizeColumnToContents(column)
            wanted = max(self.tree.columnWidth(column), widest.get(column, 0))
            self.tree.setColumnWidth(column, min(wanted, 320))
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)

        self.progress = QProgressBar()
        self.progress.setRange(0, max(1, count))
        self.progress.setFormat("%v of %m")
        self.progress.hide()
        self.status = QLabel("")
        self.status.setStyleSheet("color: #888;")
        self.status.setWordWrap(True)

        buttons = QDialogButtonBox()
        self.update_btn = buttons.addButton("Update", QDialogButtonBox.ButtonRole.ActionRole)
        self.retry_btn = buttons.addButton(
            "Retry failed", QDialogButtonBox.ButtonRole.ActionRole
        )
        self.close_btn = buttons.addButton(
            QDialogButtonBox.StandardButton.Cancel
        )
        # Cancel by default: Update rewrites fifty checkouts, and a return key
        # pressed at a window nobody read should not do that.
        self.update_btn.setAutoDefault(False)
        self.retry_btn.setAutoDefault(False)
        self.close_btn.setDefault(True)
        self.update_btn.clicked.connect(self.start)
        self.retry_btn.clicked.connect(self.retry)
        self.close_btn.clicked.connect(self.reject)
        self.update_btn.setEnabled(bool(count))
        self.retry_btn.hide()

        box = QVBoxLayout(self)
        box.addWidget(self.intro)
        box.addWidget(self.tree, 1)
        box.addWidget(self.progress)
        box.addWidget(self.status)
        box.addWidget(buttons)

    # ---- running -------------------------------------------------------------
    def start(self) -> None:
        """Bring every submodule up, filling in the rows as the results come back."""
        if self.ran or not self.targets:
            return
        self.update_btn.hide()
        self._run(range(len(self.targets)))

    def retry(self) -> None:
        """Bring up again the ones whose fetch failed, or that never had their turn."""
        if self._running:
            return
        rows = self.to_retry()
        if rows:
            self._run(rows)

    def to_retry(self) -> list[int]:
        """The rows `retry` would bring up again: failed, stopped, or never finished."""
        return [
            row
            for row, outcome in enumerate(self.outcomes)
            if outcome is None or outcome.result in _AGAIN
        ]

    def _run(self, rows) -> None:
        """Bring up the targets of ``rows``, each chain's in its order, as `start` does."""
        wanted = set(rows)
        chains: list[list[Target]] = []
        order: list[int] = []
        row = 0
        for chain in self.chains:
            kept = []
            for target in chain:
                if row in wanted:
                    kept.append(target)
                    order.append(row)
                row += 1
            if kept:
                chains.append(kept)
        self._rows = order
        self.ran = self._running = True
        self._stopping = False
        self.retry_btn.hide()
        self.close_btn.setEnabled(True)
        self.close_btn.setText("Stop")
        self.progress.show()
        for row in order:
            self.outcomes[row] = None
            self._show_state(row, "Waiting", MUTED_COLOUR)
        self._say_progress()
        # Each run numbered, and its signals with it, so a word from one since
        # replaced can be told apart. Bound in here rather than asked of Qt's sender():
        # outside a slot, PyQt answers that with whatever last sent it anything --
        # an object that may be gone, and reading it took the process down.
        self._run_id += 1
        run = self._run_id
        worker = SubmoduleUpdateWorker(chains, self._rules_for, pace=self._pace)
        worker.begun.connect(partial(self._on_begun, run=run))
        worker.waiting.connect(partial(self._on_waiting, run=run))
        worker.done.connect(partial(self._on_done, run=run))
        worker.finished.connect(partial(self._on_finished, run=run))
        worker.error.connect(partial(self._on_error, run=run))
        self._worker = worker
        run_worker(worker)

    def stop(self) -> None:
        """Begin no more. The ones already begun finish: a checkout is no thing to halve."""
        if not self._running or self._stopping:
            return
        self._stopping = True
        if self._worker is not None:
            self._worker.cancel()
        self.close_btn.setEnabled(False)
        self.close_btn.setText("Stopping...")
        self.status.setText("Stopping: the submodules already begun are finishing.")

    def reject(self) -> None:
        # Escape, the window's close button and Stop all land here. While it runs,
        # each of them means "stop" -- the window stays, to say how far it got.
        if self._running:
            self.stop()
            return
        super().reject()

    # Each of these is told a target's place in the run that sent it, which is a row
    # through `_rows`, and which run that was. One from a run since replaced is passed
    # over: whatever it had to say, that run's whole answer said already. No run at
    # all is a call rather than a signal -- a test's, say.
    def _on_begun(self, index: int, *, run: int | None = None) -> None:
        # A result can overtake the word that its submodule began: they come from
        # different threads. A row that already has its result keeps it.
        row = self._row(index, run)
        if row is not None and self.outcomes[row] is None:
            self._show_state(row, "Working...", MUTED_COLOUR)

    def _on_waiting(self, index: int, text: str, *, run: int | None = None) -> None:
        row = self._row(index, run)
        if row is not None and self.outcomes[row] is None:
            self._show_state(row, text, MUTED_COLOUR)

    def _on_done(self, index: int, outcome: Outcome, *, run: int | None = None) -> None:
        row = self._row(index, run)
        if row is not None:
            self.outcomes[row] = outcome
            self._show_outcome(row, outcome)
            self._say_progress()

    def _on_finished(self, outcomes: list[Outcome], *, run: int | None = None) -> None:
        if self._replaced(run):
            return
        # All of them again, in order: the last few results may still be on their
        # way from the threads that produced them, and this list is the whole answer.
        for index, outcome in enumerate(outcomes):
            self._on_done(index, outcome, run=run)
        self._finish(summarise([o for o in self.outcomes if o is not None]))

    def _on_error(self, message: str, *, run: int | None = None) -> None:
        if self._replaced(run):
            return
        self._finish(f"Stopped: {message}")

    def _row(self, index: int, run: int | None) -> int | None:
        """The row a signal's ``index`` is, or None for a signal to pass over."""
        if self._replaced(run) or not 0 <= index < len(self._rows):
            return None
        return self._rows[index]

    def _replaced(self, run: int | None) -> bool:
        return run is not None and run != self._run_id

    def _finish(self, said: str) -> None:
        self._running = self._stopping = False
        self.progress.setValue(sum(o is not None for o in self.outcomes))
        self.status.setText(said)
        self.close_btn.setEnabled(True)
        self.close_btn.setText("Close")
        self.close_btn.setDefault(True)
        again = len(self.to_retry())
        self.retry_btn.setText(f"Retry failed ({again})")
        self.retry_btn.setVisible(bool(again))

    # ---- drawing -------------------------------------------------------------
    def _say_progress(self) -> None:
        finished = sum(o is not None for o in self.outcomes)
        self.progress.setValue(finished)
        # Only while it runs. A result can arrive after the whole answer did -- they
        # come from different threads -- and must not write over the summary.
        if self._running and not self._stopping:
            self.status.setText(f"{finished} of {_submodules(len(self.outcomes))} done.")

    def _show_state(self, row: int, text: str, colour: QColor | None = None) -> None:
        item = self.tree.topLevelItem(row)
        item.setText(RESULT, text)
        if colour is not None:
            item.setForeground(RESULT, colour)

    def _show_outcome(self, row: int, outcome: Outcome) -> None:
        item = self.tree.topLevelItem(row)
        if outcome.was is not None or outcome.was_branch:
            item.setText(WAS, was_label(outcome))
        if outcome.now is not None:
            item.setText(NOW, f"{BRANCH} at {outcome.now.short}")
            item.setToolTip(NOW, _describe(outcome.now))
        item.setText(RESULT, result_label(outcome))
        item.setToolTip(RESULT, result_tip(outcome))
        colour = {
            Result.UPDATED: branch_colour(self.tree.palette()),
            Result.SKIPPED: SKIPPED_COLOUR,
            Result.FAILED: FAILED_COLOUR,
            Result.NOT_RUN: MUTED_COLOUR,
        }.get(outcome.result)
        item.setForeground(RESULT, colour if colour is not None else self.tree.palette().text())


def was_label(outcome: Outcome) -> str:
    """What a submodule was on: its branch, or the commit it was checked out at."""
    if outcome.was_branch:
        return outcome.was_branch
    return f"detached at {outcome.was.short}" if outcome.was is not None else ""


def result_label(outcome: Outcome) -> str:
    """The row's result: the word, a stash if one was made, why, and how many tries."""
    parts = [outcome.result.value]
    if outcome.stashed:
        parts.append("changes stashed")
    if outcome.note:
        parts.append(headline(outcome.note))
    if outcome.tries > 1:
        parts.append(f"{outcome.tries} tries")
    return " - ".join(parts)


def headline(note: str) -> str:
    """The note's sentence, with git's most telling line after it where it said one.

    "Could not fetch from origin" is true of every failed fetch; what tells them
    apart -- a connection reset, a repository not found -- is in git's words, and
    a row that hid them in its tooltip was a row that had to be hovered over to be
    read. Past the sentence git adds to every failed fetch over ssh, and its hints.
    """
    sentence, _newline, said = note.partition("\n")
    for line in said.splitlines():
        text = line.strip()
        lowered = text.lower()
        if not text or lowered.startswith("hint:") or any(
            boilerplate in lowered for boilerplate in _EVERY_TIME
        ):
            continue
        for prefix in ("fatal: ", "error: ", "warning: "):
            if lowered.startswith(prefix):
                text = text[len(prefix) :]
                break
        return f"{sentence.rstrip('.')}: {text}"
    return sentence


def result_tip(outcome: Outcome) -> str:
    """The whole of it, git's own words included, and where the stash went."""
    lines = [outcome.note] if outcome.note else []
    if outcome.stashed:
        lines.append(
            f'Its changes are in a stash called "{STASH_MESSAGE}". '
            "git stash pop, in this submodule, brings them back."
        )
    return "\n\n".join(lines)


def summarise(outcomes: list[Outcome]) -> str:
    """``41 updated, 5 up to date, 1 failed.``, and where any stashed changes are."""
    if not outcomes:
        return "Nothing was done."
    counted = Counter(outcome.result for outcome in outcomes)
    said = ", ".join(
        f"{counted[result]} {result.value.lower()}" for result in _ORDER if counted[result]
    ) + "."
    stashed = sum(outcome.stashed for outcome in outcomes)
    if stashed:
        said += (
            f" Changes were stashed in {stashed}: git stash pop, in "
            f"{'that submodule' if stashed == 1 else 'each of them'}, brings them back."
        )
    again = sum(outcome.tries > 1 for outcome in outcomes)
    if again:
        said += f" {again} had to be fetched more than once."
    return said


def _submodules(count: int) -> str:
    return f"{count} submodule" + ("" if count == 1 else "s")


def _inside(path: str, repo: str) -> str:
    """Where ``path`` is from the top of ``repo``, with forward slashes as git writes it."""
    try:
        return Path(os.path.relpath(path, repo)).as_posix()
    except ValueError:  # another drive: not inside it at all
        return path


def _on(path: str) -> str:
    """What a submodule is on, as the list says before anything is done: read, not run."""
    return git_ops.head_branch(path) or "detached"


def _describe(commit: git_ops.CommitSummary) -> str:
    tags = f" ({', '.join(commit.tags)})" if commit.tags else ""
    return f"{commit.short}{tags} {commit.date}\n{commit.subject}"
