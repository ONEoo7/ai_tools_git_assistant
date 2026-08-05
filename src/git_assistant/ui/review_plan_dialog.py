"""What a review is about to do, before it does any of it.

Every file it will send, in which language, at which version, against which
rules -- and what that costs. Shown after Review is pressed and before the first
request goes out, because a review of forty files is forty calls and a language
detected wrongly is a whole file judged by rules that were never meant for it.

The language column is editable. That is the honest answer to an ambiguous
extension: `.h` is C or C++ and no amount of sniffing settles it in every
repository, so the guess is shown where it can be corrected, and the correction
is remembered on the profile rather than asked for again next time.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from git_assistant.review import languages

_MUTED = QColor("#888888")
_PROBLEM = QColor("#ff8080")

RUN = "Run the review"
CANCEL = "Cancel"


class ReviewPlanDialog(QDialog):
    """The files, the rules and the cost, with Run and Cancel."""

    def __init__(self, plan, estimate, parent=None) -> None:
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("Code review - about to run")
        self.setMinimumSize(820, 460)

        box = QVBoxLayout(self)

        self.summary = QLabel(_summary(plan, estimate))
        self.summary.setWordWrap(True)
        font = self.summary.font()
        font.setBold(True)
        self.summary.setFont(font)
        box.addWidget(self.summary)

        self.rule_sets = QLabel(_rule_sets(plan))
        self.rule_sets.setWordWrap(True)
        self.rule_sets.setStyleSheet("color: #8ab;")
        box.addWidget(self.rule_sets)

        self.files = QTreeWidget()
        self.files.setHeaderLabels(["File", "Language", "Version", "Rules"])
        self.files.setRootIsDecorated(False)
        header = self.files.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.files, 1)
        self._fill()

        self.note = QLabel(_note(estimate))
        self.note.setWordWrap(True)
        self.note.setStyleSheet("color: #888;")
        box.addWidget(self.note)

        buttons = QDialogButtonBox()
        self.run_btn = buttons.addButton(RUN, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton(CANCEL, QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.run_btn.setEnabled(bool(plan.reviewable()))
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(buttons)
        box.addLayout(row)

    # ---- the file list -------------------------------------------------------
    def _fill(self) -> None:
        self.files.clear()
        for file in self.plan.files:
            item = QTreeWidgetItem(
                [file.path, "", file.version_label() or "-", file.rules_label()]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, file)
            if not file.reviewable:
                item.setForeground(0, _MUTED)
                item.setForeground(3, _PROBLEM if "no rules" in file.skipped else _MUTED)
            self.files.addTopLevelItem(item)
            self.files.setItemWidget(item, 1, self._language_picker(file))

    def _language_picker(self, file) -> QComboBox:
        """The detected language, and every other one it could be.

        Editable because detection cannot always be right: a header is C or
        C++, and only this repository knows which.
        """
        combo = QComboBox()
        combo.addItem("Not reviewed", "")
        for language in languages.ids():
            combo.addItem(languages.label_of(language), language)
        index = combo.findData(file.language)
        combo.setCurrentIndex(index if index >= 0 else 0)
        combo.currentIndexChanged.connect(
            lambda _i, f=file, c=combo: self._on_language_changed(f, c.currentData())
        )
        return combo

    def _on_language_changed(self, file, language: str) -> None:
        """Re-plan this one file. What is shown is what will run."""
        self._relanguage(file, language)
        self._refresh_row(file)
        self.summary.setText(_summary(self.plan, self._estimate()))
        self.rule_sets.setText(_rule_sets(self.plan))
        self.run_btn.setEnabled(bool(self.plan.reviewable()))

    #: Supplied by the panel: given a language, the rules for it. Set after
    #: construction so the dialog stays free of the profile machinery.
    rules_for = None
    #: Supplied by the panel: prices the plan as it now stands.
    price = None

    def _relanguage(self, file, language: str) -> None:
        file.language = language
        file.version = ""
        if not language:
            file.table = None
            file.skipped = "not reviewed: no language chosen"
            return
        table = self.rules_for(language, "") if self.rules_for else None
        file.table = table
        file.skipped = (
            "" if table and table.rules else f"no rules apply to {languages.label_of(language)}"
        )

    def _estimate(self):
        return self.price(self.plan) if self.price else None

    def _refresh_row(self, file) -> None:
        for index in range(self.files.topLevelItemCount()):
            item = self.files.topLevelItem(index)
            if item.data(0, Qt.ItemDataRole.UserRole) is file:
                item.setText(2, file.version_label() or "-")
                item.setText(3, file.rules_label())
                item.setForeground(0, QColor() if file.reviewable else _MUTED)
                return

    def overrides(self) -> dict[str, str]:
        """The corrections made here, by extension, to remember on the profile."""
        from git_assistant.metrics import ext_of

        out: dict[str, str] = {}
        for index in range(self.files.topLevelItemCount()):
            file = self.files.topLevelItem(index).data(0, Qt.ItemDataRole.UserRole)
            if file.language:
                out[ext_of(file.path).lower()] = file.language
        return out


# ---- what it says ----------------------------------------------------------------
def _summary(plan, estimate) -> str:
    files = len(plan.reviewable())
    skipped = len(plan.skipped())
    parts = [f"{files} file(s) will be reviewed"]
    if skipped:
        parts.append(f"{skipped} will not")
    text = ", ".join(parts) + "."
    if estimate is not None and estimate.calls:
        text += " " + estimate.summary()
    return text


def _rule_sets(plan) -> str:
    tables = plan.tables()
    if not tables:
        return "No rules apply to any of these files."
    where = f"Profile: {plan.profile}. " if plan.profile else ""
    return f"{where}Rule sets in use: {', '.join(tables)}."


def _note(estimate) -> str:
    if estimate is None:
        return ""
    if estimate.problem:
        return estimate.problem
    return " ".join(estimate.lines)


def confirm(parent, plan, estimate, *, rules_for=None, price=None) -> bool:
    """Show the plan. ``True`` if the user wants it run.

    ``rules_for`` and ``price`` let the dialog re-plan a file whose language was
    corrected, without knowing anything about profiles or tokens itself.
    """
    dialog = ReviewPlanDialog(plan, estimate, parent)
    dialog.rules_for = rules_for
    dialog.price = price
    return dialog.exec() == QDialog.DialogCode.Accepted
