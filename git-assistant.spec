# PyInstaller spec for Git Assistant.
#
# Build:  uv run --extra build pyinstaller git-assistant.spec
# Output: dist/GitAssistant.exe  (single file, no console window)

import sys
from importlib.util import find_spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
ICON = ROOT / "src" / "git_assistant" / "resources" / "icon.ico"

# Shared with the onedir spec so both builds describe themselves identically.
sys.path.insert(0, str(ROOT / "tools"))
from win_version_info import read_version, version_resource  # noqa: E402

# Optional updater: dist_client loads a native library via ctypes, which
# PyInstaller cannot follow. Mirrors .github/workflows/release.yml.
_extra_datas, _extra_binaries, _extra_hidden = [], [], []
if find_spec("dist_client") is not None:
    _extra_datas, _extra_binaries, _extra_hidden = collect_all("dist_client")

a = Analysis(
    ["src/git_assistant/__main__.py"],
    pathex=["src"],
    binaries=_extra_binaries,
    datas=[
        # Ship the .ico so the tray/window icon resolves at runtime.
        (str(ICON), "git_assistant/resources"),
        # TUF trust root: the updater refuses to run without it.
        (str(ROOT / "src" / "git_assistant" / "updating" / "root.json"),
         "git_assistant/updating"),
        # Where this build looks for updates. Bundled here as well as by the
        # onedir spec and the release workflow: three build paths that have to
        # agree about what ships, and did not.
        (str(ROOT / "src" / "git_assistant" / "updating" / "update_url.txt"),
         "git_assistant/updating"),
        *_extra_datas,
    ],
    # `anthropic` is imported inside a function (git_assistant.claude_client)
    # so a build without it still runs the other providers. Declared here
    # anyway: a lazily-imported optional dependency is exactly the shape
    # PyInstaller can miss, and the failure lands at generate time.
    hiddenimports=["anthropic", "tiktoken_ext", "tiktoken_ext.openai_public", *_extra_hidden],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "PySide6", "PyQt5"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="GitAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # tray app: no console window
    disable_windowed_traceback=False,
    icon=str(ICON),
    # See the onedir spec and tools/win_version_info.py.
    version=version_resource(read_version(ROOT), "GitAssistant"),
)
