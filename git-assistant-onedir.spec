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
# The rules the Code Review tab ships with. Read at runtime through
# git_assistant.packaged.data_file, so it has to land beside the icon.
REVIEW_RULES = ROOT / "src" / "git_assistant" / "resources" / "review_rules.json"

# Shared with the onefile spec so both builds describe themselves identically.
sys.path.insert(0, str(ROOT / "tools"))
from win_version_info import read_version, version_resource  # noqa: E402

# The updater is an optional dependency. dist_client wraps a native library via
# ctypes, which PyInstaller cannot follow - collect_all pulls it in explicitly.
# Mirrors the same conditional in .github/workflows/release.yml.
_extra_datas, _extra_binaries, _extra_hidden = [], [], []
if find_spec("dist_client") is not None:
    _extra_datas, _extra_binaries, _extra_hidden = collect_all("dist_client")

# Langfuse tracing. OpenTelemetry resolves its exporters and propagators through
# entry points, which PyInstaller does not follow, so naming the packages as
# hidden imports is not enough -- collect_all brings the metadata with them.
# Optional in the same sense `dist_client` is: a checkout without it must still
# build, and the application already degrades to "not sending traces".
_lf_datas, _lf_binaries, _lf_hidden = [], [], []
if find_spec("langfuse") is not None:
    for _pkg in ("langfuse", "opentelemetry"):
        _d, _b, _h = collect_all(_pkg)
        _lf_datas += _d
        _lf_binaries += _b
        _lf_hidden += _h

a = Analysis(
    ["src/git_assistant/__main__.py"],
    pathex=["src"],
    binaries=[*_extra_binaries, *_lf_binaries],
    datas=[
        (str(ICON), "git_assistant/resources"),
        (str(REVIEW_RULES), "git_assistant/resources"),
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
        *_lf_datas,
    ],
    # `anthropic` is imported inside a function (git_assistant.claude_client)
    # so a build without it still runs the other providers. Declared here
    # anyway: a lazily-imported optional dependency is exactly the shape
    # PyInstaller can miss, and the failure lands at generate time. `openpyxl`
    # is the same shape: imported inside git_assistant.review.xlsx, and missed
    # here it fails when a rule table is imported, not at start-up.
    hiddenimports=[
        "anthropic",
        "openpyxl",
        "tiktoken_ext",
        "tiktoken_ext.openai_public",
        *_extra_hidden,
        *_lf_hidden,
    ],
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

# The MCP server, which a client starts and talks to over stdin/stdout. It has
# to be its own executable because a windowed build has no usable standard
# streams on Windows. Same analysis and same script as the app above -- which
# of the two a process becomes is decided by argv, in git_assistant/__main__.py.
mcp_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GitAssistantMcp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # stdio IS the transport
    hide_console="hide-early",  # ...but a client should not see a window flash
    disable_windowed_traceback=False,
    icon=str(ICON),
    version=version_resource(read_version(ROOT), "GitAssistantMcp"),
)

coll = COLLECT(
    exe,
    mcp_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="GitAssistant",
)
