"""System-tray icon, menu, and top-level app wiring."""

from __future__ import annotations

import os

from PyQt6.QtCore import QTimer
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
from git_assistant.features import UPDATES_SUPPORTED
from git_assistant.ui.settings_dialog import SettingsDialog
from git_assistant.ui.workers import FunctionWorker, run_worker

# Absent from the no-update build, along with everything they reach. Imported
# behind the flag rather than defensively at each call site so that a build
# without the updater cannot import it by accident. See git_assistant.features.
if UPDATES_SUPPORTED:
    from git_assistant.ui.update_prompt import UpdateCheckWorker, ask_to_install
    from git_assistant.updating import (
        UpdateConfig,
        clear_staged_updates,
        install_update,
    )


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
        self._update_config = UpdateConfig.load() if UPDATES_SUPPORTED else None
        self._update_worker = None
        self._update_thread = None
        # Versions already put in front of the user this session. A repeating
        # check must not re-open a modal every interval for a release that was
        # answered with "Later" -- that turns an update into nagware, and is
        # the reason most applications with a background check are resented.
        self._update_offered: set[str] = set()
        # Parented to the tray icon, not to `self`: TrayApp is a plain object,
        # not a QObject, so it cannot own a QTimer. Parenting it to something
        # with the right lifetime is also what stops the timer being collected
        # while it is still armed.
        self._update_timer = QTimer(self.tray)
        self._update_timer.timeout.connect(
            lambda: self._check_for_update(announce_nothing=False)
        )

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

        # Remove the installer that produced this build, if there is one. This
        # is the only moment it can go: the copy that downloaded it launched it
        # and then quit, so it was never both alive and finished. This one is.
        if UPDATES_SUPPORTED:
            clear_staged_updates()

        if self._updates_on:
            self._check_for_update(announce_nothing=False)
            # Then keep checking, so an application left running for days does
            # not need the window raised to notice a release. Qt takes
            # milliseconds; the config is in minutes because a unit anyone can
            # get wrong by three orders of magnitude should not be the one in
            # the file people edit.
            self._update_timer.start(self._update_config.check_minutes * 60_000)

    @property
    def _updates_on(self) -> bool:
        """Is this build able to update itself, and configured to?

        Two separate questions collapsed into the one every caller asks. The
        no-update build answers False at the first hurdle, without touching
        `UpdateConfig`, which it does not have.
        """
        return self._update_config is not None and self._update_config.enabled

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
        if self._updates_on:
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
            # The window fills its update readout when it is constructed, and
            # this path does not construct one. Without this it keeps showing
            # what was true whenever it was first opened.
            existing.refresh_update_status()
            return

        # Pause watching while the dialog is open so it can't mutate settings.repos
        # underneath the dialog's own edits.
        self.watcher.set_roots([])
        dialog = SettingsDialog(self.settings)
        # Clicking "vX.Y.Z available" in the window lands in the same place as
        # the tray notification.
        dialog.installRequested.connect(self.offer_install)
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
        should stay quiet when there is nothing to say -- including when the
        check itself fails -- from the menu item, where silence would look like
        a broken button.
        """
        if not self._updates_on or self._update_thread is not None:
            return  # no updater in this build, or one already running

        worker = UpdateCheckWorker(self._update_config)
        # `announce_nothing` marks the menu item, which is someone asking. That
        # ask is always answered, even for a version already declined -- a
        # button that does nothing because of a decision made an hour ago is
        # indistinguishable from a broken one.
        worker.found.connect(
            lambda result: self._on_update_found(result, asked=announce_nothing)
        )
        worker.error.connect(self._tell_window_check_failed)
        if announce_nothing:
            worker.none_available.connect(
                lambda: self._notify("Up to date", "You have the latest version.")
            )
            # A failure is only worth interrupting for when someone asked. An
            # unreachable update server on a laptop that starts offline is the
            # normal case, not news, and the automatic check runs on a timer --
            # one toast at startup would become one every interval. The settings
            # window still shows the failure via `_tell_window_check_failed`.
            worker.error.connect(
                lambda message: self._notify("Update check failed", message)
            )

        # Keep the open window in step. Its readout is filled once, when it is
        # constructed, and a window that was already open when this check ran
        # would otherwise keep saying "up to date" while the tray was offering
        # an update -- two answers to the same question, on screen at once.
        worker.found.connect(self._tell_window_about_update)
        worker.none_available.connect(lambda: self._tell_window_about_update(None))

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

    def _tell_window_about_update(self, result: object | None) -> None:
        """Push a check result into the settings window, if one is open.

        This is what `SettingsDialog.set_online_version` was always meant for;
        its docstring called itself a hook for the updater and nothing ever
        called it.
        """
        window = getattr(self, "_main_window", None)
        if window is None:
            return
        window.set_online_version(getattr(result, "version", None))

    def _tell_window_check_failed(self, message: str) -> None:
        window = getattr(self, "_main_window", None)
        if window is not None:
            window.set_update_error(message)

    def _on_update_found(self, result, *, asked: bool = True) -> None:
        # An automatic check that has already offered this version says nothing
        # more. The window's readout is still kept current by
        # `_tell_window_about_update`, so the information is not lost -- it just
        # stops interrupting. A newer release resets this by not being in the
        # set.
        if not asked and result.version in self._update_offered:
            return
        self._update_offered.add(result.version)

        self._notify(
            "Update available",
            f"Git Assistant {result.version} is ready to install.",
        )
        self.offer_install(result)

    def offer_install(self, result) -> None:
        """Ask for consent, then install. One path, whatever led here.

        Reached from the tray notification and from clicking the readout in the
        settings window. Deliberately the same method rather than two copies:
        two copies is how a consent dialog ends up meaning slightly different
        things depending on which route you took to it.
        """
        if not self._updates_on:
            return  # nothing here can run in a build without the updater

        from git_assistant.updating.client import current_version

        if not ask_to_install(result, current_version()):
            return

        # Tens of megabytes over the network, so off the GUI thread. The tray
        # would otherwise stop responding for the whole download, which reads
        # as the application hanging on a button press.
        self._notify("Downloading update", f"Fetching Git Assistant {result.version}...")
        worker = FunctionWorker(lambda: install_update(self._update_config, result))
        worker.finished.connect(self._on_installer_started)
        worker.error.connect(lambda message: self._notify("Update failed", message))
        self._install_worker = worker
        self._install_thread = run_worker(worker)

    def _on_installer_started(self, _path: object) -> None:
        """The installer is running. Get out of its way.

        It stops this application itself before replacing files, so quitting is
        not strictly required -- but being killed means settings edited in this
        session are never written. Quitting on a short timer leaves the
        notification on screen long enough to read.
        """
        self._notify(
            "Installing update",
            "Git Assistant will close and reopen once the update is applied.",
        )
        QTimer.singleShot(2000, self.app.quit)

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
