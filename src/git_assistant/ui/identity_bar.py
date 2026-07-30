"""Committer-identity picker: the "commit as" row above the tabs.

Git can already switch identity per repository, either by hand or with an
``includeIf`` conditional include keyed on a directory. Both work; neither is
visible at the moment it matters, which is when you are about to commit. This
row makes the answer visible in the window where commits are written, and
selecting a different one writes it to the repository.

The selection is deliberately written to git rather than remembered here. Git's
config is what decides how a commit is stamped, so anything this application
stored separately would be a second opinion -- and the one on screen would be
the wrong one as soon as the two disagreed. The set of identities to choose
from is the Identities tab's business; which one is in force is this row's.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QWidget,
)

from git_assistant import git_ops
from git_assistant.config import Settings
from git_assistant.identities import IdentityStore

#: Combo entry that opens the Identities tab instead of selecting anything.
MANAGE = "__manage__"

#: Combo entry for an identity that is in git but not in the stored set.
UNSAVED = "__unsaved__"

INFO_STYLE = "color: #8ab;"
WARN_STYLE = "color: #b36b00;"


class IdentityBar(QWidget):
    """"Commit as: <email>" for the active repository."""

    #: Emitted after the active repository's identity has been changed, so the
    #: rest of the window can re-read anything that quoted the old one.
    identityChanged = pyqtSignal()  # noqa: N815 - Qt signal naming

    #: Emitted when the user picks "Manage identities...", so the window can
    #: bring the Identities tab forward.
    manageRequested = pyqtSignal()  # noqa: N815 - Qt signal naming

    def __init__(self, settings: Settings, store: IdentityStore, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.store = store
        self._repo = ""
        self._loading = False

        self.combo = QComboBox()
        self.combo.setMinimumWidth(320)
        self.combo.currentIndexChanged.connect(self._on_selected)

        self.status = QLabel("")
        self.status.setStyleSheet(INFO_STYLE)

        # Deliberately its own readout rather than more tooltip on the combo.
        # "Commit as" is only half the answer, and the half it leaves out is
        # the one people assume it covers.
        self.auth_status = QLabel("")
        self.auth_status.setStyleSheet(INFO_STYLE)

        box = QHBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(QLabel("Commit as:"))
        box.addWidget(self.combo)
        box.addWidget(self.status)
        box.addStretch(1)
        box.addWidget(self.auth_status)

        self.refresh()

    # ---- display -----------------------------------------------------------
    def set_repo(self, path: str) -> None:
        self._repo = path or ""
        self.refresh()

    def refresh(self) -> None:
        """Rebuild from the active repository's *current* git identity."""
        self._loading = True  # repopulating must not look like a user choice
        try:
            self.combo.clear()
            repo = self._repo or self.settings.active_repo
            if not repo:
                self.combo.setEnabled(False)
                self.status.setText("No repository selected")
                self.status.setToolTip("")
                self.auth_status.setText("")
                self.auth_status.setToolTip("")
                return

            self.combo.setEnabled(True)
            _name, email = git_ops.get_identity(repo)
            saved = self.store.identities

            for i, ident in enumerate(saved):
                self.combo.addItem(ident.display(), i)
                self.combo.setItemData(
                    self.combo.count() - 1,
                    ident.describe(),
                    Qt.ItemDataRole.ToolTipRole,
                )

            # An identity git is using but the user has not stored is still the
            # truth about this repo, so it is shown rather than silently
            # replaced by whichever stored entry happens to sort first.
            match = next(
                (i for i, s in enumerate(saved) if s.email.lower() == email.lower()),
                None,
            )
            if match is not None:
                self.combo.setCurrentIndex(match)
            else:
                self.combo.addItem(
                    f"{email} (not saved)" if email else "(no identity set)", UNSAVED
                )
                self.combo.setCurrentIndex(self.combo.count() - 1)

            self.combo.insertSeparator(self.combo.count())
            self.combo.addItem("Manage identities...", MANAGE)

            self._describe_scope(repo, email)
            self._describe_auth(repo)
        finally:
            self._loading = False

    def _describe_scope(self, repo: str, email: str) -> None:
        """Say where the identity came from -- pinned here, or inherited."""
        local_name, local_email = git_ops.get_local_identity(repo)
        if local_email:
            text = "set for this repository"
            tip = (
                f"{local_name} <{local_email}> is pinned in this repository's "
                "own config (.git/config), which outranks your global config "
                "and any includeIf rule."
            )
        elif email:
            text = "inherited from global git config"
            tip = (
                "This repository has no identity of its own, so git falls back "
                "to your global config. Pick one to pin it here."
            )
        else:
            text = "no identity configured"
            tip = (
                "Neither this repository nor your global git config sets "
                "user.email. Commits will fail until one is set."
            )

        # A repo that signs every commit with a key belonging to a different
        # identity produces commits every forge marks unverified, and nothing
        # in git says so at commit time.
        warn = ""
        if git_ops.signing_enabled(repo) and not git_ops.get_signingkey(repo):
            warn = (
                "commit.gpgsign is on but no user.signingkey resolves here, so "
                "commits will fail to sign. Give this identity a signing key on "
                "the Identities tab and select it again."
            )
        self.status.setText(f"{text} - signing key missing" if warn else text)
        self.status.setToolTip(f"{tip}\n\n{warn}" if warn else tip)
        self.status.setStyleSheet(WARN_STYLE if warn else INFO_STYLE)

    def _describe_auth(self, repo: str) -> None:
        """Say what will authenticate a push, which the identity does not decide."""
        auth = git_ops.describe_push_auth(repo)
        warning = auth.warning()
        self.auth_status.setText(auth.summary())
        self.auth_status.setStyleSheet(WARN_STYLE if warning else INFO_STYLE)
        self.auth_status.setToolTip(
            warning
            or (
                "The credential that will be used for a push. Set separately "
                "from the committer identity."
            )
        )

    # ---- selection ---------------------------------------------------------
    def _on_selected(self, _index: int) -> None:
        if self._loading:
            return
        data = self.combo.currentData()
        if data == MANAGE:
            self.refresh()  # the entry is not an identity; do not leave it showing
            self.manageRequested.emit()
            return
        if data == UNSAVED or not isinstance(data, int):
            return  # already the current identity; nothing to write

        repo = self._repo or self.settings.active_repo
        if not repo or data >= len(self.store.identities):
            return
        ident = self.store.identities[data]
        result = git_ops.set_identity(
            repo, ident.name, ident.email, ident.signingkey
        )
        if not result.ok:
            QMessageBox.warning(
                self,
                "Could not set identity",
                result.stderr.strip() or "git config failed.",
            )
        self.refresh()
        if result.ok:
            self.identityChanged.emit()
