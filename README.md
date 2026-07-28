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
   Set the window in Settings → Advanced to match the context length the model is
   loaded with in LM Studio, or leave it at `0` to auto-detect the model's maximum.
   The Connection tab shows the resulting split live.
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

## Install & run

```bash
uv sync
uv run git-assistant
```

(or `uv run python -m git_assistant`)

## First-time setup

1. Start LM Studio, load a model, and start its server (default `127.0.0.1:1234`).
2. Open the tray menu → **Settings…**
   - **Connection & Model:** enter IP/port, click *Test connection*, pick a model.
   - **Repositories:** add one or more git repos.
   - **Template / Advanced:** optionally customize the prompt, diff source
     (staged vs. all uncommitted), token budget, and ignore globs.
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
desktop shortcut and a **Start with Windows** option (on by default - it is a
tray app), and the uninstaller asks before removing your settings.

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
