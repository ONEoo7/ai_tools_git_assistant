# Development

```bash
uv sync
uv run git-assistant
```

## Tests

```bash
uv run --extra dev pytest -n auto
```

`--extra dev` is not optional. The test plugins are an extra, which a plain
`uv sync` leaves out, and without them `-n auto` is an unrecognised argument and
the three-minute timeout in `pyproject.toml` is silently not enforced.

Almost every test builds a real repository and shells out to git, so the wall
clock is process start-up rather than anything this code does — and that divides
cleanly across cores: roughly 150 seconds serially against 50 with `-n auto` on
24 of them. Real-time antivirus scanning changes the arithmetic: where every git
process is scanned as it starts, a spawn costs ten times as much, the suite
takes forty minutes serially and four with `-n auto`, and workers beyond about
ten only queue behind the scanner.

`-n auto` is left out of `addopts` deliberately: a single test being debugged
does not want two dozen workers spawned to run it.

```bash
uv run --extra dev pytest tests/test_theme.py -q
```

These rules hold for every test, in `tests/conftest.py`:

- **The user config directory is never the real one.** Every store is redirected
  to a directory the test owns, so forgetting to patch one writes somewhere
  harmless rather than into your settings.
- **A modal nobody arranged an answer for fails the test that opened it.** An
  unanswered `QMessageBox` does not hang — Windows returns a default after about
  thirty seconds, and the test passes, thirty seconds slower, having tested
  nothing.
- **The clipboard is the test's own.** The desktop's is shared, and a copy waits
  five seconds for whichever process copied last to let go of it — another
  worker, mid-test. A test that checks what was copied reads
  `QGuiApplication.clipboard().text()`.
- **Git's own config is read once a session.** The safe.directory list and the
  global identity are the same answer for every test that has not pointed git
  elsewhere, so they are asked once. A test that sets `GIT_CONFIG_GLOBAL`, or
  stands in for `subprocess.run`, gets git itself.
- **Repositories are made without git's sample hooks**, through
  `GIT_TEMPLATE_DIR`. They were half the files a run leaves behind, and pytest
  deletes the oldest run's folder at the end of each run, one file at a time.

Four helpers are worth knowing about. `git_init(path, config=...)` and
`committed(path, {"f.txt": "one\n"}, config=...)` make a repository, and one
with a commit, by copying what was made the first time they were asked for it:
a git process costs more than everything else a typical test does. A module
whose tests share a bigger history builds it once in a module-scoped fixture and
copies it with `shutil.copytree` for each test — see `test_history_pane.py`.
`settings_with(...)` builds settings whose repo-scoped half is written to the
user tier and hands back the bound view — which is what production hands every
consumer. `user_tier(...)` sets those values for settings that already exist. A
`Bound` resolves its rules **once**, at bind time, so a test that changes the
file afterwards has to rebind.

## Git hooks

```bash
git config core.hooksPath .githooks
```

The `pre-push` hook refuses to push a release tag whose version disagrees with
`__version__`. The release workflow checks the same thing, but only after the
tag is on the remote — by which point fixing it means deleting a published tag.
By hand:

```bash
uv run python tools/check_tag_version.py v0.3.1
```

## Where things live

| | |
|---|---|
| `config.py` | What belongs to the person: the account, the workspace, the selections |
| `repo_config.py` | What belongs to a repository, the three tiers, and the bound view a run reads |
| `jsonc.py` | JSON with comments, for the files people edit |
| `git_ops.py` | Every git command. Nothing else shells out |
| `starter_files.py` | README, .gitignore, .gitattributes and LICENSE for a new or existing repository. The templates ship in `resources/starter_templates.json`, refreshed by `tools/update_starter_templates.py` |
| `commit_generator.py`, `estimate.py` | Writing a message, and pricing it first |
| `agents/` | The four audits |
| `review/` | Rules, profiles, languages, the reviewer |
| `mcp/` | The stdio server and its tools |
| `ui/` | One module per tab, plus the shared bar, cards and theme |
| `tracing/` | Langfuse, optional and never fatal |

Settings are JSON in your platform's user-config directory (`platformdirs`, app
name `git-assistant`). Files written by earlier names and layouts are migrated
on first launch — see [settings](settings.md).

## Style

Comments explain *why*, not what. A comment that restates the line above it is
noise; a comment saying which of two plausible designs was chosen and what went
wrong with the other one is the thing you will want in six months.

Tests are named as sentences about behaviour, not about functions. When a test
exists because something specific went wrong, its docstring says what.
