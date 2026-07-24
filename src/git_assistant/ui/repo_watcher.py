"""Debounced filesystem watcher for scan-root folders.

Uses ``QFileSystemWatcher`` (native ``ReadDirectoryChangesW`` / inotify / FSEvents)
to watch each configured root **non-recursively**. When a folder's direct
contents change - e.g. a new repo is cloned into it - a short debounce timer
collapses the burst of events into a single ``folderChanged`` emission.

Cost is minimal: one OS handle per watched folder, no polling, no extra threads.
The actual (heavier) rescan is left to the listener, which runs it off-thread.
"""

from __future__ import annotations

import os

from PyQt6.QtCore import QFileSystemWatcher, QObject, QTimer, pyqtSignal


class RepoWatcher(QObject):
    folderChanged = pyqtSignal(str)  # a watched root's contents changed (debounced)

    def __init__(self, debounce_ms: int = 3000, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir_changed)
        self._pending: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(debounce_ms)
        self._timer.timeout.connect(self._flush)

    def set_roots(self, paths: list[str]) -> None:
        """Replace the watched set with the existing directories in ``paths``."""
        current = self._watcher.directories()
        if current:
            self._watcher.removePaths(current)
        self._pending.clear()
        existing = [p for p in paths if os.path.isdir(p)]
        if existing:
            self._watcher.addPaths(existing)

    def _on_dir_changed(self, path: str) -> None:
        self._pending.add(path)
        self._timer.start()  # (re)start debounce; coalesces event bursts
        # A watch can be dropped if the dir was briefly replaced; re-add to be safe.
        if path not in self._watcher.directories() and os.path.isdir(path):
            self._watcher.addPath(path)

    def _flush(self) -> None:
        paths = list(self._pending)
        self._pending.clear()
        for p in paths:
            self.folderChanged.emit(p)
