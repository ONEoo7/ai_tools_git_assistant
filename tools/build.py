"""Build the distributable artefacts.

    uv run --extra build python tools/build.py            # portable + installed
    uv run --extra build python tools/build.py portable   # dist/GitAssistant.exe
    uv run --extra build python tools/build.py installer  # dist/GitAssistant-<v>-setup.exe
    uv run --extra build python tools/build.py installer-noupdate
                                    # dist/GitAssistant-<v>-noupdate-setup.exe
                                    # same app with the self-updater left out

The version is read from ``src/git_assistant/__init__.py`` and passed to NSIS,
so the installer can never disagree with what the running app reports.
"""

from __future__ import annotations

import os
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

# ---- code signing -----------------------------------------------------------
# Azure Trusted Signing, configured entirely through the environment: no account
# details in the repository, no credentials on disk, and a checkout that has not
# been set up still builds -- unsigned, with a notice -- rather than failing.
#
# Signing is what this project actually needs, not a nicety. Unsigned builds were
# repeatedly quarantined by Defender: an unsigned, zero-reputation executable in
# a user-writable directory that listens on a pipe, phones home and can download
# and run an installer matches the behavioural profile of malware, and there is
# no way to distinguish it from one without a publisher. See the README.
SIGN_VARS = ("AZURE_TS_ACCOUNT", "AZURE_TS_PROFILE", "AZURE_TS_ENDPOINT")

# Trusted Signing issues certificates valid for ~72 hours, so an untimestamped
# signature stops verifying within days. Timestamping is not optional here.
TIMESTAMP_URL = "http://timestamp.acs.microsoft.com"


def signing_config() -> dict[str, str] | None:
    """Return the signing settings, or None when signing is not configured.

    A half-configured environment is an error rather than a silent skip: the
    failure it produces otherwise is an unsigned release that looks signed
    because the build printed no warning anyone read.
    """
    values = {name: os.environ.get(name, "").strip() for name in SIGN_VARS}
    if all(values.values()):
        return values
    if any(values.values()):
        missing = ", ".join(n for n, v in values.items() if not v)
        raise SystemExit(f"signing is half-configured: also set {missing} (or none of them)")
    return None


def sign(*paths: Path) -> None:
    """Sign the given files with Azure Trusted Signing, if it is configured.

    Credentials come from the environment via Azure's DefaultAzureCredential --
    `az login` locally, or AZURE_TENANT_ID / AZURE_CLIENT_ID /
    AZURE_CLIENT_SECRET for a service principal in CI.
    """
    config = signing_config()
    if config is None:
        print("signing   -> skipped (set %s to enable)" % ", ".join(SIGN_VARS))
        return

    tool = shutil.which("sign")
    if tool is None:
        raise SystemExit(
            "signing is configured but the 'sign' tool is not on PATH.\n"
            "  dotnet tool install --global sign"
        )

    for path in paths:
        if not path.is_file():
            raise SystemExit(f"nothing to sign at {path}")
        run(
            [
                tool, "code", "trusted-signing", str(path),
                "--trusted-signing-account", config["AZURE_TS_ACCOUNT"],
                "--trusted-signing-certificate-profile", config["AZURE_TS_PROFILE"],
                "--trusted-signing-endpoint", config["AZURE_TS_ENDPOINT"],
                "--timestamp-url", TIMESTAMP_URL,
            ]
        )
        print(f"signed    -> {path}")


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
    sign(DIST / "GitAssistant.exe")
    print(f"portable  -> {DIST / 'GitAssistant.exe'}")


def build_installer(
    version: str, *, updater: bool = True, permachine: bool = False
) -> None:
    """Onedir build wrapped in an NSIS installer.

    Two independent axes:

    ``updater``     whether the self-updater is in the bundle at all (a
                    separate spec, not a setting -- the point is that the
                    capability is absent).
    ``permachine``  Program Files and elevation, versus %LOCALAPPDATA% and no
                    UAC prompt.

    They are independent, but not equally sensible in every combination: a
    self-updating per-machine install needs a UAC prompt for each update, and
    must be served a per-machine installer to update *into* -- see
    ``default_channel`` in the updater.
    """
    spec = "git-assistant-onedir.spec" if updater else "git-assistant-onedir-noupdate.spec"
    payload = DIST / ("GitAssistant" if updater else "GitAssistant-noupdate")
    suffix = ("" if updater else "-noupdate") + ("-machine" if permachine else "-user")

    pyinstaller(spec)

    # Sign the application before NSIS packages it, then sign the installer
    # afterwards. Both matter and for different reasons: the installer is what
    # SmartScreen judges at download time, and the application is what Defender
    # judges every time it runs -- signing only the installer leaves the file
    # that actually gets flagged unsigned inside it.
    sign(payload / "GitAssistant.exe")

    makensis = find_makensis()
    if makensis is None:
        raise SystemExit(
            "makensis not found. Install NSIS (winget install NSIS.NSIS) or add "
            f"makensis to PATH, then re-run. The onedir build in {payload} "
            "is ready for it."
        )

    installer = DIST / f"GitAssistant-{version}{suffix}-setup.exe"
    defines = [
        f"/DVERSION={version}",
        f"/DSRC_DIR={payload}",
        f"/DOUT_FILE={installer}",
    ]
    if permachine:
        # Program Files rather than %LOCALAPPDATA%: not writable without
        # elevation, which removes one of the ingredients Defender's heuristics
        # score against an unsigned binary.
        defines.append("/DPERMACHINE")
    run([str(makensis), *defines, str(NSI)])

    sign(installer)
    print(f"installer -> {installer}")


#: target -> (updater, permachine). "installer" keeps its old meaning so an
#: existing habit still builds the ordinary per-user, self-updating one.
INSTALLERS = {
    "installer": (True, False),
    "installer-machine": (True, True),
    "installer-noupdate": (False, True),
    "installer-noupdate-user": (False, False),
}
TARGETS = ("all", "portable", *INSTALLERS)


def main(argv: list[str]) -> int:
    target = (argv[1] if len(argv) > 1 else "all").lower()
    if target not in TARGETS:
        raise SystemExit(f"unknown target {target!r} (use: {' | '.join(TARGETS)})")

    version = app_version()
    print(f"Git Assistant {version}")

    if target in ("all", "portable"):
        build_portable()
    if target in ("all", "installer"):
        build_installer(version, updater=True, permachine=False)
    # The variants are not part of "all": each is a deliberate alternative, and
    # building them by default would put four installers of the same version in
    # dist/ for anyone who only wanted the ordinary one.
    if target in INSTALLERS and target != "installer":
        updater, permachine = INSTALLERS[target]
        build_installer(version, updater=updater, permachine=permachine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
