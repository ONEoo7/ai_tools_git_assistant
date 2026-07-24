"""Application entry point: bootstrap Qt, install the tray, run the event loop."""

from __future__ import annotations

import sys

from PyQt6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

from git_assistant.ui.icon import app_icon


def main() -> int:
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
