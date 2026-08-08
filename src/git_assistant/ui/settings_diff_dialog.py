"""What is about to be overwritten, before it is.

Shown when saving would replace a Custom settings file that already exists.
Overwriting one silently is the sort of thing that is fine ninety-nine times
and unforgivable once, and the difference between the two is exactly what this
window shows.
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from git_assistant import settings_diff


class SettingsDiffDialog(QDialog):
    """Two sets of settings, row by row, and a question."""

    def __init__(
        self,
        before: dict,
        after: dict,
        *,
        title: str,
        question: str,
        before_label: str = "Now",
        after_label: str = "After saving",
        accept_text: str = "Overwrite",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(640, 380)

        box = QVBoxLayout(self)
        ask = QLabel(question)
        ask.setWordWrap(True)
        box.addWidget(ask)

        self.changes = settings_diff.differences(before, after)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Setting", before_label, after_label])
        self.tree.setRootIsDecorated(False)
        self.tree.setColumnWidth(0, 240)
        for change in self.changes:
            item = QTreeWidgetItem(
                [
                    change.key,
                    change.shown(change.before),
                    change.shown(change.after),
                ]
            )
            item.setToolTip(0, change.describe())
            self.tree.addTopLevelItem(item)
        box.addWidget(self.tree, 1)

        self.summary = QLabel(settings_diff.summarise(self.changes))
        self.summary.setStyleSheet("color: #888;")
        box.addWidget(self.summary)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel,
        )
        self.accept_btn = buttons.addButton(
            accept_text, QDialogButtonBox.ButtonRole.AcceptRole
        )
        # Cancel by default: the accepting button is the one that overwrites,
        # and a return key pressed at a window nobody read should not do that.
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)

    def wanted(self) -> bool:
        return self.exec() == QDialog.DialogCode.Accepted
