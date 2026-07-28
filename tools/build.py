"""Build the distributable artefacts.

    uv run --extra build python tools/build.py            # portable + installed
    uv run --extra build python tools/build.py portable   # dist/GitAssistant.exe
    uv run --extra build python tools/build.py installer  # dist/GitAssistant-<v>-setup.exe

The version is read from ``src/git_assistant/__init__.py`` and passed to NSIS,
so the installer can never disagree with what the running app reports.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
NSI = ROOT / "installer" / "git-assistant.nsi"

# Where NSIS usually lands; PATH wins if makensis is already there.
NSIS_CANDIDATES = (
    Path(r"C:\Program Files (x86)\NSIS\makensis.exe"),
    Path(r"C:\Program Files\NSIS\makensis.exe"),
)


def app_version() -> str:
    text = (ROOT / "src" / "git_assistant" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise SystemExit("could not find __version__ in src/git_assistant/__init__.py")
    return m.group(1)


def find_makensis() -> Path | None:
    found = shutil.which("makensis")
    if found:
        return Path(found)
    return next((p for p in NSIS_CANDIDATES if p.is_file()), None)


def run(cmd: list[str]) -> None:
    print(f"$ {' '.join(cmd)}", flush=True)
    proc = subprocess.run(cmd, cwd=ROOT)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def pyinstaller(spec: str) -> None:
    run([sys.executable, "-m", "PyInstaller", spec, "--noconfirm"])


def build_portable() -> None:
    """Single self-contained exe - no install, run from anywhere."""
    pyinstaller("git-assistant.spec")
    print(f"portable  -> {DIST / 'GitAssistant.exe'}")


def build_installer(version: str) -> None:
    """Onedir build wrapped in a per-user NSIS installer."""
    pyinstaller("git-assistant-onedir.spec")
    makensis = find_makensis()
    if makensis is None:
        raise SystemExit(
            "makensis not found. Install NSIS (winget install NSIS.NSIS) or add "
            "makensis to PATH, then re-run. The onedir build in dist/GitAssistant "
            "is ready for it."
        )
    run([str(makensis), f"/DVERSION={version}", str(NSI)])
    print(f"installer -> {DIST / f'GitAssistant-{version}-setup.exe'}")


def main(argv: list[str]) -> int:
    target = (argv[1] if len(argv) > 1 else "all").lower()
    version = app_version()
    print(f"Git Assistant {version}")

    if target in ("all", "portable"):
        build_portable()
    if target in ("all", "installer"):
        build_installer(version)
    if target not in ("all", "portable", "installer"):
        raise SystemExit(f"unknown target {target!r} (use: all | portable | installer)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
