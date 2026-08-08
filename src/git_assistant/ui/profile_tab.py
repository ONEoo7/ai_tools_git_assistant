"""The Profiles tab: which rules apply to which language, at which version.

Every profile there is on the left; the one picked there is opened on the right.
One row per language it covers, each carrying the version it is written in and
the rules that will be checked. The version matters twice over: it decides which
shipped rules apply, and it is told to the model, so getting it wrong is not
cosmetic.

Which profile is *looked at* here and which one a review *runs against* are two
different choices. The second belongs to the Rules profile dropdown beside the
repository, and is said again under the list so that reading one profile is
never mistaken for selecting it.

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
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant.review import builtin, languages, rule_files
from git_assistant.review import profiles as profiles_mod
from git_assistant.review.profiles import LanguageRules, Profile, Selection

MUTED = "color: #888;"
_MUTED = QColor("#888888")
_DETECTED = QColor("#8ab0cc")

NOT_SET = "not set - every rule applies"

#: Gap either side of the handle between the list and the profile it opens.
LIST_GAP = 8


class ProfileTab(QWidget):
    """Pick a profile from the list, then edit its languages, versions and rules."""

    #: The profile changed and should be saved.
    changed = pyqtSignal()
    #: Another profile was picked from the list, by name. Which profile is read
    #: here says nothing about which one a review uses.
    selected = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._profile: Profile | None = None
        self._store = None
        self._detected: dict[str, str] = {}
        self._sources: dict[str, str] = {}

        outer = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_list_pane())
        split.addWidget(self._build_editor_pane())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([180, 540])
        outer.addWidget(split, 1)

    def _build_list_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        # A handle on the right only, so a margin on the right only.
        box.setContentsMargins(0, 0, LIST_GAP, 0)
        box.addWidget(QLabel("Profiles"))

        self.profiles_list = QListWidget()
        self.profiles_list.setToolTip(
            "Pick a profile to read or edit. Which one a review runs against is "
            "chosen under Rules profile, beside the repository."
        )
        self.profiles_list.currentItemChanged.connect(self._on_picked)
        box.addWidget(self.profiles_list, 1)

        self.in_use = QLabel("")
        self.in_use.setWordWrap(True)
        self.in_use.setStyleSheet(MUTED)
        box.addWidget(self.in_use)
        return pane

    def _build_editor_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(LIST_GAP, 0, 0, 0)
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
        return pane

    # ---- the list of profiles ---------------------------------------------------
    def show_profiles(self, profiles: list, current: str, in_use: str = "") -> None:
        """List every profile, marking the one open and the one under review."""
        self.profiles_list.blockSignals(True)
        self.profiles_list.clear()
        for profile in profiles:
            item = QListWidgetItem(profile.display())
            item.setData(Qt.ItemDataRole.UserRole, profile.name)
            if profile.name == in_use:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setToolTip("A review of this repository runs against this one.")
            elif profile.from_repository():
                item.setToolTip("Shipped by this repository. Read-only here.")
            self.profiles_list.addItem(item)
            if profile.name == current:
                self.profiles_list.setCurrentItem(item)
        self.profiles_list.blockSignals(False)
        self.in_use.setText(_in_use_note(in_use))

    def _on_picked(self, current: QListWidgetItem, _previous=None) -> None:
        if current is not None:
            self.selected.emit(current.data(Qt.ItemDataRole.UserRole))

    def profile(self) -> Profile | None:
        """The profile on screen -- the object edits were made to, not a copy."""
        return self._profile

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
            return rule_files.table_for(
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


def _in_use_note(in_use: str) -> str:
    """Which profile a review uses, said where a profile is chosen to read.

    Reading one and reviewing against another is the point of the split, and it
    is also exactly the mistake it invites, so the answer is on screen rather
    than a tab away.
    """
    if not in_use:
        return "No profile is set for this repository yet."
    return f"Reviews of this repository run against '{in_use}' (in bold)."


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
