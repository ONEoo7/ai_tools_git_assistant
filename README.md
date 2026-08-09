# Git Assistant

A system-tray Git assistant for Windows. It writes commit messages, reviews
staged files, audits repositories and answers to an MCP client — using whichever
model you point it at, local or hosted.

Python 3.13, PyQt6, `uv`. Nothing leaves the machine unless you configure a
hosted provider, and no key is ever written to a settings file.

```bash
uv sync
uv run git-assistant
```

The tray icon opens the window. Its right-click menu is three items — **Git
Assistant**, **About**, **Exit** — because everything else belongs in the window.

Above the tabs, on every one of them, sits the bar that says what the *active
repository* is configured with: **Commit as** (the identity the next commit will
carry) and **Active Settings** (which of the three settings files is in force).
Top right is the theme picker: follow the system, light, dark, or pink.

---

## What it does, tab by tab

### Generate Commit Message

| | |
|---|---|
| **Generate** | Writes a Conventional Commits message for what is staged. Prices the run and asks before spending anything. |
| **Copy** / **Commit** / **Push** | Take the message, commit with it, publish it. The message is editable first. |
| **Prompt template** | Which of your named templates this repository is described with. |
| **AI Provider** | Which backend answers, switchable without leaving the tab. |
| **Staged files** | Every file in the prompt. Select one to see its diff, with anything the model *did not* see marked in red. |
| **View LLM calls** | The exact prompt sent and the exact reply, per call — including every call of a map-reduce run. |
| **Previous runs** | Messages already generated for this repository, newest first, pinnable, reopenable. |

More: [commit messages](docs/commit-messages.md) · [large diffs](docs/large-diffs.md)

### Branches & Tags

| | |
|---|---|
| **New Branch** | The name exactly as typed. Slashes are kept, so `feature/login` is one branch. |
| **New Branch from patterns** | A naming convention — `dev/rem/{user}/{name}` and friends — with the full name shown before it is created. `{user}` comes from your saved identities. |
| **Create branch** | Creates it from the current commit and switches to it. Refuses names git would, and says which existing branch is in the way. |
| **Switch / Push / Delete** | On the selected branch. A delete git refuses has to be confirmed before it is forced. |
| **Fetch** | With this repository's own depth, prune and tag settings. |
| **New version** | Proposes the next tag — major, minor, patch or custom — from the tags already there. |
| **Create tag / Push tag / Delete tag** | Annotated when you give it a message, lightweight when you do not. |

More: [branches and tags](docs/branches-and-tags.md)

### Audit

Four read-only audits of the selected repository. Git measures; the provider
writes the prose, and any figure it invents is rejected before the report shows.

| | |
|---|---|
| **Size** | Where the `.git` bytes went, what is reclaimable without rewriting history, and which paths dominate every version ever committed. |
| **Configuration** | What the repository carries with it: LFS coverage, line endings decided in `.gitattributes` rather than per machine, and a dozen more. |
| **Consistency** | Branches nobody has touched for months, and which of them are safe to delete. |
| **Metrics** | Lines in everything the repository tracks, by file type. `.gitignore` is respected and binaries are skipped. |
| **Run** | Runs everything ticked, side by side, never exceeding the provider's parallel limit. |
| **Compare** | What changed between two recorded runs — which checks were fixed or regressed, which measurements moved. |
| **Previous runs** | Every run, stamped with the commit it describes. |

More: [audits](docs/audits.md)

### Code Review

| | |
|---|---|
| **Review** | Checks the marked files against the rules for their language. One call per file, run in parallel. |
| **Rules profile** | Which rules apply to which language at which version. Remembered per repository. |
| **Mark all / Mark none** | Everything staged starts marked; unmark what you do not want checked. |
| **A window before it runs** | Every marked file with its language, its version and the rules that will be checked, beside the token estimate. |
| **Add / Remove language**, **Rename** | Edit a profile: a row per language, a version dropdown, a checkbox per rule. |
| **Share with the repository** | Writes the profile into the working tree so whoever clones it is reviewed against the same standard. |
| **Import / Export spreadsheet** | `.xlsx` in and out — the header is looked for rather than assumed. |
| **Import / Export JSON** | Move tables between machines. An import never overwrites a table you already have. |
| **Open rules folder** | The per-language rule files, which are yours to edit. |
| **Previous reviews** | Every run per repository. A stored review still reads after its rules are edited or deleted. |

More: [code review](docs/code-review.md)

### Connection & Model

