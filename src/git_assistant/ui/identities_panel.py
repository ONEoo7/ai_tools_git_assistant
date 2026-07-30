"""The Identities tab: the set of committer identities, and moving it between machines.

This tab owns the *list*. Which identity a repository uses is not shown here --
that belongs to the repository, and is set from the "Commit as" picker above
the tabs.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from git_assistant.identities import (
    Identity,
    IdentityStore,
    identities_path,
    is_valid,
)


class IdentitiesPanel(QWidget):
    """Add, remove, export and import committer identities."""

    #: Emitted whenever the stored set changes, so the picker above the tabs
    #: can re-offer it.
    identitiesChanged = pyqtSignal()  # noqa: N815 - Qt signal naming

    def __init__(self, store: IdentityStore, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self._loading = False

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                "Identities you can commit as. Pick one for the active repository\n"
                "with the \"Commit as\" selector above the tabs.\n"
                "A signing key travels with its identity: selecting one sets "
                "user.signingkey, and selecting an identity without a key clears it."
            )
        )

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            [
                "Name (user.name)",
                "Email (user.email)",
                "Signing key (user.signingkey, optional)",
            ]
        )
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        for col in range(3):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._on_edited)
        layout.addWidget(self.table, 1)

        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._on_add)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._on_remove)
        export_btn = QPushButton("Export...")
        export_btn.setToolTip("Write these identities to a JSON file.")
        export_btn.clicked.connect(self._on_export)
        import_btn = QPushButton("Import...")
        import_btn.setToolTip(
            "Merge identities from a JSON file. Existing entries are kept."
        )
        import_btn.clicked.connect(self._on_import)

        row = QHBoxLayout()
        row.addWidget(add_btn)
        row.addWidget(remove_btn)
        row.addStretch(1)
        row.addWidget(import_btn)
        row.addWidget(export_btn)
        layout.addLayout(row)

        self.status = QLabel("")
        self.status.setStyleSheet("color: #8ab;")
        self.status.setWordWrap(True)
        self.status.setToolTip(str(identities_path()))
        layout.addWidget(self.status)

        self.refresh()

    # ---- display -----------------------------------------------------------
    def refresh(self) -> None:
        self._loading = True  # repopulating must not look like an edit
        try:
            self.table.setRowCount(0)
            for ident in self.store.identities:
                self._append_row(ident.name, ident.email, ident.signingkey)
        finally:
            self._loading = False
        self._report()

    def _append_row(self, name: str, email: str, signingkey: str = "") -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(name))
        self.table.setItem(r, 1, QTableWidgetItem(email))
        self.table.setItem(r, 2, QTableWidgetItem(signingkey))

    def _report(self, message: str = "") -> None:
        count = len(self.store.identities)
        noun = "identity" if count == 1 else "identities"
        where = f"{count} {noun} in {identities_path().name}"
        self.status.setText(f"{message}  --  {where}" if message else where)

    # ---- editing -----------------------------------------------------------
    def _cell(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        return item.text().strip() if item else ""

    def _rows(self) -> list[Identity]:
        return [
            Identity(
                name=self._cell(r, 0),
                email=self._cell(r, 1),
                signingkey=self._cell(r, 2),
            )
            for r in range(self.table.rowCount())
        ]

    def _commit_rows(self, message: str = "") -> None:
        """Persist what is in the table.

        A half-typed row is not an error to complain about -- the user is still
        editing it -- so incomplete rows are simply not stored yet. They stay on
        screen, and are picked up as soon as they become valid.
        """
        rows = self._rows()
        self.store.replace(rows)
        pending = sum(1 for r in rows if (r.name or r.email) and not is_valid(r))
        if pending and not message:
            message = f"{pending} incomplete row(s) not saved; an email is required"
        self._report(message)
        self.identitiesChanged.emit()

    def _on_edited(self, _item) -> None:
        if not self._loading:
            self._commit_rows()

    def _on_add(self) -> None:
        self._loading = True
        try:
            self._append_row("", "")
        finally:
            self._loading = False
        self.table.setCurrentCell(self.table.rowCount() - 1, 0)
        self.table.editItem(self.table.item(self.table.rowCount() - 1, 0))

    def _on_remove(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            return
        self._loading = True
        try:
            for r in rows:
                self.table.removeRow(r)
        finally:
            self._loading = False
        self._commit_rows(f"Removed {len(rows)}")

    # ---- transfer ----------------------------------------------------------
    def _on_export(self) -> None:
        if not self.store.identities:
            QMessageBox.information(
                self, "Nothing to export", "There are no identities to write."
            )
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Export identities",
            str(Path.home() / "committer_identities.json"),
            "JSON files (*.json)",
        )
        if not path:
            return
        try:
            self.store.export_to(path)
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        # Worth saying out loud: the file is plain text, and the addresses in it
        # are the user's own.
        self._report(f"Exported {len(self.store.identities)} to {Path(path).name}")
        QMessageBox.information(
            self,
            "Exported",
            f"Wrote {len(self.store.identities)} identities to:\n{path}\n\n"
            "The file is plain text and contains your name and email address.",
        )

    def _on_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import identities", str(Path.home()), "JSON files (*.json)"
        )
        if not path:
            return
        added, skipped = self.store.import_from(path)
        self.refresh()
        self.identitiesChanged.emit()
        if added == 0 and skipped == 0:
            self._report("Nothing imported: no usable identities in that file")
            return
        note = f"Imported {added}"
        if skipped:
            note += f", skipped {skipped} already present"
        self._report(note)
