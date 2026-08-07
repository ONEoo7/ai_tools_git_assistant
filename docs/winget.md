# winget manifests

The manifests are in [`installer/winget/`](../installer/winget). They are not
read by anything at build time. They are the copy **we** control, so that the
fields winget-pkgs cannot work out for itself are written down, reviewable and
diffable, instead of living only in a pull request against someone else's
repository.

**winget is now also how the application updates itself.** It asks
`winget search --id StefanGhitescu.GitAssistant --exact` at startup and every
five minutes, and on the user's say-so runs `winget upgrade`. So the manifests
below are no longer only a distribution channel — an identifier that does not
match, or a version that never gets published, is an application that silently
never updates. See
[`src/git_assistant/updating/README.md`](../src/git_assistant/updating/README.md).

This document is *here* and not in that directory because the directory is
copied wholesale into `manifests/s/StefanGhitescu/GitAssistant/<version>/`, and
`winget validate` reads every file in it — a README there failed validation on
a line that started with a backtick.

## Why they exist at all

`vedantmgoyal9/winget-releaser`, which the release workflow runs, calls
`wingetcreate update`. That regenerates `PackageVersion`, `InstallerUrl` and
`InstallerSha256` from the release's assets and **carries every other field
forward from the previously published version**.

Which means:

- anything here has to reach winget-pkgs **once**, by hand;
- after that, every automatically submitted release inherits it;
- and `wingetcreate update` cannot add a field that was never there, so
  forgetting one is not something a later release fixes.

## The one that matters: `Dependencies`

```yaml
Dependencies:
  PackageDependencies:
    - PackageIdentifier: Git.Git
```

0.3.16 failed winget's manual validation with a crash in `Qt6Core.dll`
(`c0000409`, subcode 7 — `FAST_FAIL_FATAL_APP_EXIT`, which is `qFatal`).
Reproduced in Windows Sandbox and read out of the start-up log the build now
writes:

```
File "git_assistant\identities.py", line 172, in _from_git
File "git_assistant\git_ops.py", line 322, in _run_global
FileNotFoundError: [WinError 2] The system cannot find the file specified
```

**There is no git on a clean Windows**, and this application is a front end for
git. Two separate faults, and both needed fixing:

- The code raised. It no longer does — see `git_ops._cannot_run`; a missing git
  is a failed `GitResult`, and start-up says so in a sentence.
- The package did not declare that it needs git. That is this file.

An application that installs successfully and then tells you to go and install
something else is not what anyone wants from a package manager that could have
installed it.

## Submitting

The identifier is the primary key in winget-pkgs and cannot be changed once
published, so it must match exactly: `StefanGhitescu.GitAssistant`.

Manifests live at
`manifests/s/StefanGhitescu/GitAssistant/<version>/` in a fork of
`microsoft/winget-pkgs`. To validate before opening a PR:

```powershell
winget validate --manifest .\installer\winget
winget install --manifest .\installer\winget
```

`ManifestVersion` here is `1.12.0`. If the open PR uses a different schema
version, match it rather than this — a mixed set is a validation failure, and
the version in the PR is the one a reviewer has already looked at.

`PackageVersion`, `ReleaseDate`, `InstallerUrl` and `InstallerSha256` go stale
by design; the automation replaces all four on the next release.

**These four cannot be submitted as-is right now.** They name
`git-assistant-<version>-user-windows-x64-setup.exe`, and the released v0.3.18
assets are still called `...-noupdate-user-...` — the `-noupdate` suffix named a
second build with the self-updater compiled out, which no longer exists. The
first release cut after that change publishes the new filename and the
automation regenerates the URL and hash to match. Until then, submitting by hand
means taking the URL and hash from the release you are actually submitting.
