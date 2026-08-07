# Updating

Git Assistant does not update itself. It asks **winget** what version of
`StefanGhitescu.GitAssistant` is published, and if that is newer than the one
running it asks winget to install it. Everything that ends up on disk is fetched
and hash-checked by the Windows Package Manager against the manifest merged into
`microsoft/winget-pkgs` — see [`docs/winget.md`](../../../docs/winget.md) and
[`installer/winget/`](../../../installer/winget).

The code is [`winget.py`](winget.py); the Qt glue is
`git_assistant.ui.update_prompt`.

## What this replaced, and why

There used to be a TUF-verified self-updater here: `root.json` (a 3-of-5 root
shipped inside the build), `update_url.txt`, a `dist_client` wheel carrying a
compiled Rust verifier, and `update.json` for pointing an installation at a
different service. It worked, and the trust model was stronger than winget's in
one specific respect — releases were signed by keys held offline.

It went anyway, because of the capability rather than the cryptography. **An
unsigned binary that downloads an executable and runs it is behaviourally a
dropper**, which is how builds of this application kept being quarantined; one
release was blocked *in the build tree*. No configuration switch answers "can
this program fetch and execute something" — only the code's absence does, which
is why a second installer existed with the whole subsystem compiled out.

Shelling out to winget removes the capability instead of hiding it. There is now
one build, no `-noupdate` variant, and nothing in this package that fetches or
executes a release.

## When updating is offered at all

Three conditions, all required, all in `unavailable_reason()`:

1. **Windows.** winget is Windows-only.
2. **winget is on the machine.** `shutil.which`, then the App Execution Alias in
   `%LOCALAPPDATA%\Microsoft\WindowsApps` directly — the alias is the usual
   answer, but it is on the *user's* PATH, which a process launched by an
   installer may not have yet.
3. **This is a packaged build** (`sys.frozen`), not a source checkout. A
   checkout is refused because upgrading one means installing the packaged
   application beside it, and because a developer should not be told about a
   release every five minutes.

Each reason is a sentence the UI shows. "winget has not published it yet",
"there is no winget here" and "you are running a checkout" all look identical
from the outside — an application that never updates — so they are said apart.

## How often it checks

**At startup, then every five minutes** (`CHECK_MINUTES`), so an application
left running for days notices a release without anyone opening a window. The
window runs its own check when it is opened or raised.

A check is one `winget search`, which reads a local source index winget itself
keeps current; it is not a download. The tray holds one check thread at a time,
which matters more at five-minute intervals than it did at the four hours the
old updater used — a winget that is slow because its index is being rebuilt must
not accumulate a thread per tick.

Silent unless there is something to install. A failed check says nothing,
because a laptop that starts offline would otherwise produce a toast every five
minutes; the failure is shown in the window's version readout, where somebody
looking for it will find it. A version already offered this session is not
offered again — that is what turns an update into nagware.

## Reading winget's output

winget has no machine-readable output for `search` or `list`. The one thing
parsed is the version, and it is found by locating the **package identifier** in
the row and taking the next token:

```
Name          Id                           Version  Source
----------------------------------------------------------
Git Assistant StefanGhitescu.GitAssistant  0.4.0    winget
```

Matching the `Version` header instead would work on an English Windows and
quietly find nothing on any other — the worst shape a bug can take, since it
cannot be reproduced by whoever wrote it. The identifier is never translated.

Exit code 20, or "No package found", means the package is not published. That is
not an error: it is the state today, until the manifest is merged.

## Installing

`winget upgrade` if winget already lists the package, `winget install` if not.
Both, because this application also ships an NSIS installer and a portable zip:
`winget upgrade` on an install winget never made does nothing at all, and the
installer replacing an existing install in place is how it has always been
upgraded. The consent dialog names whichever command it is about to run.

`--silent` so the installer's window does not open behind the tray. A
per-machine install needs elevation to replace `Program Files`, so winget raises
a UAC prompt; declining it is reported, not swallowed — the user pressed
**Install now**, and silence after that is indistinguishable from a dead button.

The portable zip is a real exception rather than an oversight. An unzipped copy
sets `sys.frozen`, so it passes the packaged-build check, but winget would
install a *second* copy under `%LOCALAPPDATA%` while the folder actually being
run sat unchanged and stale. Portable means portable: replace the folder.

## Exercising it during development

A checkout is refused, so the useful seam is the `runner` argument every command
takes — pass something that returns a `CompletedProcess` and no process starts.
That is how `tests/test_updating.py` drives the whole module. To watch it talk
to a real winget without a packaged build, call `available_version()` directly;
it does not go through `unavailable_reason()`.
