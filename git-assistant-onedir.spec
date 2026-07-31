# PyInstaller spec for the *installed* build (one directory).
#
# Build:  uv run --extra build pyinstaller git-assistant-onedir.spec
# Output: dist/GitAssistant/GitAssistant.exe  + support files
#
# Onedir (not onefile) because this build is what the NSIS installer ships:
#   - startup is much faster; a tray app launches with Windows and stays running,
#     whereas onefile re-extracts the whole bundle to %TEMP% on every launch.
#   - the self-updater can replace individual changed files instead of swapping
#     a ~43 MB monolith.
# The onefile spec (git-assistant.spec) is kept for the portable download.

import sys
from importlib.util import find_spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_all

ROOT = Path(SPECPATH)
ICON = ROOT / "src" / "git_assistant" / "resources" / "icon.ico"

# Shared with the onefile spec so both builds describe themselves identically.
sys.path.insert(0, str(ROOT / "tools"))
from win_version_info import read_version, version_resource  # noqa: E402

# The updater is an optional dependency. dist_client wraps a native library via
# ctypes, which PyInstaller cannot follow - collect_all pulls it in explicitly.
# Mirrors the same conditional in .github/workflows/release.yml.
_extra_datas, _extra_binaries, _extra_hidden = [], [], []
if find_spec("dist_client") is not None:
    _extra_datas, _extra_binaries, _extra_hidden = collect_all("dist_client")

a = Analysis(
    ["src/git_assistant/__main__.py"],
    pathex=["src"],
    binaries=_extra_binaries,
    datas=[
        (str(ICON), "git_assistant/resources"),
        # TUF trust root: the updater refuses to run without it.
        (str(ROOT / "src" / "git_assistant" / "updating" / "root.json"),
         "git_assistant/updating"),
        # The address this build looks for updates at. Committed, and bundled
        # here as well as by the release workflow -- a local build that omits
        # it has an updater with nowhere to look, which is indistinguishable
        # from a broken one and was omitted here until it was noticed in an
        # installed build that had no packaged URL at all.
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
    [],
    exclude_binaries=True,  # onedir: binaries live beside the exe
    name="GitAssistant",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # tray app: no console window
    disable_windowed_traceback=False,
    icon=str(ICON),
    # Publisher, product and version, read by Windows and by the reputation
    # systems that judge an unsigned binary. Shipping without it is unusual
    # for real software and common in packed malware; see tools/win_version_info.py.
    version=version_resource(read_version(ROOT), "GitAssistant"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="GitAssistant",
)
