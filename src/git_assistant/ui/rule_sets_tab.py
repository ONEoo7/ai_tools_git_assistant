"""The Rule Sets tab: every set of rules a review can draw on.

Two kinds, listed together because from a profile's point of view they are the
same thing -- something a language can be checked against:

- **Built in**, one per language, shipped with the application and written out
  to ``<config dir>/code_review/<language>.json`` on first run. Editing that
  file is editing what the next review checks, so it is opened from here rather
  than described.
- **Mine**, the tables imported from a spreadsheet or from another machine.

This replaced a tab that showed only the second kind, and only one of them at a
time -- whichever the current profile happened to reach first. The shipped rules,
which is what nearly every review actually runs against, had no screen at all:
they were a sentence and a button that opened a folder.

The *Applies to* column is the part that cannot be got from the file. A shipped
rule carries the span of language versions it is true for, and a rule quoted
against a version that never had the feature is a confident, wrong finding --
so the span is shown beside the rule rather than left in the JSON.

Widget-only, in the idiom of `profile_tab`: it holds no settings and saves
nothing. The buttons are public and `review_panel` wires them, because renaming
or deleting a table has to keep every profile that points at it honest.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant.review import languages, rule_files
from git_assistant.review.profiles import BUILTIN, TABLE

MUTED = "color: #888;"
WARN = "color: #b36b00;"
_MUTED = QColor("#888888")

#: Gap either side of the handle between the list and the set it opens.
LIST_GAP = 8

NOTHING_SELECTED = "Pick a rule set on the left."


class RuleSetsTab(QWidget):
    """Every rule set there is; the one picked is opened beside the list."""

    #: A different set was picked, by ref ("builtin:python", "table:Mine").
    selected = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._store = None

        outer = QVBoxLayout(self)
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_list_pane())
        split.addWidget(self._build_rules_pane())
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)
        split.setSizes([220, 520])
        outer.addWidget(split, 1)

    def _build_list_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(0, 0, LIST_GAP, 0)

        self.sets_tree = QTreeWidget()
        self.sets_tree.setHeaderLabels(["Rule set", "Rules"])
        self.sets_tree.setRootIsDecorated(False)
        self.sets_tree.setIndentation(12)
        header = self.sets_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.sets_tree.currentItemChanged.connect(self._on_picked)
        box.addWidget(self.sets_tree, 1)

        self.shipped_rules_note = QLabel("")
        self.shipped_rules_note.setWordWrap(True)
        self.shipped_rules_note.setStyleSheet(MUTED)
        box.addWidget(self.shipped_rules_note)
        return pane

    def _build_rules_pane(self) -> QWidget:
        pane = QWidget()
        box = QVBoxLayout(pane)
        box.setContentsMargins(LIST_GAP, 0, 0, 0)

        self.header = QLabel("")
        self.header.setWordWrap(True)
        box.addWidget(self.header)

        self.rules_table = QTableWidget(0, 3)
        self.rules_table.setHorizontalHeaderLabels(["ruleID", "ruleDetails", "Applies to"])
        self.rules_table.verticalHeader().setVisible(False)
        self.rules_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self.rules_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        box.addWidget(self.rules_table, 1)

        self.rules_note = QLabel("")
        self.rules_note.setWordWrap(True)
        self.rules_note.setStyleSheet(MUTED)
        box.addWidget(self.rules_note)

        # Built-in sets are files, so they are opened and reset rather than
        # renamed and deleted; the user's own are the opposite. Both rows are
        # always present and greyed, so neither looks like a missing feature.
        shipped_row = QHBoxLayout()
        self.open_folder_btn = QPushButton("Open rules folder")
        self.open_folder_btn.setToolTip(
            "One file per language, with the versions each rule applies to. "
            "Editing one changes what the next review checks."
        )
        self.open_file_btn = QPushButton("Open this file")
        self.restore_btn = QPushButton("Reset to shipped")
        self.restore_btn.setToolTip(
            "Throw away your edits to this language's file and write the rules "
            "this build shipped with back over it."
        )
        for button in (self.open_folder_btn, self.open_file_btn, self.restore_btn):
            shipped_row.addWidget(button)
        shipped_row.addStretch(1)
        box.addLayout(shipped_row)

        mine_row = QHBoxLayout()
        self.import_xlsx_btn = QPushButton("Import spreadsheet...")
        self.import_xlsx_btn.setToolTip(
            "Read a .xlsx with a ruleID column and a ruleDetails column."
        )
        self.export_xlsx_btn = QPushButton("Export spreadsheet...")
        self.import_json_btn = QPushButton("Import JSON...")
        self.import_json_btn.setToolTip("Merge tables exported from another machine.")
        self.export_json_btn = QPushButton("Export JSON...")
        for button in (
            self.import_xlsx_btn,
            self.export_xlsx_btn,
            self.import_json_btn,
            self.export_json_btn,
        ):
            mine_row.addWidget(button)
        mine_row.addStretch(1)
        self.rename_table_btn = QPushButton("Rename...")
        self.delete_table_btn = QPushButton("Delete")
        mine_row.addWidget(self.rename_table_btn)
        mine_row.addWidget(self.delete_table_btn)
        box.addLayout(mine_row)
        return pane

    # ---- what it is showing -------------------------------------------------
    def show_sets(self, store, current: str = "") -> None:
        """List every rule set, re-opening ``current`` if it is still there."""
        self._store = store
        wanted = current or self.current_ref()
        # Signals stay blocked across the re-select as well: this is "draw
        # this", not a user picking something, and letting it emit would have
        # the owner redraw the tab it is in the middle of filling.
        self.sets_tree.blockSignals(True)
        self.sets_tree.clear()
        self._add_group("Built in", _builtin_rows())
        self._add_group("Mine", _table_rows(store))
        self._select(wanted)
        self.sets_tree.blockSignals(False)
        self._refresh_shipped_note()
        self._show_selected()

    def _add_group(self, title: str, rows: list[tuple[str, str, str]]) -> None:
        head = QTreeWidgetItem([title, ""])
        font = head.font(0)
        font.setBold(True)
        head.setFont(0, font)
        head.setFlags(Qt.ItemFlag.ItemIsEnabled)  # a heading, not a choice
        self.sets_tree.addTopLevelItem(head)
        if not rows:
            empty = QTreeWidgetItem(["none yet", ""])
            empty.setForeground(0, _MUTED)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            head.addChild(empty)
        for ref, label, count in rows:
            item = QTreeWidgetItem([label, count])
            item.setData(0, Qt.ItemDataRole.UserRole, ref)
            head.addChild(item)
        head.setExpanded(True)

    def _select(self, ref: str) -> None:
        """Re-open a set by ref, falling back to the first there is."""
        first = None
        for item in self._set_items():
            if first is None:
                first = item
            if item.data(0, Qt.ItemDataRole.UserRole) == ref:
                self.sets_tree.setCurrentItem(item)
                return
        if first is not None:
            self.sets_tree.setCurrentItem(first)

    def _set_items(self) -> list[QTreeWidgetItem]:
        found = []
        for group in range(self.sets_tree.topLevelItemCount()):
            head = self.sets_tree.topLevelItem(group)
            for index in range(head.childCount()):
                child = head.child(index)
                if child.data(0, Qt.ItemDataRole.UserRole):
                    found.append(child)
        return found

    def current_ref(self) -> str:
        """The selected set, as a profile would name it. ``""`` for none."""
        item = self.sets_tree.currentItem()
        return item.data(0, Qt.ItemDataRole.UserRole) or "" if item else ""

    def current_table_name(self) -> str:
        """The selected set's name, if it is one of the user's own."""
        ref = self.current_ref()
        return ref[len(TABLE) :] if ref.startswith(TABLE) else ""

    def current_language(self) -> str:
        """The selected set's language, if it is a built-in one."""
        ref = self.current_ref()
        return ref[len(BUILTIN) :] if ref.startswith(BUILTIN) else ""

    def _on_picked(self, current, _previous=None) -> None:
        self._show_selected()
        self.selected.emit(self.current_ref())

    def _show_selected(self) -> None:
        language, table_name = self.current_language(), self.current_table_name()
        # A built-in set is a file; one of the user's own is a row in a store.
        # Nothing can act on both, so each row of buttons follows its own kind.
        for button in (self.open_file_btn, self.restore_btn):
            button.setEnabled(bool(language))
        for button in (
            self.export_xlsx_btn,
            self.export_json_btn,
            self.rename_table_btn,
            self.delete_table_btn,
        ):
            button.setEnabled(bool(table_name))

        self.rules_table.setRowCount(0)
        if language:
            self._fill_builtin(language)
        elif table_name:
            self._fill_table(table_name)
        else:
            self.header.setText(NOTHING_SELECTED)
            self.rules_note.setText("")

    def _fill_builtin(self, language: str) -> None:
        table = rule_files.table(language)
        lang = languages.get(language)
        self.header.setText(f"{languages.label_of(language)} — built in")
        if table is None:
            self.rules_note.setText(f"No rules ship for {language}.")
            return
        self.rules_table.setRowCount(len(table.rules))
        for row, rule in enumerate(table.rules):
            self.rules_table.setItem(row, 0, QTableWidgetItem(rule.rule_id))
            self.rules_table.setItem(row, 1, QTableWidgetItem(rule.details))
            self.rules_table.setItem(row, 2, QTableWidgetItem(applies_label(lang, rule)))

        problem = rule_files.problem_with(language)
        path = rule_files.path_for(language)
        if problem:
            self.rules_note.setText(
                f"{path} could not be read, so the rules this build shipped with "
                f"are being used instead: {problem}"
            )
            self.rules_note.setStyleSheet(WARN)
        else:
            self.rules_note.setText(f"{len(table.rules)} rule(s) — {path}")
            self.rules_note.setStyleSheet(MUTED)

    def _fill_table(self, name: str) -> None:
        table = self._store.find(name) if self._store else None
        self.header.setText(f"{name} — one of yours")
        self.rules_note.setStyleSheet(MUTED)
        if table is None:
            self.rules_note.setText("")
            return
        self.rules_table.setRowCount(len(table.rules))
        for row, rule in enumerate(table.rules):
            self.rules_table.setItem(row, 0, QTableWidgetItem(rule.rule_id))
            self.rules_table.setItem(row, 1, QTableWidgetItem(rule.details))
            # No version span: only the shipped rules carry one, and a blank
            # cell says that more honestly than "any" would.
            self.rules_table.setItem(row, 2, QTableWidgetItem(""))
        source = f" from {table.source}" if table.source else ""
        self.rules_note.setText(f"{len(table.rules)} rule(s){source}")

    def _refresh_shipped_note(self) -> None:
        covered = rule_files.languages_covered()
        broken = [one for one in covered if rule_files.problem_with(one)]
        self.shipped_rules_note.setText(
            f"{len(covered)} built-in set(s) in {rule_files.rules_dir()}."
            + (
                f"  {len(broken)} could not be read and the shipped rules are "
                f"being used instead: {', '.join(broken)}."
                if broken
                else ""
            )
        )
        self.shipped_rules_note.setStyleSheet(WARN if broken else MUTED)


def _builtin_rows() -> list[tuple[str, str, str]]:
    rows = []
    for language in rule_files.languages_covered():
        table = rule_files.table(language)
        rows.append(
            (
                f"{BUILTIN}{language}",
                languages.label_of(language),
                str(len(table.rules)) if table else "0",
            )
        )
    return rows


def _table_rows(store) -> list[tuple[str, str, str]]:
    if store is None:
        return []
    rows = []
    for name in store.names():
        table = store.find(name)
        rows.append((f"{TABLE}{name}", name, str(len(table.rules)) if table else "0"))
    return rows


def applies_label(language, rule) -> str:
    """The span of language versions a shipped rule is true for.

    Written the way someone would say it -- "C++20 and later" -- rather than as
    the two version ids, because the ids are an implementation detail of the
    file and the span is the thing that decides whether the rule is sent.
    """
    since = getattr(rule, "since", "")
    until = getattr(rule, "until", "")
    if not since and not until:
        return ""
    first = language.label_for(since) if language and since else ""
    last = language.label_for(until) if language and until else ""
    if first and last:
        return f"{first} to {last}"
    if first:
        return f"{first} and later"
    return f"up to {last}"
