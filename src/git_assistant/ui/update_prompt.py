"""Qt glue for updating: ask winget off-thread, then ask the user.

Follows the same worker pattern as `ui/workers.py` -- a QObject moved onto a
QThread by `run_worker` -- because a check starts a winget process that talks to
the network, and blocking the event loop would freeze the tray.

The user decides. An update is offered and installed only on an explicit yes;
nothing here installs anything on its own.
"""

from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal
from PyQt6.QtWidgets import QMessageBox

from git_assistant.updating import PACKAGE_ID, UpdateResult, check_for_update


class UpdateCheckWorker(QObject):
    """Runs one winget check off the GUI thread.

    Three outcomes, three signals, so the caller never has to inspect a
    sentinel: found something, found nothing, or could not tell.
    """

    found = pyqtSignal(object)  # UpdateResult
    none_available = pyqtSignal()
    error = pyqtSignal(str)
    finished = pyqtSignal(object)  # run_worker cleans up on this

    def run(self) -> None:
        try:
            result = check_for_update()
        except Exception as exc:  # the UI reports whatever went wrong
            self.error.emit(str(exc))
            self.finished.emit(None)
            return

        if result is None:
            self.none_available.emit()
        else:
            self.found.emit(result)
        self.finished.emit(result)


class UpgradeWorker(QObject):
    """Runs `winget upgrade` off the GUI thread.

    Its own worker rather than a `FunctionWorker`, because this one is not a
    quick call: winget downloads an installer and runs it, and the tray has to
    stay responsive throughout -- an application that appears hung is one
    people kill halfway through an install.
    """

    finished = pyqtSignal(object)  # the message winget's outcome deserves
    error = pyqtSignal(str)

    def __init__(self, result: UpdateResult) -> None:
        super().__init__()
        self._result = result

    def run(self) -> None:
        from git_assistant.updating import upgrade

        try:
            note = upgrade(self._result)
        except Exception as exc:
            self.error.emit(str(exc))
            self.finished.emit(None)
            return
        self.finished.emit(note)


def ask_to_install(result: UpdateResult) -> bool:
    """Show the consent dialog. Returns True if the user wants it installed.

    Names the command rather than describing it. "winget will install this" is
    a claim about someone else's software running on their machine, and the
    exact line is both shorter and checkable -- and it is the line they can run
    themselves if they would rather not let the application do it.
    """
    box = QMessageBox()
    box.setIcon(QMessageBox.Icon.Information)
    box.setWindowTitle("Update available")
    box.setText(f"Git Assistant {result.version} is published on winget.")

    verb = "upgrade" if result.installed else "install"
    box.setInformativeText(
        f"You have {result.current}.\n\n"
        f"This runs:\nwinget {verb} --id {PACKAGE_ID} --exact\n\n"
        "Git Assistant closes while the installer replaces it."
    )

    install = box.addButton("Install now", QMessageBox.ButtonRole.AcceptRole)
    box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(install)
    box.exec()
    return box.clickedButton() is install
