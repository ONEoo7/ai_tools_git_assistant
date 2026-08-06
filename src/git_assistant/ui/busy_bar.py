"""One progress bar for the whole window, shared by every tab that runs a task.

Each tab used to keep its own, which meant a bar appeared in a different place
depending on which tab you were looking at -- and none at all on the tab you had
switched to while an audit carried on behind it. There is one now, at the foot
of the window, visible from every tab.

**Several tasks can run at once**, and that is the reason this is not a plain
``QProgressBar``. An audit takes minutes and keeps going while you switch to
Code Review and start one there. So owners are counted: the bar is up while any
of them is working, and a percentage is only shown when exactly one task is
reporting one -- two tasks at 40% and 90% have no single number between them,
and picking one would be inventing it.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QWidget

#: Wide enough to read as a bar, narrow enough not to crowd the buttons.
BAR_WIDTH = 150

MUTED = "color: #888;"


class BusyBar(QWidget):
    """A progress bar and a word about what is keeping it up."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        #: owner -> what it is doing, and its percentage or None for "unknown".
        self._owners: dict[object, tuple[str, int | None]] = {}

        self.label = QLabel("")
        self.label.setStyleSheet(MUTED)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(BAR_WIDTH)

        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.label)
        box.addWidget(self.bar)
        self._redraw()

    # ---- what a tab tells it -------------------------------------------------
    def start(self, owner: object, what: str = "") -> None:
        """``owner`` has begun something, of unknown length."""
        self._owners[owner] = (what or "Working", None)
        self._redraw()

    def step(self, owner: object, percent: int) -> None:
        """How far ``owner`` has got, or a negative number for "still unknown".

        Ignored for an owner that never started: a stray progress signal from a
        run that has already finished must not put the bar back up.
        """
        if owner not in self._owners:
            return
        what, _previous = self._owners[owner]
        self._owners[owner] = (what, percent if percent >= 0 else None)
        self._redraw()

    def stop(self, owner: object) -> None:
        """``owner`` has finished, one way or another."""
        if self._owners.pop(owner, None) is not None:
            self._redraw()

    def is_busy(self) -> bool:
        return bool(self._owners)

    # ---- what it shows -------------------------------------------------------
    def _redraw(self) -> None:
        running = list(self._owners.values())
        self.setVisible(bool(running))
        if not running:
            self.label.setText("")
            return

        if len(running) == 1:
            what, percent = running[0]
            self.label.setText(f"{what}...")
        else:
            # No single percentage is true of two tasks, so none is shown.
            what, percent = f"{len(running)} tasks", None
            self.label.setText(f"{len(running)} tasks running...")

        if percent is None:
            self.bar.setRange(0, 0)  # indeterminate
        else:
            self.bar.setRange(0, 100)
            self.bar.setValue(max(0, min(100, percent)))
