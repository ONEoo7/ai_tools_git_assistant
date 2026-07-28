"""System-tray icon, menu, and top-level app wiring."""

from __future__ import annotations

import os

from PyQt6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from git_assistant import git_ops
from git_assistant.config import RepoEntry, Settings
from git_assistant.ui.icon import app_icon
from git_assistant.ui.preview_dialog import PreviewDialog
from git_assistant.ui.repo_watcher import RepoWatcher
from git_assistant.ui.settings_dialog import SettingsDialog
from git_assistant.ui.update_prompt import UpdateCheckWorker, ask_to_install
from git_assistant.ui.workers import FunctionWorker, run_worker
from git_assistant.updating import UpdateConfig

def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


class TrayApp:
    def __init__(self, app: QApplication) -> None:
        self.app = app
        self.settings = Settings.load()
        self._main_window = None  # set while the main window is open

        self.icon = app_icon()
        self.tray = QSystemTrayIcon(self.icon)
        self.tray.setToolTip("Git Assistant")
        self.tray.activated.connect(self._on_activated)

        # Self-update. Read before the menu is built, because the menu asks
        # whether updating is configured. The address comes from `update.json`
        # if the user wrote one, otherwise from the build. A developer checkout
        # has neither and is refused anyway, so it never reaches the network.
        self._update_config = UpdateConfig.load()
        self._update_worker = None
        self._update_thread = None

        self.menu = QMenu()
        self.tray.setContextMenu(self.menu)
        self._rebuild_menu()
        self.tray.show()

        # Auto-watch opted-in folders for newly cloned repos.
        self._watch_workers: set = set()
        self._watch_threads: set = set()
        self.watcher = RepoWatcher(parent=self.tray)
        self.watcher.folderChanged.connect(self._on_watched_change)
        self._refresh_watcher()

        # Fill in any missing repo owners (e.g. from configs saved before owners
        # were resolved, or repos unblocked since) so the tray shows owner\name.
        self._backfill_owners()

        if self._update_config.enabled:
            self._check_for_update(announce_nothing=False)

    # ---- menu --------------------------------------------------------------
    def _rebuild_menu(self) -> None:
        self.menu.clear()

        # The active repository is chosen in the Generate tab's selector, so the
        # tray menu stays a short list of actions.
        gen = self.menu.addAction("Generate commit message")
        gen.triggered.connect(self._on_generate)
        metrics_act = self.menu.addAction("Metrics...")
        metrics_act.triggered.connect(self._on_metrics)
        settings_act = self.menu.addAction("Settings...")
        settings_act.triggered.connect(self._on_settings)
        if self._update_config.enabled:
            update_act = self.menu.addAction("Check for updates...")
            update_act.triggered.connect(
                lambda: self._check_for_update(announce_nothing=True)
            )
        self.menu.addSeparator()
        quit_act = self.menu.addAction("Quit")
        quit_act.triggered.connect(self.app.quit)

    # ---- actions -----------------------------------------------------------
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Left-click / double-click opens the main window. Right-click still
        # gets the context menu (Qt shows it automatically).
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_settings()

    def _on_generate(self) -> None:
        if not self.settings.repos:
            self._notify(
                "No repository configured",
                "Add a git repository in Settings first.",
            )
            self._on_settings()
            return
        if not self.settings.selected_model:
            self._notify(
                "No model selected",
                "Open Settings, test the connection, and pick a model.",
            )
            self._on_settings()
            return
        dialog = PreviewDialog(self.settings)
        dialog.exec()

    def _on_metrics(self) -> None:
        from git_assistant.ui.metrics_dialog import MetricsDialog

        MetricsDialog(self.settings).exec()

    def show_main_window(self) -> None:
        """Open (or raise) the main window. Entry point for a second launch."""
        self._on_settings()

    def _on_settings(self) -> None:
        # exec() runs a nested event loop, so further tray clicks still arrive.
        # Raise the existing window instead of opening a second one.
        existing = getattr(self, "_main_window", None)
        if existing is not None:
            existing.raise_()
            existing.activateWindow()
            return

        # Pause watching while the dialog is open so it can't mutate settings.repos
        # underneath the dialog's own edits.
        self.watcher.set_roots([])
        dialog = SettingsDialog(self.settings)
        self._main_window = dialog
        try:
            dialog.exec()
        finally:
            self._main_window = None
        # The window autosaves, so always reload rather than relying on a
        # Save button's result code.
        self.settings = Settings.load()
        self._rebuild_menu()
        self._refresh_watcher()
        # If safe.directory was just fixed here, previously-blocked owners can
        # now be resolved.
        self._backfill_owners()

    def _notify(self, title: str, message: str) -> None:
        if self.tray.supportsMessages():
            self.tray.showMessage(title, message)
        else:
            QMessageBox.information(None, title, message)

    # ---- self-update -------------------------------------------------------
    def _check_for_update(self, *, announce_nothing: bool) -> None:
        """Run one update check off-thread.

        ``announce_nothing`` separates the automatic check at startup, which
        should stay quiet when there is nothing to say, from the menu item,
        where silence would look like a broken button.
        """
        if self._update_thread is not None:
            return  # one at a time

        worker = UpdateCheckWorker(self._update_config)
        worker.found.connect(self._on_update_found)
        worker.error.connect(
            lambda message: self._notify("Update check failed", message)
        )
        if announce_nothing:
            worker.none_available.connect(
                lambda: self._notify("Up to date", "You have the latest version.")
            )

        thread = run_worker(worker)

        # Both references must outlive the call, for two different reasons, and
        # dropping either looks like "the check silently never runs":
        #  - the worker is a local, and once it is collected the queued
        #    thread.started -> worker.run never arrives;
        #  - the QThread must not be destroyed while it is still running.
        # Released on thread.finished, not worker.finished: the latter fires
        # first, since it is what asks the thread to quit.
        self._update_worker = worker
        self._update_thread = thread
        thread.finished.connect(self._forget_update_worker)

    def _forget_update_worker(self) -> None:
        self._update_worker = None
        self._update_thread = None

    def _on_update_found(self, result) -> None:
        from git_assistant.updating.client import current_version

        self._notify(
            "Update available",
            f"Git Assistant {result.version} is ready to install.",
        )
        if not ask_to_install(result, current_version()):
            return

        # Finding and verifying an update works; installing it does not yet.
        # The installer has to close the running application, swap the A/B
        # slot and relaunch, which needs the launcher shim from the
        # distribution platform. Saying so beats a button that looks like it
        # worked.
        self._notify(
            "Not yet supported",
            "This build can find and verify updates but cannot install them yet.",
        )

    # ---- owner backfill ----------------------------------------------------
    def _backfill_owners(self) -> None:
        """Resolve owners for repos that lack one, off-thread, then persist."""
        missing = [r.path for r in self.settings.repos if not r.owner]
        if not missing:
            return
        worker = FunctionWorker(
            lambda paths=tuple(missing): {
                p: (git_ops.repo_owner(p) or "") for p in paths
            }
        )
        worker.finished.connect(
            lambda owners, w=worker: self._apply_owners(owners, w)
        )
        worker.error.connect(lambda _m, w=worker: self._watch_workers.discard(w))
        self._watch_workers.add(worker)
        thread = run_worker(worker)
        self._watch_threads.add(thread)
        thread.finished.connect(lambda t=thread: self._watch_threads.discard(t))

    def _apply_owners(self, owners: dict, worker) -> None:
        self._watch_workers.discard(worker)
        changed = False
        for repo in self.settings.repos:
            resolved = owners.get(repo.path)
            if resolved and not repo.owner:
                repo.owner = resolved
                changed = True
        if changed:
            self.settings.save()
            self._rebuild_menu()

    # ---- auto-watch --------------------------------------------------------
    def _refresh_watcher(self) -> None:
        self.watcher.set_roots(list(self.settings.watched_roots))

    def _on_watched_change(self, folder: str) -> None:
        """A watched folder changed: rescan it off-thread and auto-add new repos."""
        worker = FunctionWorker(
            lambda f=folder: [
                (p, *git_ops.resolve_repo_meta(p)) for p in git_ops.find_git_repos(f)
            ]
        )
        worker.finished.connect(
            lambda results, f=folder, w=worker: self._merge_watched(f, results, w)
        )
        worker.error.connect(lambda _m, w=worker: self._watch_workers.discard(w))
        self._watch_workers.add(worker)
        thread = run_worker(worker)
        self._watch_threads.add(thread)
        thread.finished.connect(lambda t=thread: self._watch_threads.discard(t))

    def _merge_watched(self, folder: str, results, worker) -> None:
        self._watch_workers.discard(worker)
        existing = {_norm(r.path) for r in self.settings.repos}
        added = 0
        for path, owner, _blocked in results:
            if _norm(path) not in existing:
                self.settings.repos.append(RepoEntry(path=path, owner=owner))
                existing.add(_norm(path))
                added += 1
        if added:
            self.settings.save()
            self._rebuild_menu()
            self._notify(
                "Git Assistant",
                f"Auto-added {added} new repo(s) from {folder}.",
            )
