"""Saying that a change to a list of past runs did not reach the disk.

The Commit, Audit and Code Review tabs each keep a list of what they produced, read
back from a file whenever it is drawn. A pin, a delete or a clear that could not be
written is simply not there when the list is drawn again -- and a click that
silently did nothing is worse than one that says why.

Almost always, why is another program holding the file open for a moment: an
antivirus, the search indexer, a second copy of this application. The saves wait
that out for about a second (see `git_assistant.atomic`); this is what is said when
that was not long enough.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtWidgets import QMessageBox, QWidget


def history_not_saved(
    parent: QWidget | None, what: str, *, listing: str, where: str | Path
) -> None:
    """Say ``what`` did not happen, because the list of ``listing`` at ``where`` was not saved."""
    QMessageBox.warning(
        parent,
        "Not saved",
        f"{what}\n\nThe list of {listing} could not be saved to {where}. Another "
        "program may have had it open; try again in a moment.",
    )
