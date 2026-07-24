"""Metrics window: count lines of code across selected repos or a directory."""

from __future__ import annotations

import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from git_assistant import git_ops, metrics
from git_assistant.config import Settings
from git_assistant.metrics import RepoMetrics
from git_assistant.ui.workers import FunctionWorker, run_worker

_NUM_COLS = (1, 2, 3, 4)  # right-aligned numeric columns


class MetricsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._thread = None
        self._worker = None
        self.setWindowTitle("Git Assistant - Metrics")
        self.setMinimumSize(720, 560)

        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel("Select repositories, then count lines of code (tracked files only):")
        )

        self.repo_list = QListWidget()
        layout.addWidget(self.repo_list, 1)
        for entry in settings.repos:
            self._add_repo_item(entry.path, entry.display(), checked=True)

        sel_row = QHBoxLayout()
        all_btn = QPushButton("Select all")
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn = QPushButton("Select none")
        none_btn.clicked.connect(lambda: self._set_all(False))
        add_dir_btn = QPushButton("Add directory...")
        add_dir_btn.clicked.connect(self._on_add_directory)
        sel_row.addWidget(all_btn)
        sel_row.addWidget(none_btn)
        sel_row.addWidget(add_dir_btn)
        sel_row.addStretch(1)
        layout.addLayout(sel_row)

        self.run_btn = QPushButton("Count lines of code")
        self.run_btn.clicked.connect(self._on_run)
        layout.addWidget(self.run_btn)

        self.status = QLabel("")
        self.status.setStyleSheet("color: #8ab;")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.results = QTreeWidget()
        self.results.setHeaderLabels(
            ["Repository / file type", "Files", "Code", "Blank", "Total"]
        )
        self.results.setColumnWidth(0, 320)
        for c in _NUM_COLS:
            self.results.headerItem().setTextAlignment(
                c, Qt.AlignmentFlag.AlignRight
            )
        layout.addWidget(self.results, 2)

    # ---- repo selection ----------------------------------------------------
    def _add_repo_item(self, path: str, label: str, checked: bool) -> None:
        item = QListWidgetItem(f"{label}  -  {path}")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(
            Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        )
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)
        self.repo_list.addItem(item)

    def _existing_paths(self) -> set[str]:
        return {
            os.path.normcase(os.path.normpath(self.repo_list.item(i).data(Qt.ItemDataRole.UserRole)))
            for i in range(self.repo_list.count())
        }

    def _set_all(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self.repo_list.count()):
            self.repo_list.item(i).setCheckState(state)

    def _checked_paths(self) -> list[str]:
        return [
            self.repo_list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self.repo_list.count())
            if self.repo_list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def _on_add_directory(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select a folder to scan for git repositories"
        )
        if not folder:
            return
        self.status.setText(f"Scanning {folder} ...")
        worker = FunctionWorker(lambda f=folder: git_ops.find_git_repos(f))
        worker.finished.connect(self._on_dir_scanned)
        worker.error.connect(lambda m: self.status.setText(f"Scan failed: {m}"))
        self._worker = worker
        self._thread = run_worker(worker)

    def _on_dir_scanned(self, paths: list[str]) -> None:
        existing = self._existing_paths()
        added = 0
        for p in paths:
            if os.path.normcase(os.path.normpath(p)) not in existing:
                self._add_repo_item(p, os.path.basename(p), checked=True)
                added += 1
        self.status.setText(f"Found {len(paths)} repo(s); added {added} new.")

    # ---- run ---------------------------------------------------------------
    def _on_run(self) -> None:
        paths = self._checked_paths()
        if not paths:
            self.status.setText("Select at least one repository.")
            return
        self.run_btn.setEnabled(False)
        self.status.setText(f"Counting lines in {len(paths)} repo(s)...")
        worker = FunctionWorker(
            lambda ps=tuple(paths): [metrics.analyze_repo(p) for p in ps]
        )
        worker.finished.connect(self._on_done)
        worker.error.connect(self._on_error)
        self._worker = worker
        self._thread = run_worker(worker)

    def _on_error(self, message: str) -> None:
        self.run_btn.setEnabled(True)
        self.status.setText(f"Failed: {message}")

    def _on_done(self, results: list[RepoMetrics]) -> None:
        self.run_btn.setEnabled(True)
        self.results.clear()

        ok = [m for m in results if m.ok]
        blocked = [m for m in results if not m.ok]

        # Grand total across all successful repos.
        agg = metrics.aggregate(ok)
        total_item = QTreeWidgetItem(["TOTAL (all selected)"])
        self._fill_numbers(total_item, agg.values())
        self._add_ext_rows(total_item, agg)
        self.results.addTopLevelItem(total_item)
        total_item.setExpanded(True)

        for m in results:
            if not m.ok:
                item = QTreeWidgetItem(
                    [f"{os.path.basename(m.path)}  -  blocked: {m.error[:60]}"]
                )
                self.results.addTopLevelItem(item)
                continue
            item = QTreeWidgetItem([os.path.basename(m.path) or m.path])
            self._set_row(item, m.totals)
            self._add_ext_rows(item, m.by_ext)
            item.setToolTip(0, m.path)
            self.results.addTopLevelItem(item)

        msg = f"Counted {len(ok)} repo(s)"
        if blocked:
            msg += (
                f"; {len(blocked)} blocked by git ownership check - run:  "
                "git config --global --add safe.directory '*'"
            )
        self.status.setText(msg + ".")

    # ---- rendering helpers -------------------------------------------------
    def _add_ext_rows(self, parent: QTreeWidgetItem, by_ext: dict) -> None:
        for ext, stat in sorted(by_ext.items(), key=lambda kv: kv[1].code, reverse=True):
            child = QTreeWidgetItem([ext])
            self._set_row(child, stat)
            parent.addChild(child)

    def _fill_numbers(self, item: QTreeWidgetItem, stats) -> None:
        files = lines = blank = 0
        for s in stats:
            files += s.files
            lines += s.lines
            blank += s.blank
        self._write(item, files, lines - blank, blank, lines)

    def _set_row(self, item: QTreeWidgetItem, stat) -> None:
        self._write(item, stat.files, stat.code, stat.blank, stat.lines)

    def _write(self, item, files, code, blank, total) -> None:
        for col, val in zip(_NUM_COLS, (files, code, blank, total)):
            item.setText(col, f"{val:,}")
            item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight)
