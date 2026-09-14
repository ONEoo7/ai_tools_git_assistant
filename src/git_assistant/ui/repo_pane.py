"""The repository list, folded against the left edge of a tab until it is wanted.

Every repo-driven tab opened on a column-wide repository list, spent on a
control that is used once per switch and only looked at the rest of the time --
and what it was looked at for, which repository is selected, is named in the
bar above the tabs now. So the list folds, the way the right-hand pane does:
down to a strip with its title on it, which stays on screen folded.

It is the right-hand pane's mechanism mirrored rather than a second one; see
`side_panel.FoldingPane`. The title runs up the left edge because that is the
edge it folds against. A tab can put more behind the same strip -- the Commit tab
puts the selected repository's branches there, under a title of their own.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from git_assistant.ui.repo_picker import RepoPicker
from git_assistant.ui.side_panel import Edge, FoldingPane

REPO_TAB = "Repository"


class RepoPane(FoldingPane):
    """A `RepoPicker` behind a "Repository" title on the left edge, first of any."""

    def __init__(
        self,
        picker: RepoPicker,
        *,
        margins: tuple[int, int, int, int] = (0, 0, 0, 0),
        parent=None,
    ) -> None:
        super().__init__(edge=Edge.LEFT, margins=margins, parent=parent)
        self.picker = picker
        # The strip already says what this is.
        picker.title_label.setVisible(False)
        self.add_page(picker, REPO_TAB)
        self.tabs.setToolTip(
            "Click to choose a repository; click again to fold the list away."
        )
        # Folded is for when a repository is selected, and named in the bar
        # above. With none selected, folding would hide the one thing to do.
        if not picker.current_path():
            self.set_open(True)

    def add_page(self, page: QWidget, title: str) -> int:
        index = super().add_page(page, title)
        if self.tabs.count() > 1:
            # No longer only the repository behind the strip.
            self.tabs.setToolTip(
                "Click a title to open it; click the open one again to fold the "
                "pane away."
            )
        return index
