"""Application entry point: bootstrap Qt, install the tray, run the event loop."""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from git_assistant.ui.icon import app_icon

APP_ID = "ONEoo7.GitAssistant"


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


def main() -> int:
    _set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName("git-assistant")
    # Use the tray icon for window title bars / taskbar too, so they match.
    app.setWindowIcon(app_icon())
    # Tray apps must keep running after their dialogs close.
    app.setQuitOnLastWindowClosed(False)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(
            None,
            "System tray unavailable",
            "No system tray is available on this system, so the app cannot run.",
        )
        return 1

    # Import after QApplication exists (widgets require a running app object).
    from git_assistant.ui.tray import TrayApp

    tray_app = TrayApp(app)  # keep a reference so it isn't garbage-collected
    exit_code = app.exec()
    del tray_app
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
