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

## Build a standalone .exe

Produces a single `dist/GitAssistant.exe` (no console window) that can be
double-clicked or pinned to the taskbar with the app's own icon:

```bash
uv run --extra build pyinstaller git-assistant.spec
```

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

Config is stored as JSON in your platform's user-config directory
(`platformdirs`, app name `git-assistant`). Settings from the previous
`git-commit-assistant` name are migrated automatically on first launch.