| | |
|---|---|
| **Providers** | LM Studio, Claude, OpenAI, Azure AI Foundry, Litellm Proxy, Ollama, Lemonade Server, and two agent CLIs. The form reshapes around the one selected. |
| **API key** | Stored in the Windows Credential Manager, never in a settings file. The field says whether one is stored, never what it is. |
| **Test connection & list models** | One round trip, and the model list from it. |
| **Model / Temperature** | Kept per provider and per model. |
| **Context window / Parallel requests** | What a run may use, with the resulting split shown live. |
| **Set up LM Studio for me…** | Installs LM Studio, turns on its server, downloads a model and configures it. Names every step first and skips what is already done. |
| **Usage** | Lifetime totals per provider, per model and per feature, and the last few hundred calls behind them. |

More: [providers](docs/providers.md)

### Repositories & Settings

| | |
|---|---|
| **Add repo…** | One repository. |
| **Add folder (scan for repos)…** | Every repository under a folder, submodules nested under their parent. |
| **Auto-watch** | Tick a scanned folder and newly cloned repositories are added on their own. |
| **Rescan / Remove selected** | Keep the list honest. |
| **Fix blocked repos…** | Repositories git refuses to read, and why. |
| **Editing** | Which of the three settings files to show — not which one applies. |
| **Create / Save / Reload / Reset / Remove** | Edit the file in place, with the JSON coloured. Saving your changes forks them to your own copy rather than editing a file a team shares. |
| **Compare & merge…** | Any two settings files side by side, key by key, taking either. |

More: [settings files](docs/settings.md)

### Identities

| | |
|---|---|
| **Add / Remove selected** | The identities you commit as: name, email, optional signing key. |
| **Import… / Export…** | The list is its own file, made to be carried between machines. |
| **Commit as** (above the tabs) | Switches the active repository's identity, and says whether it is set here or inherited. |

More: [identities](docs/identities.md)

### MCP Server

Offers the repositories, audits and commit-message generation to an MCP client
over stdio.

| | |
|---|---|
| **Test server** | Starts it and reports what it answered. |
| **Register / Remove** | Claude Desktop, Claude Code, VS Code's GitHub Copilot, Antigravity. Each is a merge that leaves every other setting alone. |
| **Copy command / Copy JSON** | For a client that is not on the list. |
| **Allow write operations** | Off by default. The flag lives in the registered command, not in a file the server re-reads. |

More: [MCP server](docs/mcp.md), including the tool list.

### Template

| | |
|---|---|
| **New / Duplicate / Rename / Delete** | Your named prompt templates. Each repository picks one. |
| **Default** | The prompt that is always offered, whatever a repository ships. |
| **Reset to default text** | Back to the prompt this build ships with. |
| **Import… / Export…** | JSON, one template or many. |

More: [commit messages](docs/commit-messages.md#templates)

### Advanced

| | |
|---|---|
| **Diff source** | Staged changes, or everything uncommitted. |
| **Output reserve** | How much of the context window is kept for the answer. |
| **Subject / body limits** | Reported, not enforced. |
| **Ignore globs** | Files that never reach the model. |
| **Langfuse** | Send every call to your own instance, prompt and reply included. Off by default. |
| **Shipped settings** | Check the factory copy against its checksum, and restore from it. |
| **Update source** | A readout: where updates come from and why they are or are not available. |

More: [tracing](docs/tracing.md) · [settings files](docs/settings.md)

---

## Documentation

| | |
|---|---|
| [Install and update](docs/install.md) | Running it, first-time setup, how updates work |
| [Providers](docs/providers.md) | Every backend, API keys, agent CLIs, temperature, usage |
| [Commit messages](docs/commit-messages.md) | Templates, length rules, what happens when one comes back too long |
| [Large diffs](docs/large-diffs.md) | What happens when a diff does not fit the context window |
| [Branches and tags](docs/branches-and-tags.md) | Naming patterns, what git will accept, fetching, versioning |
| [Audits](docs/audits.md) | What each audit measures and how runs are compared |
| [Code review](docs/code-review.md) | Rules, profiles, versions, sharing a standard |
| [Identities](docs/identities.md) | Committer identities, signing keys, and why pushing is not committing |
| [Settings files](docs/settings.md) | The three files, what is in each, and how to edit them |
| [MCP server](docs/mcp.md) | Tools, registration, and the write gate |
| [Tracing](docs/tracing.md) | Langfuse |
| [Building](docs/build.md) | Distributables, why there is no autostart, antivirus |
| [Development](docs/development.md) | Working on it |

## Licence

See [LICENSE](LICENSE).
