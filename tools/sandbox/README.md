# Testing a build on a clean Windows

Windows Sandbox is a throwaway Windows that starts from nothing every time: no
Visual C++ runtime beyond the OS's own, no Python, no Qt, nothing this project
has ever installed. That is what makes it the right place to reproduce a
start-up failure — the 0.3.16 crash winget reported (`Qt6Core.dll`,
`c0000409` with subcode `7`, which is `abort()`, which in Qt means `qFatal`)
does not happen on a machine that has been developing this application, and
never will.

## Once, to enable it

Sandbox is an optional Windows feature and is off by default. In an **elevated**
PowerShell:

```powershell
Enable-WindowsOptionalFeature -Online -FeatureName "Containers-DisposableClientVM" -All
```

Then reboot. (Requires Windows Pro, Enterprise or Education, and virtualisation
enabled in the firmware.)

## Every time

1. Download `GitAssistant-<version>-noupdate-user-windows-x64-setup.exe` from
   the release and drop it in **this folder**, beside `git-assistant.wsb`.
2. Double-click `git-assistant.wsb`.
3. Wait. It installs silently, starts the application, and writes everything
   into `results\` **in this folder** — the sandbox is destroyed when you close
   it, so anything left inside goes with it.
4. Read `results\report.txt`.

The per-user installer asks for no elevation, so there is nothing to click.

If the sandbox refuses to start with an error about the mapped folder, replace
`<HostFolder>.</HostFolder>` with this folder's full path — relative paths are
a recent addition and older builds want an absolute one.

## Reading the result

**`Qt FATAL: ...` in `startup.log`** — the answer, in Qt's own words. This is
what the whole `faults` module exists to capture, and it is the line the WER
bucket threw away.

**`MISSING: ...\plugins\platforms`** — the platform plugin never shipped. That
alone is enough to abort `QApplication`, and it is a packaging fault, not a code
one.

**`startup.log` NOT WRITTEN, but the application is running** — not a start-up
failure at all. `faults.install()` is the first statement of `main()`, so a
running application that has written nothing means the log *path* is wrong or
the write failed. The `--- …\git-assistant ---` listing above it says which:
an empty directory is one fault, a directory full of everything except
`startup.log` is a different one.

**`NO startup.log` and it is not running** — it died before `faults.install()`,
which puts the failure in Python's own start-up (a missing DLL, a failed import
in the frozen bundle) rather than in Qt. The collected WER and crash-dump files
are then the next thing to look at.

**`Still running after 30s`** — look at the sandbox window before closing it.
A dialog on screen is the finding; Qt shows one for some fatal errors, and an
unattended validation VM has nobody to dismiss it.

## What this does not prove

A clean *Windows* is not a clean *everything*. Sandbox runs the same build of
Windows as the host, and winget's validation machine may not. It also has
networking and a desktop; something that only fails without either will pass
here. It reproduces the common case — a missing runtime, a missing plugin, a
fatal at start-up — and not every case.
