# Git Assistant

A **system-tray Git assistant** powered by a **local LLM** served by
[LM Studio](https://lmstudio.ai/). Built with Python 3.13, PyQt6, and `uv`.

Its first feature generates git commit messages; more Git helpers are planned.

- Lives in the system tray — click the icon or use the menu to generate a message.
- Talks to LM Studio's OpenAI-compatible API (pick IP/port, list models, select one).
- Manages a **list of repos** (add one by one, or scan a folder to add all repos
  under it), grouped by folder; the tray shows the 3 most-recent with the rest
  in a submenu.
- **Auto-watch** (opt-in per folder): tick a scanned folder and newly cloned
  repos are added automatically, via a lightweight native filesystem watcher.
- **Committer identity switcher**: a *Commit as* dropdown above the tabs shows the
  `user.email` the active repo will commit with; the **Identities** tab manages the
  list, stored in its own importable/exportable file (see below).
- Output: an **editable preview** you can **Copy** to the clipboard or **Commit** directly.
- Default format: **Conventional Commits**, with a fully **editable prompt template**.
- **Handles diffs larger than the model's context window** (see below).
- **Metrics** window: count lines of code across selected repos or a scanned
  directory, broken down by file type (uses `git ls-files`, so `.gitignore` is
  respected and binaries are skipped).

## Handling large diffs (context overflow)

A `git diff` can easily exceed a local model's context window. The tool uses a
**hybrid** strategy:

1. **Noise filtering** — lockfiles, binaries, minified assets, and any user-defined
   globs are dropped before anything is counted. A compact `--stat` is always
   included so the model still sees the overall shape.
2. **Budget** — a single **context window size** (input + output combined) is split
   by the **safety margin**: `output = window × margin`, `diff budget = window − output`.
   Because output is carved out of the same window, input + output never exceeds it.
   Set the window in Settings → Connection & Model to match the context length the
   model is loaded with in LM Studio, or leave it at `0` to auto-detect the model's
   maximum. The same tab shows the resulting split live.
3. **Single-shot** — if the whole prompt fits the budget, it's sent in one call.
4. **Map-reduce** — otherwise the diff is split per-file, then per-hunk, then
   hard-truncated if a single hunk is still too big. Each budget-sized chunk is
   summarized ("map"), the notes are condensed if they overflow ("reduce"), and a
   final Conventional-Commits message is synthesized from the notes.
   The map/reduce calls are independent, so they run **in parallel** —
   4 at a time by default, configurable via *Settings → Connection & Model →
   Parallel requests* (1 = sequential).

It never hard-fails on size — the preview window reports which strategy ran and the
token counts involved.

**Transparency:** the preview shows the commit message side by side with the
staged files. Selecting a file displays its diff with any content that was
**omitted from the prompt highlighted in red**, so it is obvious exactly what the
model did *not* see when a diff overflows the budget (truncated hunks, or files
dropped by the noise filter).

## Committer identities

The **Commit as** dropdown above the tabs shows the `user.email` that the **active
repository** will stamp on its next commit, and lets you switch it. The
**Identities** tab manages the list.

- Selecting an identity runs the equivalent of
  `git config --local user.name/user.email` **in that repository**, so it outranks
  your global config and applies to commits made from any tool, not just this one.
- The dropdown always reflects git's own answer, resolved the way git resolves it —
  repo config, `includeIf` conditional includes, then the global fallback. The note
  beside it says whether the identity is *set for this repository* or *inherited
  from global git config*.
- An identity that git reports but you have not stored is shown as
  `someone@example.com (not saved)` rather than being quietly swapped for a stored
  one.

### Signing keys

An identity can carry an optional `user.signingkey`. It is written when you select
that identity and **cleared** when you select one without a key — otherwise a commit
ends up authored by one identity and signed by another's key, which every forge
reports as *Unverified*.

If `commit.gpgsign` is on but no key resolves, the readout says **signing key
missing** rather than letting the next commit fail at the git level.

### Pushing is not the same as committing

`user.email` decides how a commit is **labelled**. It has no effect on **which
credential pushes**. You can commit as your personal identity and still push with
work credentials; git will not complain, and the forge attributes the commit to
whoever owns the email.

The right-hand readout says what will actually authenticate:

| Readout | Meaning |
|---|---|
| `push: github.com as ONEoo7` | A username is pinned (in the remote URL or `credential.…username`) |
| `push: github.com` *(amber)* | **One credential serves every account on this host** — the identity you pick does not change who you push as |
| `push: SSH to github.com (default key)` *(amber)* | Your default SSH key, whichever identity is selected |
| `push: SSH to github-personal (key from SSH config)` | A host alias, so the key is chosen per alias |

The amber cases have a tooltip with the fix — `credential.<host>.useHttpPath` for
HTTPS, or a `Host` alias with its own `IdentityFile` for SSH.

This is read from configuration only. Asking the credential helper would give a
firmer answer and can pop an authentication prompt, which is not acceptable while
merely redrawing the window.

### Where they live

Identities are kept in **`committer_identities.json`**, next to `settings.json` in
the config folder — their own file, so it can be moved between machines without
dragging along an LM Studio address or a list of local repo paths.

```json
{
  "version": 1,
  "identities": [
    { "name": "Work", "email": "me@work.example", "signingkey": "" },
    { "name": "Personal", "email": "me@personal.example", "signingkey": "ABC123" }
  ]
}
```

On first run the file is created from your **global git identity**, so the list is
never empty for no reason. It is written even when git has nothing to offer, so an
intentionally emptied list is not refilled on the next start.

**Export / Import** (Identities tab) writes and merges that file. Import *merges* —
it never deletes identities that exist only on this machine, and an email already
present is left alone rather than overwritten, so an old export cannot silently
rename the identity you are using.

Nothing about "which identity is current" is stored by the app — git is the single
source of truth, so the two cannot drift apart. The trade-off is that a **fresh
clone** starts on your global identity again, because the pin lived in the old
clone's `.git/config`; use git's own `includeIf` if you want a rule that survives
re-cloning.

## Install & run

```bash
uv sync
uv run git-assistant
```

(or `uv run python -m git_assistant`)

## First-time setup

1. Start LM Studio, load a model, and start its server (default `127.0.0.1:1234`).
2. Open the tray menu → **Settings…**
   - **Connection & Model:** enter IP/port, click *Test connection*, pick a model,
     and optionally set the context window size.
   - **Repositories:** add one or more git repos.
   - **Identities:** add the identities you commit as; pick one for the active
     repo with the *Commit as* selector above the tabs.
   - **Template / Advanced:** optionally customize the prompt, diff source
     (staged vs. all uncommitted), output reserve, and ignore globs.
3. Stage your changes, pick the **active repo** from the tray menu, and choose
   **Generate commit message**. Edit if needed, then **Copy** or **Commit**.

## Build

Two distributables, both driven by one script (the version is read from
`src/git_assistant/__init__.py`, so the installer cannot drift from the app):

```bash
uv run --extra build python tools/build.py
```

| Target | Output | Notes |
| --- | --- | --- |
| `portable` | `dist/GitAssistant.exe` | Single file, no install, run from anywhere |
| `installer` | `dist/GitAssistant-<version>-setup.exe` | Per-user NSIS installer |

Build just one with `python tools/build.py portable` or `... installer`.
The installer needs NSIS (`winget install NSIS.NSIS`).

The installer is deliberately **per-user** (`%LOCALAPPDATA%\Programs\GitAssistant`,
no admin): the app self-updates by replacing the files it runs from, and a
Program Files install would demand a UAC prompt for every update. It offers a
desktop shortcut, and the uninstaller asks before removing your settings.

### No automatic startup (and why)

The installer does **not** register the app to run at sign-in, even though it is a
tray app and that would be the natural default. Two mechanisms were tried and both
were flagged by Windows Defender's cloud heuristics:

| Release | Mechanism | Detection |
|---|---|---|
| ≤ 0.3.7 | `HKCU\…\CurrentVersion\Run` value | `Behavior:Win32/SuspiciousFileInRunKey.A!cl` |
| 0.3.8 | Shortcut in the Startup folder | `Trojan:Win32/SuspStartupFolderFileTarget.A!cl` |

Two names for one judgement. What is scored is not the *mechanism* but the
*target*: an unsigned, low-prevalence executable in a user-writable directory that
registers itself to run at sign-in. That is the shape of malware persistence, and
this application matches every part of it except the intent.

It did not merely warn. It quarantined the executable and deleted the shortcuts,
the registry entries and the uninstall entry, leaving an install that could neither
start nor be removed — and once the behaviour was observed, the same binary was
blocked **in the build tree**, so the project would not compile without an
antivirus exclusion.

Installing 0.3.9 or later removes autostart left by either earlier release.

**Setting it up yourself** is still supported — the app takes a `--startup` flag,
which brings it up in the tray only (without it, launching opens the window, since
an icon that appears to do nothing reads as broken). Create a shortcut to
`GitAssistant.exe --startup` in `shell:startup`. Be aware you may hit the same
detection; that is precisely why the installer will not do it for you.

### Reputation

The executable carries a version resource (publisher, product, version — see
`tools/win_version_info.py`); shipping without one is unusual for real software and
common in packed malware. The installer no longer shells out to PowerShell to stop
a running copy — it uses `taskkill` on both executable names instead — after the
installer was separately detected as `Trojan:Script/Wacatac.F!ml`, a *script*
detection whose only plausible subject was that call. The cost is precision: it
will also stop a portable build running from elsewhere, which the old path-based
match left alone.

Both are weak evidence, and neither is the fix.

### What was measured, and what did not work

Five behavioural changes were tried against Defender. All of them were eliminated
as the cause:

| Change | Outcome |
|---|---|
| Run key → Startup-folder shortcut | Flagged by the sibling rule |
| Autostart removed entirely | Flagged on run, same rule |
| New hash (version resource) | Clean until it ran, then flagged |
| Self-updater removed from the bundle | Flagged, **identical verdict** |
| PowerShell removed from the installer | Unverified |
| Program Files instead of `%LOCALAPPDATA%` | Unverified |

The verdict never changed. Behavioural tuning is not a productive direction here,
and a build that has just been rebuilt from unchanged source reproduces the same
hash and the same block — so "rebuild and retry" measures nothing.

The observed sequence is always the same: a fresh hash builds and sits readable in
`build/` and `dist/`; installing and running it produces the verdict; the verdict
then propagates back onto the build-tree copies, so the project stops compiling.

### Per-machine install (no-update build only)

The no-update installer installs to **`%ProgramFiles%\GitAssistant`** and requires
elevation, where the ordinary one installs per-user without it. A user-writable
install directory is one of the ingredients these heuristics score, and it only
exists so the self-updater can replace the files it runs from without a UAC prompt
— a build with no updater has no such need.

Consequences: a UAC prompt at install, shortcuts and the uninstall entry become
machine-wide (`HKLM`), and the finish page no longer offers to launch the app,
since it would inherit the installer's elevated token. Per-user settings are
untouched — the app resolves those with `platformdirs` regardless.

Both installers still clean up the *per-user* autostart leftovers from ≤ 0.3.8,
since every release that wrote them was a per-user install.

**Code signing is the fix.** The binary is unsigned and self-updating, so its hash
changes with every release and reputation never accumulates — each build is judged
on its features alone. Azure Trusted Signing is a per-month option that needs no
hardware token. If a build is flagged, report it at
<https://www.microsoft.com/en-us/wdsi/filesubmission>, which retracts the verdict
for every user rather than one machine.

The installed build uses PyInstaller's *onedir* layout rather than onefile, so
startup does not re-extract the whole bundle each launch and the updater can
replace individual files.

The icon is a multi-resolution `.ico` at
`src/git_assistant/resources/icon.ico`. Regenerate it after changing the artwork
in `ui/icon.py`:

```bash
uv run python tools/make_icon.py
```

## Development

```bash
uv run pytest
```

Enable the repository's git hooks once per clone:

```bash
git config core.hooksPath .githooks
```

The `pre-push` hook refuses to push a release tag whose version disagrees with
`__version__` in `src/git_assistant/__init__.py`. The release workflow checks the
same thing, but only after the tag is on the remote - by which point fixing it
means deleting a published tag. Run the check by hand with:

```bash
uv run python tools/check_tag_version.py v0.3.1
```

Config is stored as JSON in your platform's user-config directory
(`platformdirs`, app name `git-assistant`). Settings from the previous
`git-commit-assistant` name are migrated automatically on first launch.
