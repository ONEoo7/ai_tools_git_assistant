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

from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPalette
from PyQt6.QtWidgets import (
    QWIDGETSIZE_MAX,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QWidget,
)

from git_assistant import git_ops, providers, repo_config
from git_assistant.config import RepoEntry, Settings
from git_assistant.identities import IdentityStore
from git_assistant.ui import theme
from git_assistant.ui.repo_picker import branch_colour

#: Combo entry that opens the Identities tab instead of selecting anything.
MANAGE = "__manage__"

#: Combo entry for an identity that is in git but not in the stored set.
UNSAVED = "__unsaved__"

INFO_STYLE = "color: #8ab;"
WARN_STYLE = "color: #b36b00;"


class _Shrinking(QLabel):
    """A readout that can be drawn narrower than its text, cut short with "…".

    A plain label given less room than its text is clipped mid-letter, with
    nothing to say that there was more. Only what is painted is cut: `text()`
    is still the whole text, and the tooltip goes on saying what it said.
    """

    def __init__(self, mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight) -> None:
        super().__init__("")
        self._mode = mode
        # Drawn as it reads, never as markup: a repository's label and a model's
        # name are someone else's text, and what is cut is characters.
        self.setTextFormat(Qt.TextFormat.PlainText)

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        hint = super().minimumSizeHint()
        # Two letters and the ellipsis: any narrower, and all it could show is
        # that something was cut.
        least = self.fontMetrics().horizontalAdvance("MM…")
        return QSize(min(hint.width(), least), hint.height())

    def shown(self) -> str:
        """What is painted: the text, cut short to the width it has been given."""
        return self.fontMetrics().elidedText(
            self.text(), self._mode, self.contentsRect().width()
        )

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        try:
            self.style().drawItemText(
                painter,
                self.contentsRect(),
                self.alignment().value,
                self.palette(),
                self.isEnabled(),
                self.shown(),
                self.foregroundRole(),
            )
        finally:
            painter.end()


