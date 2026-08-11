"""The Languages tab: what the reviewer understands, and what it will skip.

Every language a review can recognise, the file types that reach it, and the
versions a rule can be pinned to. Read-only, because none of it is a setting --
it is what this build knows, and the honest place to see it was `languages.py`.

The per-version rule counts are the point. "Which rules do I get if I say
C++17" is the question the Profiles tab makes you answer and nothing answered,
and it is not the same number for every version: a rule marked `since: c++20` is
simply absent below it. Reading the count next to the version is faster than
reading the rule set and doing the arithmetic.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant.review import languages, rule_files

MUTED = "color: #888;"

#: Selectable and no more. Qt's default flags include ItemIsUserCheckable, and
#: a box is simply not drawn until something sets a check state -- which makes
#: "this tab is a read-out" true by accident. Said here so it stays true.
_READ_ONLY = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

#: What a file whose language could not be worked out gets. Said on the tab
#: because the alternative -- reviewing it against another language's rules --
#: is the failure this whole table exists to avoid, and silence about a skipped
#: file reads as the reviewer having missed it.
SKIP_NOTE = (
    "A file whose language cannot be worked out is skipped, never guessed: "
    "reviewing it against another language's rules produces confident, wrong "
    "findings."
)


class LanguagesTab(QWidget):
    """Every supported language, its file types, and its versions."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        box = QVBoxLayout(self)
        box.addWidget(
            QLabel(
                "Languages a review can recognise. Expand one for the versions "
                "a rule can be pinned to, and how many built-in rules apply at "
                "each."
            )
        )

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Language", "File types", "Versions", "Rules"])
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.tree, 1)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(MUTED)
        box.addWidget(self.note)

        self.refresh()

    def refresh(self) -> None:
        """Re-read the rule counts. Cheap, and the files behind them are edited."""
        self.tree.clear()
        for language in languages.LANGUAGES:
            self.tree.addTopLevelItem(_language_row(language))
        self.note.setText(f"{_ambiguity_note()}  {SKIP_NOTE}")


def _language_row(language: languages.Language) -> QTreeWidgetItem:
    """One language, with a child per version it can be pinned to."""
    table = rule_files.table(language.id)
    total = len(table.rules) if table else 0
    item = QTreeWidgetItem(
        [language.label, file_types(language), version_span(language), str(total)]
    )
    item.setToolTip(1, file_types(language))
    item.setFlags(_READ_ONLY)
    font = item.font(0)
    font.setBold(True)
    item.setFont(0, font)

    for version, label in zip(language.versions, language.version_labels):
        applies = rule_files.table_for(language.id, version)
        child = QTreeWidgetItem(["", "", label, str(len(applies.rules) if applies else 0)])
        child.setFlags(_READ_ONLY)
        if total and applies is not None and len(applies.rules) < total:
            child.setToolTip(
                3,
                f"{total - len(applies.rules)} of the {total} built-in rules are "
                f"about features {label} does not have.",
            )
        item.addChild(child)
    return item


def version_span(language: languages.Language) -> str:
    """Oldest to newest, on the row that is collapsed.

    A count would sit directly above the versions it counts, which reads as a
    version rather than as how many there are.
    """
    labels = language.version_labels or language.versions
    if not labels:
        return ""
    return labels[0] if len(labels) == 1 else f"{labels[0]} – {labels[-1]}"


def file_types(language: languages.Language) -> str:
    """The extensions that reach this language, and any shebang that does.

    A shebang is how a file with no extension at all gets reviewed, which is
    most scripts, so leaving it out would make the table look wrong to anyone
    whose `bin/` is full of them.
    """
    parts = list(language.extensions)
    parts += [f"#!{word}" for word in language.shebangs]
    return ", ".join(parts)


def _ambiguity_note() -> str:
    """Which extensions two languages claim, and how that is settled."""
    if not languages.AMBIGUOUS:
        return ""
    said = "; ".join(
        f"{ext} is {' or '.join(languages.label_of(x) for x in claimants)}"
        for ext, claimants in languages.AMBIGUOUS.items()
    )
    return (
        f"{said} — settled by what the file itself contains, then by the rest of "
        "the repository, and remembered per profile once you correct it."
    )
