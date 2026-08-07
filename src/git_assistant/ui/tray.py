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
from git_assistant.ui.repo_watcher import RepoWatcher
from git_assistant.ui.settings_dialog import SettingsDialog, show_about
from git_assistant.ui.update_prompt import UpdateCheckWorker, UpgradeWorker, ask_to_install
from git_assistant.ui.workers import FunctionWorker, run_worker
from git_assistant.updating import CHECK_MINUTES, unavailable_reason


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

        # Updating, which is winget's job now -- see git_assistant.updating.
        self._update_worker = None
        self._update_thread = None
        self._upgrade_worker = None
        self._upgrade_thread = None
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
        self._update_timer.timeout.connect(self._check_for_update)

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

        if self._updates_on:
            self._check_for_update()
            # Then keep checking, so an application left running for days does
            # not need the window raised to notice a release. Qt takes
            # milliseconds; CHECK_MINUTES is in minutes because that is the unit
            # the interval is reasoned about in.
            self._update_timer.start(CHECK_MINUTES * 60_000)

    @property
    def _updates_on(self) -> bool:
        """Can this installation be updated by winget at all?

        Asked once, at startup. Whether winget exists and whether this is an
        installed build are both settled for the life of the process, so
        re-deciding it every five minutes would only mean running the same
        checks to reach the same answer.
        """
        return unavailable_reason() is None

    # ---- menu --------------------------------------------------------------
    def _rebuild_menu(self) -> None:
        """Open the window, say what this is, or stop. Nothing else.

        Everything the menu used to offer -- generating a message, metrics,
        checking for an update -- is in the window, and a tray menu that
        duplicates the window is two places to keep in step and two places to
        look for the same thing.
        """
        self.menu.clear()

        show_act = self.menu.addAction("Git Assistant")
        show_act.triggered.connect(self._on_settings)
        # Bold, and what Enter picks: the same thing clicking the icon does.
        self.menu.setDefaultAction(show_act)
        about_act = self.menu.addAction("About")
        about_act.triggered.connect(self._on_about)
        self.menu.addSeparator()
        exit_act = self.menu.addAction("Exit")
        exit_act.triggered.connect(self.app.quit)

    # ---- actions -----------------------------------------------------------
    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # Left-click / double-click opens the main window. Right-click still
        # gets the context menu (Qt shows it automatically).
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._on_settings()

    def _on_about(self) -> None:
        # Parentless: the menu is reachable with no window open, and the About
        # box is the one thing here that does not need one.
        show_about(self._main_window)

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

    # ---- updating ------------------------------------------------------------
    def _check_for_update(self) -> None:
        """Ask winget, off-thread. Runs at startup and every CHECK_MINUTES.

        Quiet unless there is something to install. Nothing in the tray menu
        asks for a check, so every call here is the timer or startup, and
        silence when there is nothing to say -- including when winget itself
        fails -- is what stops a laptop that starts offline from producing a
        toast every five minutes. Asking is done by opening the window, which
        runs its own check and shows the result, and any failure, in its
        version readout.

        The guard on `_update_thread` matters more at five-minute intervals
        than it did at four hours: a winget that is slow because its source
        index is being rebuilt must not accumulate one thread per tick.
        """
        if not self._updates_on or self._update_thread is not None:
            return  # winget cannot update this install, or a check is running

        worker = UpdateCheckWorker()
        worker.found.connect(self._on_update_found)
        worker.error.connect(self._tell_window_check_failed)

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

    def _on_update_found(self, result) -> None:
        # A check that has already offered this version says nothing more. The
        # window's readout is still kept current by `_tell_window_about_update`,
        # so the information is not lost -- it just stops interrupting. A newer
        # release resets this by not being in the set.
        if result.version in self._update_offered:
            return
        self._update_offered.add(result.version)

        self._notify(
            "Update available",
            f"Git Assistant {result.version} is ready to install.",
        )
        self.offer_install(result)

    def offer_install(self, result) -> None:
        """Ask for consent, then let winget do it. One path, whatever led here.

        Reached from the tray notification and from clicking the readout in the
        settings window. Deliberately the same method rather than two copies:
        two copies is how a consent dialog ends up meaning slightly different
        things depending on which route you took to it.
        """
        if not self._updates_on or self._upgrade_thread is not None:
            return

        if not ask_to_install(result):
            return

        # winget downloads the installer and runs it, so this is a download and
        # an install, off the GUI thread. The tray would otherwise stop
        # responding throughout, which reads as the application hanging on a
        # button press.
        self._notify(
            "Installing update",
            f"winget is installing Git Assistant {result.version}. This "
            "application will close while it does.",
        )
        worker = UpgradeWorker(result)
        worker.finished.connect(self._on_upgrade_finished)
        worker.error.connect(self._on_upgrade_failed)
        self._upgrade_worker = worker
        self._upgrade_thread = run_worker(worker)
        self._upgrade_thread.finished.connect(self._forget_upgrade_worker)

    def _forget_upgrade_worker(self) -> None:
        self._upgrade_worker = None
        self._upgrade_thread = None

    def _on_upgrade_failed(self, message: str) -> None:
        # Always announced, unlike a failed check: this one the user asked for
        # by pressing Install now, and silence after that reads as a dead button.
        self._notify("Update failed", message)

    def _on_upgrade_finished(self, note: object) -> None:
        """winget is done. Get out of the way.

        The installer stops this application itself while it replaces the
        files, so reaching here at all usually means it did not need to -- but
        quitting is what gets settings written either way, and the new build
        cannot start while the old one holds its own directory open.

        The installer does not start the new version -- see
        installer/git-assistant.nsi -- so this is the last thing the user is
        told before the tray icon disappears. It has to say that reopening is
        theirs to do, or an update looks like a crash.
        """
        if note is None:
            return  # the error path already said what happened; stay running
        self._notify(
            "Update installed",
            "Git Assistant will close now. Start it again from the Start menu.",
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
