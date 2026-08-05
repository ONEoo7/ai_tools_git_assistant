"""The Profile tab: which rules apply to which language, at which version.

One row per language the profile covers, each carrying the version it is
written in and the rules that will be checked. The version matters twice over:
it decides which shipped rules apply, and it is told to the model, so getting it
wrong is not cosmetic.

What the repository declares about itself is filled in and labelled with where
it came from. Everything it does not declare is a dropdown, because the honest
answer to "which C++ is this" in a repository that never says is to ask.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant.review import builtin, languages, profiles as profiles_mod
from git_assistant.review.profiles import LanguageRules, Profile, Selection

MUTED = "color: #888;"
_MUTED = QColor("#888888")
_DETECTED = QColor("#8ab0cc")

NOT_SET = "not set - every rule applies"


class ProfileTab(QWidget):
    """Edit one profile: its languages, their versions, and their rules."""

    #: The profile changed and should be saved.
    changed = pyqtSignal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._profile: Profile | None = None
        self._store = None
        self._detected: dict[str, str] = {}
        self._sources: dict[str, str] = {}

        box = QVBoxLayout(self)
        self.header = QLabel("")
        self.header.setWordWrap(True)
        box.addWidget(self.header)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Language / rule", "Version", "Checked"])
        self.tree.setColumnWidth(0, 380)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._on_item_changed)
        box.addWidget(self.tree, 1)

        self.note = QLabel("")
        self.note.setWordWrap(True)
        self.note.setStyleSheet(MUTED)
        box.addWidget(self.note)

        row = QHBoxLayout()
        self.add_language_btn = QPushButton("Add language...")
        self.add_language_btn.setToolTip(
            "Cover a language this profile says nothing about yet."
        )
        self.remove_language_btn = QPushButton("Remove language")
        self.share_btn = QPushButton("Share with the repository")
        self.share_btn.setToolTip(
            "Write this profile into the repository, so anyone who clones it is "
            "reviewed against the same rules."
        )
        self.copy_btn = QPushButton("Copy into my rule tables")
        self.copy_btn.setToolTip(
            "Add the tables this repository shipped to your own library."
        )
        self.copy_btn.setVisible(False)
        for button in (
            self.add_language_btn,
            self.remove_language_btn,
            self.share_btn,
            self.copy_btn,
        ):
            row.addWidget(button)
        row.addStretch(1)
        box.addLayout(row)

    # ---- what it is showing ---------------------------------------------------
    def show_profile(
        self,
        profile: Profile | None,
        store,
        detected: dict[str, str],
        sources: dict[str, str] | None = None,
    ) -> None:
        """Draw ``profile``, with what the repository declared filled in."""
        self._profile = profile
        self._store = store
        self._detected = dict(detected or {})
        self._sources = dict(sources or {})
        self._fill()

    def _fill(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        profile = self._profile
        editable = profile is not None and not profile.from_repository()
        for button in (self.add_language_btn, self.remove_language_btn, self.share_btn):
            button.setEnabled(editable)
        self.copy_btn.setVisible(bool(profile and profile.from_repository()))

        if profile is None:
            self.header.setText("No profile is selected.")
            self.tree.blockSignals(False)
            return

        self.header.setText(_header(profile))
        for entry in profile.languages:
            self.tree.addTopLevelItem(self._language_row(entry))
        self.tree.blockSignals(False)
        self.note.setText(_note(profile, self._sources))

    def _language_row(self, entry: LanguageRules) -> QTreeWidgetItem:
        item = QTreeWidgetItem([entry.label(), "", ""])
        item.setData(0, Qt.ItemDataRole.UserRole, entry)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        for selection in entry.selections:
            table = self._table_of(selection, entry)
            if table is None:
                child = QTreeWidgetItem([f"{selection.target} (missing)", "", ""])
                child.setForeground(0, _MUTED)
                item.addChild(child)
                continue
            head = QTreeWidgetItem([table.name, "", f"{len(table.rules)}"])
            head.setData(0, Qt.ItemDataRole.UserRole, selection)
            item.addChild(head)
            for rule in table.rules:
                head.addChild(_rule_row(rule, selection))
        return item

    def _table_of(self, selection: Selection, entry: LanguageRules):
        version = self._version_of(entry)
        if selection.is_builtin:
            wanted = selection.target
            return builtin.table_for(
                entry.language if wanted == languages.ANY else wanted, version
            )
        return self._store.find(selection.target) if self._store else None

    def _version_of(self, entry: LanguageRules) -> str:
        return entry.version or self._detected.get(entry.language, "")

    # ---- editing --------------------------------------------------------------
    def attach_version_pickers(self) -> None:
        """Put a version dropdown on every language row.

        Done after the rows exist: a widget in a column needs its item to be in
        the tree already, and doing it during the fill loses them silently.
        """
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            entry = item.data(0, Qt.ItemDataRole.UserRole)
            if entry is None:
                continue
            self.tree.setItemWidget(item, 1, self._version_picker(entry))
            item.setExpanded(True)

    def _version_picker(self, entry: LanguageRules) -> QComboBox:
        combo = QComboBox()
        language = languages.get(entry.language)
        detected = self._detected.get(entry.language, "")
        combo.addItem(_not_set_label(language, detected), "")
        if language is not None:
            for version, label in zip(language.versions, language.version_labels):
                combo.addItem(label, version)
        index = combo.findData(entry.version)
        combo.setCurrentIndex(index if index >= 0 else 0)
        if not entry.version and detected:
            combo.setStyleSheet("color: #8ab;")
            combo.setToolTip(self._sources.get(entry.language, "detected"))
        combo.setEnabled(self._profile is not None and not self._profile.from_repository())
        combo.currentIndexChanged.connect(
            lambda _i, e=entry, c=combo: self._on_version_changed(e, c.currentData())
        )
        return combo

    def _on_version_changed(self, entry: LanguageRules, version: str) -> None:
        entry.version = version or ""
        self._fill()
        self.attach_version_pickers()
        self.changed.emit()

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        """A rule was ticked or unticked."""
        if column != 0:
            return
        payload = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(payload, tuple):
            return
        selection, rule_id = payload
        checked = item.checkState(0) == Qt.CheckState.Checked
        excluded = [x for x in selection.exclude if x != rule_id]
        if not checked:
            excluded.append(rule_id)
        selection.exclude = excluded
        self.changed.emit()

    def add_language(self, language: str) -> None:
        """Cover a language, with whatever ships for it."""
        if self._profile is None or self._profile.covers_exactly(language):
            return
        self._profile.languages.append(
            LanguageRules(
                language=language,
                selections=[Selection(builtin.ref_of(language))]
                if builtin.get(language)
                else [],
            )
        )
        self.changed.emit()
        self._fill()
        self.attach_version_pickers()

    def remove_language(self) -> None:
        item = self.tree.currentItem()
        while item is not None and item.parent() is not None:
            item = item.parent()
        if item is None or self._profile is None:
            return
        entry = item.data(0, Qt.ItemDataRole.UserRole)
        if entry is None:
            return
        self._profile.languages = [e for e in self._profile.languages if e is not entry]
        self.changed.emit()
        self._fill()
        self.attach_version_pickers()

    def missing_languages(self) -> list[str]:
        """Languages this profile does not cover yet."""
        if self._profile is None:
            return []
        covered = {e.language for e in self._profile.languages}
        return [l for l in languages.ids() if l not in covered]


def _rule_row(rule, selection: Selection) -> QTreeWidgetItem:
    item = QTreeWidgetItem([f"{rule.rule_id}: {rule.details}", "", ""])
    item.setData(0, Qt.ItemDataRole.UserRole, (selection, rule.rule_id))
    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
    item.setCheckState(
        0, Qt.CheckState.Checked if selection.keeps(rule.rule_id) else Qt.CheckState.Unchecked
    )
    return item


def _not_set_label(language, detected: str) -> str:
    if not detected:
        return NOT_SET
    label = language.label_for(detected) if language else detected
    return f"{label} (detected)"


def _header(profile: Profile) -> str:
    if profile.from_repository():
        return (
            f"{profile.name} - shipped by this repository. It is read-only here; "
            "copy its tables to make them yours."
        )
    return f"{profile.name} - {len(profile.languages)} language(s)."


def _note(profile: Profile, sources: dict[str, str]) -> str:
    unset = [
        languages.label_of(e.language)
        for e in profile.languages
        if not e.version and e.language != languages.ANY and e.language not in sources
    ]
    if unset:
        return (
            "No version is set for " + ", ".join(unset) + ", and the repository "
            "does not declare one, so every rule for those languages is checked. "
            "Set one to check only the rules that version has."
        )
    return (
        "A version in blue was read from what the repository declares; hover it "
        "to see where. Unticking a rule leaves it out of every review."
    )
