"""Qt glue for self-update: check off-thread, notify, ask, then stop.

Follows the same worker pattern as `ui/workers.py` — a QObject moved onto a
QThread by `run_worker` — because an update check makes several network round
trips and blocking the event loop would freeze the tray menu.

The user decides. An update is offered as a notification and installed only on
an explicit yes; nothing here installs anything on its own.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from git_assistant.updating import UpdateConfig, UpdateResult, check_for_update


class UpdateCheckWorker(QObject):
    """Runs one update check off the GUI thread.

    Three outcomes, three signals, so the caller never has to inspect a
    sentinel: found something, found nothing, or could not tell.
    """

    found = pyqtSignal(object)  # UpdateResult
    none_available = pyqtSignal()
    error = pyqtSignal(str)
    finished = pyqtSignal(object)  # run_worker cleans up on this

    def __init__(self, config: UpdateConfig) -> None:
        super().__init__()
        self._config = config

    def run(self) -> None:
        try:
            result = check_for_update(self._config)
        except Exception as exc:  # the UI reports whatever went wrong
            # Includes verification failures. Those are *not* "no updates
            # available": something served metadata that did not verify, and
            # silently reporting no-update would hide an attack in progress.
            self.error.emit(str(exc))
            self.finished.emit(None)
            return

        if result is None:
            self.none_available.emit()
        else:
            self.found.emit(result)
        self.finished.emit(result)


def ask_to_install(result: UpdateResult, current: str) -> bool:
    """Show the consent dialog. Returns True if the user wants it installed.

    A mandatory release is still offered rather than forced. Marking it
    mandatory changes what the user is told, not whether they are asked —
    installing without consent would make the notification decorative.
    """
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("Update available")
    box.setText(f"Git Assistant {result.version} is available.")

    size_mb = result.length / (1024 * 1024)
    detail = f"You have {current}.\nDownload size: {size_mb:.1f} MB."
    if result.mandatory:
        detail += "\n\nThis release is marked as a security update."
    box.setInformativeText(detail)

    install = box.addButton("Install now", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(install)
    box.exec()
    return box.clickedButton() is install
