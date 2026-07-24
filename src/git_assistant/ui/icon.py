"""Tray icon drawn at runtime with QPainter (no bundled image file needed)."""

from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QIcon, QPainter, QPixmap


def app_icon(size: int = 64) -> QIcon:
    """Return a simple round 'commit' badge icon."""
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
