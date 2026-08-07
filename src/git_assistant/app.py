"""Application entry point: bootstrap Qt, install the tray, run the event loop."""

from __future__ import annotations

import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from git_assistant import faults, git_ops
from git_assistant.ui.icon import app_icon

APP_ID = "ONEoo7.GitAssistant"
# Named pipe used to detect an instance that is already running.
IPC_NAME = "git-assistant-single-instance"
# Passed by the "start with Windows" entry: come up in the tray only, without
# popping the window in front of someone who just signed in.
STARTUP_FLAG = "--startup"


def _set_windows_app_id() -> None:
    """Give the process its own taskbar identity on Windows.

    Without an explicit AppUserModelID, Windows groups the window under the
    host interpreter (python.exe) and shows *its* icon in the taskbar instead
    of the one Qt sets. Must run before any window is created. No-op elsewhere.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass  # cosmetic only - never block startup over the taskbar icon


def _signal_running_instance() -> bool:
    """Ask an already-running instance to show itself. True if one answered.

    Launching the shortcut again should raise the existing window, not add a
    second tray icon.
    """
    sock = QLocalSocket()
    sock.connectToServer(IPC_NAME)
    if not sock.waitForConnected(500):
        return False
    sock.write(b"show")
    sock.flush()
    sock.waitForBytesWritten(500)
    sock.disconnectFromServer()
    return True


def _listen_for_other_instances(on_show) -> QLocalServer:
    """Serve the pipe later launches connect to."""
    # A crash can leave the pipe behind; without this the new instance cannot
    # listen and every subsequent launch would spawn another tray icon.
    QLocalServer.removeServer(IPC_NAME)
    server = QLocalServer()
    server.listen(IPC_NAME)

    def _handle() -> None:
        conn = server.nextPendingConnection()
        if conn is None:
            return
        conn.disconnected.connect(conn.deleteLater)
        # The connection itself is the message: waiting on readyRead races with
        # the client disconnecting, which silently dropped the request.
        on_show()

    server.newConnection.connect(_handle)
    return server


def main() -> int:
    # First, and before QApplication: the failure this guards against happens
    # inside QApplication's constructor, and Qt aborts the process rather than
    # raising. See git_assistant.faults.
    faults.install()
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("git-assistant")
    # Use the tray icon for window title bars / taskbar too, so they match.
    app.setWindowIcon(app_icon())
    # Tray apps must keep running after their dialogs close.
    app.setQuitOnLastWindowClosed(False)

    # Everything this application does is a git command. Without git it is not
    # degraded, it is inert -- every repository would read as empty and every
    # tab would show nothing, with no clue why. Said once, plainly, rather than
    # left to be inferred from a window full of blanks.
    if not git_ops.git_available():
        faults.write("Git was not found; refusing to start.")
        QMessageBox.critical(
            None,
            "Git is not installed",
            "Git Assistant works by running git, and no git was found on this "
            "machine.\n\nInstall Git for Windows from https://git-scm.com/download/win "
            "and start Git Assistant again.",
        )
        return 1

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(
            None,
            "System tray unavailable",
            "No system tray is available on this system, so the app cannot run.",
        )
        return 1

    # A second launch hands off to the running instance and exits, so the
    # shortcut never stacks up tray icons.
    if _signal_running_instance():
        return 0

    # Import after QApplication exists (widgets require a running app object).
    from git_assistant.ui.tray import TrayApp

    tray_app = TrayApp(app)  # keep a reference so it isn't garbage-collected
    server = _listen_for_other_instances(tray_app.show_main_window)

    # Launched from a shortcut (not the startup entry): show the window, since
    # clicking an icon that only adds a tray entry looks like nothing happened.
    if STARTUP_FLAG not in sys.argv[1:]:
        QTimer.singleShot(0, tray_app.show_main_window)

    faults.write("Started; entering the event loop.")
    exit_code = app.exec()
    server.close()
    del tray_app
    # A clean exit says so, so a log ending mid-start-up is recognisable as one.
    faults.write(f"Exited normally ({exit_code}).")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
