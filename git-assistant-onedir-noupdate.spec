# PyInstaller spec for the installed build WITHOUT the self-updater.
#
# Build:  uv run --extra build pyinstaller git-assistant-onedir-noupdate.spec
# Output: dist/GitAssistant-noupdate/GitAssistant.exe  + support files
#
# Same application as git-assistant-onedir.spec, minus the ability to download
# and run an executable. That capability is what makes an unsigned build look
# like a dropper to endpoint protection, and no configuration switch answers
# the question "can this program fetch and execute something" -- only its
# absence does. So the package is excluded from the bundle rather than turned
# off at runtime; git_assistant.features derives the flag from whether it is
# there, and the UI adjusts.
#
# Ship this to environments that will not tolerate a self-updater. Upgrading is
# then what it is for most software: install the new version over the old one.

import sys
from pathlib import Path

ROOT = Path(SPECPATH)
ICON = ROOT / "src" / "git_assistant" / "resources" / "icon.ico"

sys.path.insert(0, str(ROOT / "tools"))
from win_version_info import read_version, version_resource  # noqa: E402

# The updater and the verifier it wraps. `dist_client` is loaded through ctypes
# and reached only from git_assistant.updating, so it goes with it.
#
# httpx is NOT excluded, however tempting it looks: lmstudio_client imports it
# too, and it is how the application talks to LM Studio. Removing it produced a
# build that started and then died with ModuleNotFoundError before the tray
# appeared. "Used only by the updater" has to be checked, not assumed -- there
# is a test below that pins this.
EXCLUDED = [
    "git_assistant.updating",
    "git_assistant.updating.client",
    "git_assistant.ui.update_prompt",
    "dist_client",
    "tkinter",
    "matplotlib",
    "numpy",
    "PySide6",
    "PyQt5",
]

a = Analysis(
    ["src/git_assistant/__main__.py"],
    pathex=["src"],
    binaries=[],
    # No root.json and no update_url.txt: the trust root and the address only
    # mean anything to the updater, and shipping them in a build that cannot
    # update would suggest it can.
    datas=[(str(ICON), "git_assistant/resources")],
    # `anthropic` is imported inside a function (git_assistant.claude_client)
    # so a build without it still runs the other providers. Declared here
    # anyway: a lazily-imported optional dependency is exactly the shape
    # PyInstaller can miss, and the failure lands at generate time.
    hiddenimports=["anthropic", "tiktoken_ext", "tiktoken_ext.openai_public"],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDED,
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
    version=version_resource(read_version(ROOT), "GitAssistant"),
)

# The MCP server: a client starts it and talks over stdin/stdout, which a
# windowed build cannot do. Same analysis and script as the app; argv decides
# which one a process becomes. See git_assistant/__main__.py.
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
    hide_console="hide-early",  # ...but no window flash when a client starts it
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
    # Its own directory so the two installers never package each other's build.
    name="GitAssistant-noupdate",
)
