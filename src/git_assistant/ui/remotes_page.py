"""The selected repository's remotes: which there are, and which one the branch tracks.

Behind a title of its own beside the repository and its branches, and folded
away with them. The bar above the tabs names the remote a push goes to; this is
where that is changed -- by choosing another one for the branch to track -- and
where remotes are added and taken away.

All of it is this repository's own configuration. Adding a remote fetches
nothing, removing one changes nothing on the server, and a branch that has never
been pushed can be set to track a remote as readily as one that has: its first
push then goes there.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from git_assistant import git_ops

REMOTES_TAB = "Remotes"

MUTED_STYLE = "color: #888;"
WARN_STYLE = "color: #b36b00;"

#: What a row keeps: the remote's name, and where it fetches from.
_NAME = Qt.ItemDataRole.UserRole
_URL = Qt.ItemDataRole.UserRole + 1

#: Beside the remote the branch tracks, in the list.
TRACKED_MARK = "tracked"


class RemotesPage(QWidget):
    """A repository's remotes, the tracked one marked, with ways to change them."""

    #: A remote was added or removed here, or the branch was set to track another.
    remotesChanged = pyqtSignal()  # noqa: N815 - Qt signal naming

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._repo = ""
        self._branch = ""
        self._tracked = ""

        self.tracking_label = QLabel("")
        self.tracking_label.setWordWrap(True)

        self.remote_list = QListWidget()
        self.remote_list.currentItemChanged.connect(self._on_selected)

        # The selected remote's address. Under the list rather than in it: a URL
        # is longer than the pane is wide, and a list of them is a list of
        # ellipses.
        self.url_label = QLabel("")
        self.url_label.setWordWrap(True)
        self.url_label.setStyleSheet(MUTED_STYLE)
        self.url_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        self.track_btn = QPushButton("Track")
        self.track_btn.setToolTip(
            "Make the checked-out branch track the selected remote: it is pulled "
            "from there and pushed there, its first push included."
        )
        self.track_btn.clicked.connect(self._on_track)
        self.remove_btn = QPushButton("Remove...")
        self.remove_btn.setToolTip(
            "Remove the selected remote from this repository (asks first). "
            "Nothing on the server is changed."
        )
        self.remove_btn.clicked.connect(self._on_remove)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Name, e.g. upstream")
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("URL")
        for edit in (self.name_edit, self.url_edit):
            edit.returnPressed.connect(self._on_add)
        self.add_btn = QPushButton("Add remote")
        self.add_btn.setToolTip("Add it to this repository. Nothing is fetched.")
        self.add_btn.clicked.connect(self._on_add)
        self.problem_label = QLabel("")
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet(WARN_STYLE)
        self.problem_label.setVisible(False)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.addWidget(self.track_btn)
        buttons.addWidget(self.remove_btn)

        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(self.tracking_label)
        box.addWidget(self.remote_list, 1)
        box.addWidget(self.url_label)
        box.addLayout(buttons)
        box.addSpacing(12)
        box.addWidget(QLabel("Add a remote:"))
        box.addWidget(self.name_edit)
        box.addWidget(self.url_edit)
        box.addWidget(self.problem_label)
        box.addWidget(self.add_btn)

        self.show_repo("")

    # ---- what is on screen ---------------------------------------------------
    def show_repo(self, repo: str, *, select: str = "") -> None:
        """Read ``repo``'s remotes, and the one its branch tracks, from git.

        ``select`` is the remote to leave selected; by default the one that was,
        if it is still there, and otherwise the tracked one.
        """
        if repo != self._repo:
            self._say("")
        chosen = select or self.selected_remote()
        self._repo = repo or ""
        self._branch = git_ops.head_branch(repo) if repo else ""
        remotes = git_ops.list_remotes(repo) if repo else []
        self._tracked = git_ops.tracking_remote(repo, self._branch) if repo else ""

        self.remote_list.blockSignals(True)
        self.remote_list.clear()
        for remote in remotes:
            tracked = remote.name == self._tracked
            item = QListWidgetItem(
                f"{remote.name}  ({TRACKED_MARK})" if tracked else remote.name
            )
            item.setData(_NAME, remote.name)
            item.setData(_URL, remote.url)
            item.setToolTip(remote.url)
            if tracked:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.remote_list.addItem(item)
        names = [remote.name for remote in remotes]
        for name in (chosen, self._tracked, names[0] if names else ""):
            if name in names:
                self.remote_list.setCurrentRow(names.index(name))
                break
        self.remote_list.blockSignals(False)

        self.tracking_label.setText(self._tracking_sentence(names))
        self.setEnabled(bool(repo))
        self._on_selected()

    def remotes(self) -> list[str]:
        return [
            self.remote_list.item(i).data(_NAME) for i in range(self.remote_list.count())
        ]

    def selected_remote(self) -> str:
        item = self.remote_list.currentItem()
        return item.data(_NAME) if item is not None else ""

    def tracked_remote(self) -> str:
        return self._tracked

    def _tracking_sentence(self, names: list[str]) -> str:
        if not self._repo:
            return "No repository selected."
        if not self._branch:
            return "Not on a branch, so there is no branch to track a remote."
        if not names:
            return "This repository has no remotes yet. Add one below."
        if not self._tracked:
            return (
                f"'{self._branch}' tracks no remote. Select one and press Track, "
                "and its first push goes there."
            )
        if self._tracked not in names:
            return (
                f"'{self._branch}' is set to track '{self._tracked}', which this "
                "repository no longer has."
            )
        return f"'{self._branch}' tracks {self._tracked}."

    def _on_selected(self, _current=None, _previous=None) -> None:
        item = self.remote_list.currentItem()
        name = item.data(_NAME) if item is not None else ""
        self.url_label.setText(item.data(_URL) if item is not None else "")
        self.track_btn.setEnabled(
            bool(name) and bool(self._branch) and name != self._tracked
        )
        self.remove_btn.setEnabled(bool(name))

    # ---- changing it ---------------------------------------------------------
    def _on_track(self) -> None:
        name = self.selected_remote()
        if not (self._repo and self._branch and name):
            return
        result = git_ops.set_tracking_remote(self._repo, self._branch, name)
        if not result.ok:
            QMessageBox.warning(
                self,
                "Could not change the tracked remote",
                f"git refused to make '{self._branch}' track '{name}':\n\n"
                f"{result.stderr.strip() or result.stdout.strip()}",
            )
            return
        self.show_repo(self._repo, select=name)
        self.remotesChanged.emit()

    def _on_remove(self) -> None:
        item = self.remote_list.currentItem()
        if not (self._repo and item is not None):
            return
        name, url = item.data(_NAME), item.data(_URL)
        answer = QMessageBox.question(
            self,
            "Remove remote",
            f"Remove the remote '{name}' from this repository?\n\n{url}\n\n"
            "Its remote-tracking branches go with it, and any branch that tracks "
            "it stops tracking anything. Nothing on the server is changed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = git_ops.remove_remote(self._repo, name)
        if not result.ok:
            QMessageBox.warning(
                self,
                "Could not remove the remote",
                f"git refused to remove '{name}':\n\n"
                f"{result.stderr.strip() or result.stdout.strip()}",
            )
            return
        self.show_repo(self._repo)
        self.remotesChanged.emit()

    def _on_add(self) -> None:
        name = self.name_edit.text().strip()
        url = self.url_edit.text().strip()
        problem = self._add_problem(name, url)
        if problem:
            self._say(problem)
            return
        result = git_ops.add_remote(self._repo, name, url)
        if not result.ok:
            self._say(result.stderr.strip() or "git refused to add the remote.")
            return
        self._say("")
        self.name_edit.clear()
        self.url_edit.clear()
        self.show_repo(self._repo, select=name)
        self.remotesChanged.emit()

    def _say(self, problem: str) -> None:
        """Why the last remote could not be added; no gap when there is nothing."""
        self.problem_label.setText(problem)
        self.problem_label.setVisible(bool(problem))

    def _add_problem(self, name: str, url: str) -> str:
        """Why ``name`` and ``url`` cannot be added, in a sentence, or ``""``."""
        if not self._repo:
            return "Select a repository first."
        if not name:
            return "Give the remote a name."
        if name in self.remotes():
            return f"There is already a remote called '{name}'."
        if not git_ops.valid_remote_name(self._repo, name):
            return f"'{name}' is not a name git accepts for a remote."
        if not url:
            return "Give the remote's URL."
        return ""
