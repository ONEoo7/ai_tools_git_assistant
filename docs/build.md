# Building

Two distributables in three flavours, all from one script. The version is read
from `src/git_assistant/__init__.py`, so the installer cannot drift from the app.

```bash
uv run --extra build python tools/build.py
```

| Target | Output | Notes |
|---|---|---|
| `portable` | `dist/GitAssistant.exe` | Single file, no install, run from anywhere |
| `installer` | `dist/GitAssistant-<version>-user-setup.exe` | Per-user, no admin |
| `installer-machine` | `dist/GitAssistant-<version>-machine-setup.exe` | Program Files, needs admin |

Build one with `python tools/build.py portable`. The installers need NSIS
(`winget install NSIS.NSIS`).

The installed build uses PyInstaller's **onedir** layout rather than onefile, so
start-up does not re-extract the whole bundle each launch and the installer
winget runs replaces individual changed files.

The icon is a multi-resolution `.ico` at
`src/git_assistant/resources/icon.ico`. Regenerate it after changing the artwork
in `ui/icon.py`:

```bash
uv run python tools/make_icon.py
```

## No automatic startup, and why

The installer does **not** register the app to run at sign-in, even though it is
a tray app and that would be the natural default. Two mechanisms were tried and
both were flagged by Windows Defender's cloud heuristics:

| Release | Mechanism | Detection |
|---|---|---|
| ≤ 0.3.7 | `HKCU\…\CurrentVersion\Run` value | `Behavior:Win32/SuspiciousFileInRunKey.A!cl` |
| 0.3.8 | Shortcut in the Startup folder | `Trojan:Win32/SuspStartupFolderFileTarget.A!cl` |

Two names for one judgement. What is scored is not the *mechanism* but the
*target*: an unsigned, low-prevalence executable in a user-writable directory
that registers itself to run at sign-in. That is the shape of malware
persistence, and this application matches every part of it except the intent.

It did not merely warn. It quarantined the executable and deleted the shortcuts,
the registry entries and the uninstall entry, leaving an install that could
neither start nor be removed — and once the behaviour was observed, the same
binary was blocked **in the build tree**, so the project would not compile
without an antivirus exclusion.

Installing 0.3.9 or later removes autostart left by either earlier release.

**Setting it up yourself** is still supported. The app takes `--startup`, which
brings it up in the tray only — without it, launching opens the window, since an
icon that appears to do nothing reads as broken. Create a shortcut to
`GitAssistant.exe --startup` in `shell:startup`. You may hit the same detection;
that is precisely why the installer will not do it for you.

## Antivirus and reputation

The executable carries a version resource — publisher, product, version, see
`tools/win_version_info.py`. Shipping without one is unusual for real software
and common in packed malware.

The installer no longer shells out to PowerShell to stop a running copy; it uses
`taskkill` on both executable names instead, after the installer was separately
detected as `Trojan:Script/Wacatac.F!ml`, a *script* detection whose only
plausible subject was that call. The cost is precision: it will also stop a
portable build running from elsewhere, which the old path-based match left alone.

Both are weak evidence, and neither is the fix.

### What was measured, and what did not work

Five behavioural changes were tried against Defender. All were eliminated as the
cause:

| Change | Outcome |
|---|---|
| Run key → Startup-folder shortcut | Flagged by the sibling rule |
| Autostart removed entirely | Flagged on run, same rule |
| New hash (version resource) | Clean until it ran, then flagged |
| Self-updater removed from the bundle | Flagged, **identical verdict** |
| PowerShell removed from the installer | Unverified |
| Program Files instead of `%LOCALAPPDATA%` | Unverified |

The verdict never changed. Behavioural tuning is not a productive direction
here, and a build rebuilt from unchanged source reproduces the same hash and the
same block — so "rebuild and retry" measures nothing.

The observed sequence is always the same: a fresh hash builds and sits readable
in `build/` and `dist/`; installing and running it produces the verdict; the
verdict then propagates back onto the build-tree copies, so the project stops
compiling.

**Code signing is the fix.** The binary is unsigned, so its hash changes with
every release and reputation never accumulates — each build is judged on its
features alone. Azure Trusted Signing is a per-month option that needs no
hardware token. If a build is flagged, report it at
<https://www.microsoft.com/en-us/wdsi/filesubmission>, which retracts the verdict
for every user rather than one machine.

## Per-machine install

`installer-machine` installs to `%ProgramFiles%\GitAssistant` and requires
elevation. A user-writable install directory is one of the ingredients these
heuristics score; the ordinary installer is per-user because that is the build
winget upgrades, and a Program Files install would make every `winget upgrade`
raise a UAC prompt from a process the user did not start.

Consequences: a UAC prompt at install, shortcuts and the uninstall entry become
machine-wide (`HKLM`), and the finish page no longer offers to launch the app,
since it would inherit the installer's elevated token. Per-user settings are
untouched — the app resolves those with `platformdirs` regardless.

Both installers clean up the *per-user* autostart leftovers from ≤ 0.3.8, since
every release that wrote them was a per-user install.
