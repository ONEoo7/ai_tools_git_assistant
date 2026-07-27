"""Generate the multi-resolution application icon (resources/icon.ico).

Renders the same badge drawn by ``git_assistant.ui.icon`` at several sizes and
packs them into a single ICO container using PNG-compressed entries (supported
by Windows Vista and later). Run after changing the icon artwork:

    uv run python tools/make_icon.py
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

# Make ``src`` importable when running from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from PyQt6.QtCore import QBuffer, QIODevice  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

SIZES = (16, 24, 32, 48, 64, 128, 256)
OUT = ROOT / "src" / "git_assistant" / "resources" / "icon.ico"


def png_bytes(size: int) -> bytes:
    # draw_icon (not app_icon) so we render the artwork rather than reloading
    # any previously generated .ico.
    from git_assistant.ui.icon import draw_icon

    pixmap = draw_icon(size).pixmap(size, size)
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buf, "PNG")
    return bytes(buf.data())


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    """Pack (size, png_bytes) pairs into an ICO container."""
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type=icon, count
    entries = b""
    offset = len(header) + 16 * len(images)
    for size, data in images:
        # 256 is encoded as 0 in the directory entry.
        dim = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset
        )
        offset += len(data)
    return header + entries + b"".join(d for _, d in images)


def main() -> int:
    app = QApplication(sys.argv)  # noqa: F841 - QPixmap needs a QApplication
    images = [(s, png_bytes(s)) for s in SIZES]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(build_ico(images))
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, sizes: {list(SIZES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
