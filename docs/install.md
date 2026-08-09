# Install and update

## From source

```bash
uv sync
uv run git-assistant
```

Or `uv run python -m git_assistant`. Python 3.13.

## Built distributables

Three, all built from one script — see [building](build.md).

| | |
|---|---|
| **Per-user installer** | `%LOCALAPPDATA%\Programs\GitAssistant`, no admin. This is what winget publishes. |
| **Per-machine installer** | Program Files, needs admin. |
| **Portable** | A single `.exe`. No install, no update mechanism — replace the folder to upgrade. |

The installer is deliberately per-user. A Program Files install would make every
`winget upgrade` raise a UAC prompt from a process the user did not start.

## First run

1. **Connection & Model** — pick a provider. LM Studio is the default and needs
   no key; enter its address, press *Test connection & list models*, pick a
   model, and set the context window to match how the model is loaded.

   With LM Studio selected there is also **Set up LM Studio for me…**, which
   installs LM Studio through winget, turns on developer mode and the background
   service, downloads `lmstudio-community/Qwen3.5-4B-GGUF@Q8_0` (~5 GB) and
   configures it for 32,768 tokens with thinking off. It names every step before
   doing anything and skips whatever is already in place.

   It reads and writes LM Studio's own configuration files. Their shapes are not
   a published contract, so a future LM Studio may move them — at which point
   the step that touches them fails with what it expected, and everything else
   still runs.

2. **Repositories & Settings** — add repositories one at a time, or scan a
   folder for all of them. Submodules are found too and listed under the
   repository they belong to. Tick a scanned folder to **auto-watch** it, and
   repositories cloned there afterwards are added on their own.

3. **Identities** — add the identities you commit as. See
   [identities](identities.md).

4. Stage something, pick the repository on the **Generate Commit Message** tab,
   and press *Generate*.

## Updating

Git Assistant does not update itself. It asks **winget** whether a newer
`StefanGhitescu.GitAssistant` is published — at startup and every five minutes —
and offers to install it:

```bash
winget upgrade --id StefanGhitescu.GitAssistant --exact
```

`winget install` instead, when winget does not already list the package: this
app also ships an NSIS installer and a portable zip, and `winget upgrade` on an
install winget never made does nothing at all. The consent dialog names whichever
command it is about to run, and that command is yours to run by hand if you would
rather the application did not.

Everything that lands on disk is fetched and hash-checked by the Windows Package
Manager against the merged manifest. **Nothing in this application downloads or
executes a release.**

Updating is off — with the reason shown in *Advanced → Update source* — when
there is no winget, when running from a source checkout, or off Windows. The
version readout at the bottom left shows the last check; click it when it says
an update is available.

### Why it is not a self-updater

This replaced one. That version downloaded an installer, verified it against
metadata signed by keys held for this project, and ran it. The cryptography was
not the problem — the *capability* was. An unsigned binary that downloads and
executes something is behaviourally a dropper, which is why builds of it kept
being quarantined, and why a second installer had to exist with the whole
subsystem compiled out.

There is one build now, no `-noupdate` variant, and no such code in it. See
[building](build.md#antivirus-and-reputation).
