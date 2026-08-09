"""Two ways to name a new branch, drawn as the audit cards are.

A project that names its branches ``dev/rem/<who>/<what>`` gets that wrong once
per branch until somebody automates it, and the automation nobody can see is
the one nobody trusts. So the convention is a pattern, the patterns are offered
in a dropdown, and the name that will be created is on screen before the button
is pressed.

Two cards rather than a dropdown with an "(none)" entry at the top. They are
not two values of one setting -- one has a pattern, a user and a name, the
other has a name -- and a card that grows three fields when an entry is chosen
moves everything under it. The plain one is selected by default, because most
branches are not part of anybody's convention.

Cards and not radio buttons in a row for the same reason the audits are cards:
what belongs to a choice is drawn inside that choice's border, so the user
field cannot be mistaken for something the plain name will use.

Nothing here runs git or writes a file. The panel owns the repository, asks the
selected card what it would create, and does the creating.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QRadioButton,
    QVBoxLayout,
)

from git_assistant import repo_config
from git_assistant.ui import theme
from git_assistant.ui.audit_cards import MUTED_COLOUR, card_stylesheet

#: How far a card's fields sit in from its title, so they read as belonging to
#: the line above rather than to the card below.
INDENT = 22


class BranchCard(QFrame):
    """One way of naming a branch: a title, a name field, and what it adds."""

    #: This card is the one being used. Emitted for a click anywhere on it, not
    #: only on the radio button -- the whole card is the target.
    picked = pyqtSignal()
    #: Something typed or chosen. The panel re-renders the preview.
    changed = pyqtSignal()
    #: Enter in the name field: create it.
    submitted = pyqtSignal()

    def __init__(self, title: str, description: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("branchCard")
        self.setProperty("selected", False)
        self.restyle()
        # Baked colours cannot follow a palette; being told is the only way.
        # Held weakly there, so there is nothing to unregister here.
        theme.on_change(self.restyle)

        box = QVBoxLayout(self)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(6)

        self.radio = QRadioButton(title)
        font = self.radio.font()
        font.setBold(True)
        self.radio.setFont(font)
        self.radio.clicked.connect(self.picked.emit)
        box.addWidget(self.radio)

        note = QLabel(description)
        note.setWordWrap(True)
        note.setStyleSheet(MUTED_COLOUR)
        note.setContentsMargins(INDENT, 0, 0, 0)
        box.addWidget(note)

        self.fields = QVBoxLayout()
        self.fields.setContentsMargins(INDENT, 0, 0, 0)
        self.fields.setSpacing(4)
        box.addLayout(self.fields)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("what you are about to work on")
        self.name_edit.textChanged.connect(lambda _t: self.changed.emit())
        self.name_edit.returnPressed.connect(self.submitted.emit)
        self._row("Name:", self.name_edit)

    def _row(self, label: str, widget) -> None:
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        row.addWidget(widget, 1)
        self.fields.addLayout(row)

    # ---- what it would create -----------------------------------------------
    def typed(self) -> str:
        return self.name_edit.text().strip()

    def pattern(self) -> str:
        """The pattern in force on this card. Blank is the plain name."""
        return ""

    def typed_user(self) -> str:
        """A ``{user}`` typed here, or blank to let the settings and git answer."""
        return ""

    # ---- which one is in use ------------------------------------------------
    def set_selected(self, on: bool) -> None:
        """Colour and the radio, and nothing else. Choosing must not resize."""
        self.radio.setChecked(on)
        self.setProperty("selected", on)
        # A property a stylesheet selects on is read when the widget is
        # polished, and that already happened.
        self.style().unpolish(self)
        self.style().polish(self)
        for i in range(self.fields.count()):
            layout = self.fields.itemAt(i).layout()
            for j in range(layout.count() if layout else 0):
                widget = layout.itemAt(j).widget()
                if widget is not None:
                    widget.setEnabled(on)

    def restyle(self) -> None:
        """Work the card's colours out from the theme in force right now.

        The application's palette, not this widget's: setting a stylesheet that
        names a background makes Qt pin a palette on the widget, and a pinned
        palette stops following the application's.
        """
        self.setStyleSheet("")  # drop the pinned palette before reading one
        self.setStyleSheet(card_stylesheet(QApplication.palette(), "branchCard"))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        """Anywhere on the card means "this is the one I am using"."""
        if event.button() == Qt.MouseButton.LeftButton:
            self.picked.emit()
        super().mousePressEvent(event)


class PlainBranchCard(BranchCard):
    """The name as typed, and nothing added to it."""

    def __init__(self, parent=None) -> None:
        super().__init__(
            "New Branch",
            "Exactly what you type. Slashes are kept, so feature/login is a "
            "branch called feature/login.",
            parent,
        )


class PatternBranchCard(BranchCard):
    """A pattern from the settings, filled in with who you are and what it is."""

    def __init__(self, parent=None) -> None:
        super().__init__(
            "New Branch from patterns",
            "A naming convention from the settings in force. Who {user} is "
            "comes from git unless you say otherwise.",
            parent,
        )
        self.pattern_combo = QComboBox()
        self.pattern_combo.currentIndexChanged.connect(
            lambda _i: self.changed.emit()
        )
        self.user_edit = QLineEdit()
        self.user_edit.setPlaceholderText("from git")
        self.user_edit.textChanged.connect(lambda _t: self.changed.emit())

        # Inserted above the name, which the base class added: the pattern
        # decides the shape and the name fills the last piece of it, and
        # reading them in the other order explains nothing.
        self.fields.insertLayout(0, self._made_row("Pattern:", self.pattern_combo))
        self.fields.insertLayout(1, self._made_row("User:", self.user_edit))

    @staticmethod
    def _made_row(label: str, widget) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel(label))
        row.addWidget(widget, 1)
        return row

    def show_patterns(self, patterns, chosen: str = "") -> None:
        """Offer these, selecting ``chosen`` if it is one of them.

        A pattern that was chosen and is no longer offered -- the settings
        changed, or another tier came into force -- is added back rather than
        silently dropped, because dropping it would rename the next branch
        without saying so.
        """
        offered = list(patterns)
        if chosen and chosen not in offered:
            offered.insert(0, chosen)
        self.pattern_combo.blockSignals(True)
        self.pattern_combo.clear()
        for pattern in offered:
            self.pattern_combo.addItem(pattern, pattern)
        if chosen:
            self.pattern_combo.setCurrentIndex(max(0, offered.index(chosen)))
        self.pattern_combo.blockSignals(False)

    def pattern(self) -> str:
        return self.pattern_combo.currentData() or ""

    def typed_user(self) -> str:
        return self.user_edit.text().strip()

    def show_user(self, user: str) -> None:
        self.user_edit.blockSignals(True)
        self.user_edit.setText(user)
        self.user_edit.blockSignals(False)


def offered_or_default(rules) -> list[str]:
    """What to put in the dropdown, never empty.

    A repository whose settings list no patterns still gets the built-in ones:
    an empty dropdown beside a card called "from patterns" is a dead end with
    nothing saying why.
    """
    return rules.offered() or list(repo_config.DEFAULT_PATTERNS)