class _Divider(QWidget):
    """The upright line between one group on the bar and the next."""

    #: The line and the room either side of it.
    WIDTH = 9

    #: How much of the text colour the line is drawn with, out of 255: enough to
    #: see where one group ends, not so much that it reads as a character.
    INK = 80

    def __init__(self) -> None:
        super().__init__()
        # Fixed by policy rather than by `setFixedWidth`: an explicit minimum is
        # one a row with no room left would have to draw over its neighbours.
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        theme.on_change(self._repaint)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt naming
        return QSize(self.WIDTH, self.fontMetrics().height())

    def colour(self) -> QColor:
        """The text colour of the theme in force, faded.

        Read off the application's palette at the time, as the branch's green
        is: a widget's own palette is not told when the theme changes.
        """
        ink = QApplication.palette().color(QPalette.ColorRole.WindowText)
        ink.setAlpha(self.INK)
        return ink

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        # As tall as a line of the text beside it, and centred on it.
        height = min(self.fontMetrics().height(), self.height())
        line = QRect(self.width() // 2, (self.height() - height) // 2, 1, height)
        painter = QPainter(self)
        try:
            painter.fillRect(line, self.colour())
        finally:
            painter.end()

    def _repaint(self) -> None:
        self.update()


class _Row(QHBoxLayout):
    """The bar's row, which decides what is cut short when there is no room.

    Qt's own row takes the same few pixels from everything that can spare them,
    which cut "gpt-4o-mini" to "gpt…" to spare a sentence that had plenty to
    lose. Here the readouts give way one at a time, in the order they were
    offered: each only as far as it still reads, and only once every one of
    them is that short do they carry on, in the same order, to an ellipsis.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._giving: list[tuple[QWidget, int]] = []

    def give_way(self, widget: QWidget, readable: int) -> None:
        """Let `widget` be cut short, after everything offered before it.

        It is cut down to `readable` characters' width before anything offered
        after it is cut at all, and past that only once everything is that short.
        """
        self._giving.append((widget, readable))

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 - Qt naming
        whole = [widget.sizeHint().width() for widget, _ in self._giving]
        # Qt's width for the row counts each readout as no wider than it was
        # last allowed to be, so what that took off is added back.
        wanted = super().sizeHint().width() + sum(
            width - min(width, widget.maximumWidth())
            for (widget, _), width in zip(self._giving, whole)
        )
        short = wanted - rect.width()
        widths = list(whole)
        for floor in (self._readable, self._least):
            for i, (widget, readable) in enumerate(self._giving):
                give = max(0, min(short, widths[i] - floor(widget, readable)))
                widths[i] -= give
                short -= give
        for (widget, _), width, full in zip(self._giving, widths, whole):
            widget.setMaximumWidth(width if width < full else QWIDGETSIZE_MAX)
        super().setGeometry(rect)

    @staticmethod
    def _readable(widget: QWidget, characters: int) -> int:
        return widget.fontMetrics().averageCharWidth() * characters

    @staticmethod
    def _least(widget: QWidget, _characters: int) -> int:
        return widget.minimumSizeHint().width()


class IdentityBar(QWidget):
    """"Commit as: <email>" for the active repository."""

    #: Emitted after the active repository's identity has been changed, so the
    #: rest of the window can re-read anything that quoted the old one.
    identityChanged = pyqtSignal()  # noqa: N815 - Qt signal naming

    #: Emitted when the user picks "Manage identities...", so the window can
    #: bring the Identities tab forward.
    manageRequested = pyqtSignal()  # noqa: N815 - Qt signal naming

    #: Emitted after the active repository's settings tier has been changed, so
    #: the pane that edits those files can show the one now in force.
    settingsTierChanged = pyqtSignal()  # noqa: N815 - Qt signal naming

    def __init__(self, settings: Settings, store: IdentityStore, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.store = store
        self._repo = ""
        self._loading = False

        self.combo = QComboBox()
        # As wide as its longest entry, and no wider: a fixed width reserved
        # room the rest of the row now needs, and a width fixed when it was
        # first shown would cut short an identity added after that.
        self.combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.combo.currentIndexChanged.connect(self._on_selected)

        self.status = _Shrinking()
        self.status.setStyleSheet(INFO_STYLE)

        # Deliberately its own readout rather than more tooltip on the combo.
        # "Commit as" is only half the answer, and the half it leaves out is
        # the one people assume it covers.
        self.auth_status = _Shrinking()
        self.auth_status.setStyleSheet(INFO_STYLE)

        # Which of the three sets of settings this repository runs on. Here
        # rather than on the tab that edits them, for the same reason the
        # identity is here: it belongs to the repository being worked on, it
        # decides what every tab does, and it was previously only visible on
        # the tab you would have to already suspect something to go and open.
        self.tier_combo = QComboBox()
        self.tier_combo.setToolTip(
            "Which settings this repository uses. They are not combined -- the "
            "one chosen here is the one that applies."
        )
        for tier in repo_config.Tier:
            self.tier_combo.addItem(tier.label(), tier.value)
        self.tier_combo.currentIndexChanged.connect(self._on_tier_selected)

        # The long version is on the tab that can do something about it. Here it
        # is a flag, because a sentence in this row would push the identity off
        # the side of the window to say something that is true most of the time
        # and worth acting on rarely.
        self.tier_warning = QLabel("")
        self.tier_warning.setStyleSheet(WARN_STYLE)

        # Which repository every tab is working on, and the branch it is on.
        # Here because the repository list folds away, and this is what it was
        # mostly being read for -- so the list only has to be opened to switch.
        # Cut from the middle, keeping the folder it sits in and the end of its
        # own name; the branch from the left, as the repository list cuts it.
        self.repo_name = _Shrinking(Qt.TextElideMode.ElideMiddle)
        self.repo_branch = _Shrinking(Qt.TextElideMode.ElideLeft)
        self._restyle_branch()
        theme.on_change(self._restyle_branch)

        # What every run is generated with: the provider, and the model it will
        # be asked for. Chosen on Connection & Model and in the Commit tab's
        # folded Inference section, and named here for the same reason the
        # repository is -- it decides what the next run does.
        self.inference_name = QLabel("")
        self.inference_model = _Shrinking()

        box = _Row(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(QLabel("Commit as:"))
        box.addWidget(self.combo)
        box.addWidget(self.status)
        box.addWidget(_Divider())
        box.addWidget(QLabel("Active Settings:"))
        box.addWidget(self.tier_combo)
        box.addWidget(self.tier_warning)
        box.addWidget(_Divider())
        box.addWidget(QLabel("Active Repository:"))
        box.addWidget(self.repo_name)
        box.addWidget(self.repo_branch)
        box.addWidget(_Divider())
        box.addWidget(QLabel("Active Inference:"))
        box.addWidget(self.inference_name)
        box.addWidget(self.inference_model)
        # The slack goes here, so where a push goes stays over at the right,
        # against the theme picker -- and the line after it divides it from that.
        box.addStretch(1)
        box.addWidget(_Divider())
        box.addWidget(QLabel("Push to:"))
        box.addWidget(self.auth_status)
        box.addWidget(_Divider())
        # What a window too narrow for all of it cuts short first: the readouts
        # that explain -- each has its tooltip -- before the names that say what
        # is in use. The captions, the combos and the provider stay whole for as
        # long as anything here can still give.
        box.give_way(self.status, 10)
        box.give_way(self.auth_status, 14)
        box.give_way(self.repo_name, 14)
        box.give_way(self.repo_branch, 12)
        box.give_way(self.inference_model, 14)

        self.refresh()

    # ---- display -----------------------------------------------------------
    def set_repo(self, path: str) -> None:
        self._repo = path or ""
        self.refresh()

    def show_active_repository(self) -> None:
        """Name the active repository and the branch it is on.

        Cheap enough to call on every checkout: the branch is read out of .git
        rather than asked of git, and none of the identity is looked up again.
        """
        repo = self._repo or self.settings.active_repo
        if not repo:
            self.repo_name.setText("(none)")
            self.repo_name.setToolTip("")
            self.repo_branch.setText("")
            self.repo_branch.setToolTip("")
            return
        entry = next((r for r in self.settings.repos if r.path == repo), None)
        # As the repository list names it, label and all.
        self.repo_name.setText((entry or RepoEntry(path=repo)).display())
        self.repo_name.setToolTip(repo)
        branch = git_ops.head_branch(repo)
        self.repo_branch.setText(branch)
        self.repo_branch.setToolTip(f"On branch {branch}" if branch else "")

    def show_active_inference(self) -> None:
        """Name the provider every run uses, and the model it will ask for.

        Application-wide rather than per repository, so it is read from the
        settings as they stand and asks nothing of git.
        """
        provider = providers.get(self.settings.provider)
        # The label, not `display()`: "(experimental)" belongs to choosing it, and
        # the tooltip keeps it for anyone who wants the long form.
        self.inference_name.setText(provider.label)
        self.inference_name.setToolTip(provider.display())
        model = self.settings.active_model()
        self.inference_model.setText(model or "no model selected")
        self.inference_model.setStyleSheet(INFO_STYLE if model else WARN_STYLE)
        self.inference_model.setToolTip(
            "The model runs are generated with. Chosen on Connection & Model."
            if model
            else "No model is selected for this provider yet: choose one on "
            "Connection & Model before generating."
        )

    def _restyle_branch(self) -> None:
        """The same green the repository list gives a branch, for this theme.

        Worked out from the application's palette rather than this widget's, as
        the cards do: a stylesheet pins a palette on the widget it is set on,
        and a pinned palette stops following the theme.
        """
        colour = branch_colour(QApplication.palette(), QPalette.ColorRole.Window)
        self.repo_branch.setStyleSheet(f"color: {colour.name()};")

    def refresh(self) -> None:
        """Rebuild from the active repository's *current* git identity."""
        self._loading = True  # repopulating must not look like a user choice
        try:
            self.combo.clear()
            repo = self._repo or self.settings.active_repo
            self.show_active_repository()
            self.show_active_inference()
            self._show_tier(repo)
            if not repo:
                self.combo.setEnabled(False)
                self.status.setText("No repository selected")
                self.status.setToolTip("")
                # As Active Repository says it, rather than a caption with
                # nothing after it.
                self.auth_status.setText("(none)")
                self.auth_status.setStyleSheet(INFO_STYLE)
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

    # ---- which settings are in force ---------------------------------------
    def _show_tier(self, repo: str) -> None:
        """Put the combo on the tier in force, without calling that a choice."""
        self.tier_combo.setEnabled(bool(repo))
        if not repo:
            self.tier_warning.setText("")
            self.tier_warning.setToolTip("")
            self.tier_warning.setVisible(False)
            return

        tier = repo_config.effective_tier(repo, self.settings.settings_tier(repo))
        index = self.tier_combo.findData(tier.value)
        self.tier_combo.blockSignals(True)
        self.tier_combo.setCurrentIndex(max(0, index))
        self.tier_combo.blockSignals(False)

        # A repository carrying settings that nobody is reading. Worth saying
        # because it is invisible otherwise: the file is right there in the
        # working tree, checked in, and being ignored.
        stranded = tier is not repo_config.Tier.REPO and repo_config.has_repo_config(
            repo
        )
        self.tier_warning.setText("Repo settings exist" if stranded else "")
        # Hidden rather than empty: an empty label still takes the row's spacing,
        # and the line after it then stands further off this group than the next.
        self.tier_warning.setVisible(stranded)
        self.tier_warning.setToolTip(
            "Not recommended setup, Repo settings exist.\n\n"
            f"This repository has {repo_config.path_for(repo_config.Tier.REPO, repo)}, "
            f"which is checked in and shared, but it is running on its "
            f"{tier.label()} settings instead."
            if stranded
            else ""
        )

    def _on_tier_selected(self, _index: int) -> None:
        if self._loading:
            return
        repo = self._repo or self.settings.active_repo
        if not repo:
            return
        self.settings.set_settings_tier(repo, self.tier_combo.currentData())
        self.settings.save()
        self._show_tier(repo)
        self.settingsTierChanged.emit()

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
        self.auth_status.setText(auth.destination())
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
