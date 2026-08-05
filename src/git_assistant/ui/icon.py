"""Application icon.

Prefers the bundled multi-resolution ``resources/icon.ico`` (regenerate with
``uv run python tools/make_icon.py``) so Windows can pick the right size for the
taskbar, alt-tab and Explorer. Falls back to drawing the badge at runtime, which
keeps the app working from a plain source checkout with no build step.
"""

from __future__ import annotations

from pathlib import Path

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QIcon, QPainter, QPixmap

from git_assistant.packaged import data_file


def icon_file() -> Path:
    """Path to the bundled .ico, working both from source and PyInstaller.

    Returns the source-tree path when nothing is there, so the caller's
    ``is_file()`` check answers "no icon" rather than this raising.
    """
    found = data_file("resources", "icon.ico")
    return found or Path(__file__).resolve().parent.parent / "resources" / "icon.ico"


def app_icon(size: int = 64) -> QIcon:
    """Return the application icon (bundled .ico if available, else drawn)."""
    path = icon_file()
    if path.is_file():
        icon = QIcon(str(path))
        if not icon.isNull():
            return icon
    return draw_icon(size)


def draw_icon(size: int = 64) -> QIcon:
    """Draw a simple round 'commit' badge icon with QPainter."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    # Filled circle background.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor("#2d7d46")))
    painter.drawEllipse(QRectF(2, 2, size - 4, size - 4))

    # A stylized commit node: small inner circle + vertical line.
    painter.setBrush(QBrush(QColor("#ffffff")))
    r = size * 0.16
    cx = size / 2
    cy = size / 2
    painter.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))
    pen = painter.pen()
    pen.setColor(QColor("#ffffff"))
    pen.setWidthF(size * 0.06)
    painter.setPen(pen)
    painter.drawLine(int(cx), int(size * 0.12), int(cx), int(cy - r))
    painter.drawLine(int(cx), int(cy + r), int(cx), int(size * 0.88))

    painter.end()
    return QIcon(pm)
