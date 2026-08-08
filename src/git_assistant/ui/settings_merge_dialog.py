"""Two sets of settings side by side, and one built out of both.

The settings tiers do not merge themselves -- one is in force and the others
are not -- so this is where merging happens: on purpose, one row at a time, and
with the result visible before it is written anywhere.

Rows start on the left, so a merge nobody touches is the left-hand source and
the button that saves it says so. Choosing is done to a selection rather than
per row: a difference of twenty rows is usually "take that side, except for
these two", and twenty little combo boxes is the wrong shape for that.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant import repo_config, settings_diff

#: The built-in constants, offered beside the three files. Not a tier -- see
#: repo_config -- but the only answer to "what did this look like before
#: anybody configured anything", which is the comparison people actually want.
BUILT_IN = "built-in"

_TAKEN_ROLE = Qt.ItemDataRole.UserRole + 1


class SettingsMergeDialog(QDialog):
    """Compare any two sources, take from either, and save the result."""

    def __init__(self, settings, repo_path: str, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.repo_path = repo_path
        self.saved_to: repo_config.Tier | None = None
        self.setWindowTitle("Compare and merge settings")
        self.setMinimumSize(760, 520)

        box = QVBoxLayout(self)
        box.addWidget(
            self._wrapped(
                "Take each setting from either side. Nothing is written until "
                "you save, and saving asks where."
            )
        )

        self.left_combo = self._source_combo()
        self.right_combo = self._source_combo()
        self.left_combo.currentIndexChanged.connect(self._reload)
        self.right_combo.currentIndexChanged.connect(self._reload)

        row = QHBoxLayout()
        row.addWidget(QLabel("Compare:"))
        row.addWidget(self.left_combo)
        row.addWidget(QLabel("with:"))
        row.addWidget(self.right_combo)
        row.addStretch(1)
        box.addLayout(row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Setting", "Left", "Right", "Result"])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.tree.setColumnWidth(0, 220)
        box.addWidget(self.tree, 1)

        take_row = QHBoxLayout()
        self.take_left_btn = QPushButton("Take left")
        self.take_left_btn.clicked.connect(lambda: self._take(settings_diff.LEFT))
        self.take_right_btn = QPushButton("Take right")
        self.take_right_btn.clicked.connect(lambda: self._take(settings_diff.RIGHT))
        self.all_left_btn = QPushButton("All from left")
        self.all_left_btn.clicked.connect(
            lambda: self._take(settings_diff.LEFT, everything=True)
        )
        self.all_right_btn = QPushButton("All from right")
        self.all_right_btn.clicked.connect(
            lambda: self._take(settings_diff.RIGHT, everything=True)
        )
        for button in (
            self.take_left_btn,
            self.take_right_btn,
            self.all_left_btn,
            self.all_right_btn,
        ):
            take_row.addWidget(button)
        take_row.addStretch(1)
        box.addLayout(take_row)

        self.summary = QLabel("")
        self.summary.setStyleSheet("color: #888;")
        box.addWidget(self.summary)

        self.target_combo = QComboBox()
        for tier in repo_config.Tier:
            self.target_combo.addItem(f"Save to {tier.label()}", tier.value)
        self.target_combo.setCurrentIndex(
            self.target_combo.findData(repo_config.Tier.CUSTOM.value)
        )
        self.save_btn = QPushButton("Save")
        self.save_btn.clicked.connect(self._on_save)
        self.discard_btn = QPushButton("Discard")
        self.discard_btn.clicked.connect(self.reject)

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        save_row.addWidget(self.target_combo)
        save_row.addWidget(self.save_btn)
        save_row.addWidget(self.discard_btn)
        box.addLayout(save_row)

        # Whatever is in force against what a repository ships: the comparison
        # somebody opening this window is most often here to make.
        in_force = repo_config.effective_tier(
            repo_path, settings.settings_tier(repo_path)
        )
        self.left_combo.setCurrentIndex(self.left_combo.findData(in_force.value))
        other = (
            repo_config.Tier.REPO
            if in_force is not repo_config.Tier.REPO
            else repo_config.Tier.USER
        )
        self.right_combo.setCurrentIndex(self.right_combo.findData(other.value))
        self._reload()

    # ---- what is being compared ---------------------------------------------
    def _wrapped(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        return label

    def _source_combo(self) -> QComboBox:
        combo = QComboBox()
        for tier in repo_config.Tier:
            # Said in the name: comparing against a file that is not there is a
            # legitimate thing to do, and a surprising thing to do by accident.
            missing = "" if repo_config.exists(tier, self.repo_path) else "  (none)"
            combo.addItem(f"{tier.label()}{missing}", tier.value)
        combo.addItem("Built-in defaults", BUILT_IN)
        return combo

    def _source(self, combo: QComboBox) -> dict:
        key = combo.currentData()
        tier = None if key == BUILT_IN else repo_config.tier_of(key)
        return repo_config.source_dict(tier, self.repo_path)

    def _reload(self) -> None:
        """Rebuild the rows. Every choice made so far is a choice about these."""
        left, right = self._source(self.left_combo), self._source(self.right_combo)
        self.tree.clear()
        for change in settings_diff.compare(left, right):
            item = QTreeWidgetItem(
                [
                    change.key,
                    change.shown(change.before),
                    change.shown(change.after),
                    "",
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, change)
            item.setData(0, _TAKEN_ROLE, settings_diff.LEFT)
            if not change.differs:
                # Nothing to choose, so nothing that reads as a choice.
                item.setDisabled(True)
            self.tree.addTopLevelItem(item)
        self._refresh_results()

    # ---- choosing -------------------------------------------------------------
    def _rows(self, only_selected: bool):
        items = (
            self.tree.selectedItems()
            if only_selected
            else [
                self.tree.topLevelItem(i)
                for i in range(self.tree.topLevelItemCount())
            ]
        )
        return [item for item in items if not item.isDisabled()]

    def _take(self, side: str, *, everything: bool = False) -> None:
        for item in self._rows(only_selected=not everything):
            item.setData(0, _TAKEN_ROLE, side)
        self._refresh_results()

    def _refresh_results(self) -> None:
        taken_right = 0
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            change = item.data(0, Qt.ItemDataRole.UserRole)
            side = item.data(0, _TAKEN_ROLE)
            value = change.before if side == settings_diff.LEFT else change.after
            item.setText(3, change.shown(value))
            if change.differs and side == settings_diff.RIGHT:
                taken_right += 1
        differing = sum(1 for item in self._rows(only_selected=False))
        self.summary.setText(
            f"{differing} difference(s); {taken_right} taken from the right."
            if differing
            else "No differences between these two."
        )

    def taken(self) -> dict[str, str]:
        return {
            self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole).key: (
                self.tree.topLevelItem(i).data(0, _TAKEN_ROLE)
            )
            for i in range(self.tree.topLevelItemCount())
        }

    def result(self) -> dict:
        """The merged settings, as they would be written."""
        return settings_diff.merged(
            self._source(self.left_combo), self._source(self.right_combo), self.taken()
        )

    # ---- saving ---------------------------------------------------------------
    def _on_save(self) -> None:
        from git_assistant.ui.settings_diff_dialog import SettingsDiffDialog

        tier = repo_config.tier_of(self.target_combo.currentData())
        if tier is None:
            return
        merged = self.result()
        if repo_config.exists(tier, self.repo_path) and not SettingsDiffDialog(
            repo_config.source_dict(tier, self.repo_path),
            merged,
            title=f"Replace the {tier.label()} settings",
            question=(
                f"The {tier.label()} settings already exist. Saving the merge "
                "replaces them:"
            ),
            before_label=f"{tier.label()} now",
            after_label="After saving",
            parent=self,
        ).wanted():
            return

        problem = repo_config.write_text(
            tier, self.repo_path, repo_config.text_from(merged)
        )
        if problem:
            self.summary.setText(problem)
            return
        self.saved_to = tier
        self.accept()
