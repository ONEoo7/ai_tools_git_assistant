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
- **Code review** tab: check the files you have staged against a table of rules
  kept in a spreadsheet (see below).
- **LLM usage**: every completion is counted, per provider and per model, and
  shown beside the connection settings (see below).
- **Asked before it spends**: pressing Generate, Review or Run shows what the run
  will send - how many calls, and roughly how many tokens - before the first
  request goes out.
- **Langfuse tracing** (optional, off by default): send every call to your own
  Langfuse instance, prompt and reply included, to keep a searchable record of
  what was actually asked (see below).

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

## AI providers

Pick one in the **Providers** list on the left of *Connection & Model*; the same
choice appears as **AI Provider** in the Generate tab. The form reshapes around it
— only the settings a provider actually has are shown.

| Provider | Needs | Notes |
|---|---|---|
| **LM Studio** | IP + port | Local, no key. The default. |
| **Claude** | API key | Anthropic Messages API via the official SDK |
| **OpenAI** | API key | Fixed endpoint |
| **Azure AI Foundry** | API key + endpoint | Endpoint is per-resource; `api-version` is configurable |
| **Litellm Proxy** | endpoint (+ optional key) | Defaults to `localhost:4000`; connects unauthenticated if your proxy has no auth |
| **Ollama** | endpoint | Defaults to `localhost:11434`; no key — Ollama has no auth of its own |
| **Lemonade Server** | endpoint | Defaults to `localhost:13305/api/v1`; no key — Lemonade has no auth of its own |

The self-hosted providers all default to localhost, so the usual setup needs no
typing. Put a remote one behind auth via the **Litellm Proxy** entry, which has a
key field.

**Model and endpoint are stored per provider**, so switching doesn't carry one
backend's model name into another's request.

### API keys

Keys go in the **Windows Credential Manager**, never in `settings.json` — that file
is plain text, the app rewrites it constantly, and a key there would survive in
backups and file-history copies after rotation. Entries are named
`git-assistant:<provider>` and are visible in *Credential Manager → Windows
Credentials*.

The key field is an input, not a display: it shows whether a key is stored, never
the value, and clears itself after saving. **Remove** deletes the entry.

Claude and OpenAI/Azure differ on the wire, which is why Claude is its own client:
Anthropic takes the system prompt as a top-level parameter rather than a message,
requires `max_tokens`, returns `content` as a list of typed blocks, and **rejects
`temperature`** on current models. The OpenAI-compatible providers share one client
that differs only by base URL, auth header, and query parameters.

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

## Code review against your own rules

The **Code Review** tab checks staged files against a table of rules - the
spreadsheet a team already keeps its standard in, with a `ruleID` column and a
`ruleDetails` column.

- **Languages are detected per file** - C, C++, C#, CSS, HTML, Java, JavaScript,
  Python, Rust, TypeScript, shell and PowerShell - so one review can span a
  polyglot repository and each file is judged by the rules that apply to it. A
  file no language claims is listed as unreviewable rather than checked against
  somebody else's rules.
- **Rules ship with the app**, six to ten per language, each carrying the
  language versions it is true for: `nullptr` is not a C++98 rule and f-strings
  are not a Python 2 one. The version is read from what the repository already
  declares (`pyproject.toml`, `Cargo.toml`, `tsconfig.json`, a `.csproj`,
  `pom.xml`, `CMakeLists.txt`, a doctype, a shebang) and can be set by hand
  where nothing declares one.
- **A profile** ties it together: which rules apply to which language at which
  version. The *Profiles* tab lists every profile you have; open one and it has
  a row per language, a version dropdown, and a checkbox per rule. Which profile
  a review actually runs against is the *Rules profile* dropdown beside the
  repository - remembered per repository, and marked in bold in the list, so
  reading one profile is never mistaken for selecting it. The shipped defaults
  are read-only: editing them keeps the change in a copy of your own.
  **Share with the repository** writes it to
  `.git-assistant/code-review-profile.json` - your own tables in full, the
  shipped ones by name - so whoever clones the repository is reviewed against
  the same standard. That is the only file this application ever writes into a
  working tree, and it takes an explicit press.
- **Import** a `.xlsx` under *Rules*. The header is looked for rather than
  assumed, so a title row above it and columns nobody here cares about are both
  fine; `Rule ID`, `rule_id` and `RULEID` all read the same. Tables can be
  exported back to `.xlsx`, or exported and imported as JSON to move them
  between machines (an import never overwrites a table you already have).
- **Several named tables**, each repository picking the one it is reviewed
  against. The choice is remembered per repository.
- **Mark the files to review.** Everything staged starts marked; unmark what you
  do not want checked. Files dropped by the noise filter are listed as
  unreviewable rather than silently left out.
- **A window before it runs** lists every marked file with its language, its
  version and the rules that will be checked, beside the token estimate. The
  language column is editable - `.h` is C or C++ and only your repository knows
  which - and the answer is remembered so it is asked once.
- **One call per file**, run `parallel_calls` at a time, each carrying the rules,
  that file's diff and the file as it will be after the change. When they do not
  all fit, the content is dropped before the diff, and the diff before the rules
  - and whatever was cut is said on the file's row, above the findings, and in
  the prompt itself, so a partial review cannot be mistaken for a clean one.
- **View LLM Calls**, the same pane as the commit tab: the exact prompt sent for
  each file and exactly what came back. A reply that cannot be read as findings
  is asked again once, then kept verbatim as a visible finding - never as a
  clean file.
- **Previous reviews** records every run per repository beside the settings file
  (`review_runs/`, newest 20 kept, pinnable), so you can reopen one and see
  whether a repository improved. The findings carry their rule text with them, so
  a stored review still reads after the table is edited or deleted.

## What each provider has been asked to do

The **Connection & Model** tab carries a usage pane on the right: lifetime
totals per provider, per model and per *feature* -- a commit message, a code
review, a repository audit -- and the last few hundred calls behind them:
provider, model, what for, when, input tokens, output tokens, total. A model
every tab shares would otherwise answer "how much has this cost" with a single
figure, which is not the question anyone is asking.

The count is taken where the answer comes back, inside the provider clients, so
a run started from the tray, from a tab or through the MCP server all count the
same. The numbers are the provider's own wherever it reports them (every
OpenAI-shaped API and Anthropic do); when a proxy reports none, this build
counts the tokens itself and marks those rows with `~` rather than presenting an
estimate as measured.

The file is `llm_usage.json`, beside `settings.json`. The recent-calls list is
capped at 500 rows; the totals are never pruned, because "how much has this
cost" must not change when the log is trimmed.

## How long a commit message may be

Two conventions nobody wrote down but everybody follows: **50 characters** for
the subject line as a soft target, **72** as the hard cap — past it, `git log
--oneline` and every web interface cut the subject without saying they did, so
the last words are simply gone. The body has no such convention, but a model
asked for "a body" will write nine paragraphs about a two-line change, so there
is a cap there too, defaulting to **1000 characters** (500–1000 is what teams
that bother with a rule tend to pick).

Both halves matter, because a limit told to a model is a request, not a
guarantee:

- The rules are **appended to whichever template is in use** — the default one,
  a custom one, or a per-repository one — so a template saved last year is held
  to the limits set today, and changing the numbers in Settings actually changes
  something.
- The message is **measured under the editor**, live, as you type over it:
  `Subject 47/72 - body 312/1000`, turning amber with the reason when it runs
  over.

Nothing is ever shortened to fit. Cutting a subject at 72 characters would
produce exactly the mangled subject the limit exists to prevent. Set any of the
three to 0 in **Advanced** to turn that rule off entirely.

### When it comes back too long anyway

You are asked whether to pay for a shorter one, priced first, in the same window
every other spend goes through:

```
1 call(s), about 1,240 tokens in and 512 out (1,752 in total).

- One call: the prompt that wrote this message, with the length it overran
  quoted back to it.
- The diff was summarised in 15 call(s) the first time. None of that is
  repeated: the summaries are already in this prompt.
- The same prompt with a different instruction, not the same prompt again --
  so the answer changes even at a low temperature.
```

**A map-reduce run re-sends only its synthesis.** The notes are already in that
prompt, so asking again costs one call rather than the fifteen the first attempt
took, and no chunk is read or summarised twice.

**The retry differs by prompt, not by sampling** — the exact overrun is quoted
back to the model. That matters because the default temperature here is low, and
re-sending an identical prompt at a low temperature fairly reliably produces the
identical answer.

Declining keeps the message: a run you choose not to redo still produced
something. It is offered once, not in a loop — a model that ignored the
instruction once will ignore it again, and asking repeatedly spends money on
that. Retries are billed under their own feature in **LLM usage**, so "what did
the retries cost me" has an answer.

## Temperature

Per provider **and** per model, under **Connection & Model** — what is careful
for one set of weights is mute for another. The default is 0.2, because a commit
message describes a diff rather than inventing anything; leaving it at the
default stores nothing, so the default can change later and reach you.

Anthropic's current models reject the parameter outright, and the field says so
rather than pretending to set it.

## Keeping the prompts (Langfuse)

Two records of every model call already exist and neither survives the day:
**View LLM Calls** holds one run until the window closes, and `llm_usage.json`
holds tokens without a word of what was sent. So *"why did last Tuesday's review
miss that"* cannot be answered -- the prompt is gone.

**Settings → Advanced → Langfuse** sends every call to a
[Langfuse](https://langfuse.com) instance you run: one trace per run, one
generation per call, named for what that call was doing. Off until you fill in a
host, a public key and a secret key -- both keys go to the Windows Credential
Manager, never to `settings.json`.

Every prompt here is built from your staged diffs, so **Send prompts and
responses** is a real choice: unticked, a trace still carries the model, the
timings, the token counts and any error, and no source code. The repository's
*name* travels; its path does not.

Traces are filed under the account running the application, tagged
`git-assistant`, and carry the feature, the repository and the provider as
filterable metadata.

Tracing can never cost you a generation. A server that is down, misconfigured,
or absent from the build produces a missing trace and a line in the tab saying
why -- never a lost commit message. See
[`src/git_assistant/tracing/README.md`](src/git_assistant/tracing/README.md).

## Before a run spends anything

Generating a message, reviewing files and narrating an audit all cost tokens,
and how many is not obvious: a diff that has quietly grown past the context
window becomes fifteen calls instead of one, and a review is one call per marked
file. So each of those buttons shows what the run will send, and waits:

```
4 call(s), about 21,879 tokens in and 2,048 out (23,927 in total).

LM Studio - qwen3.5-4b

- One call per marked file: 4 file(s), 4 at a time.
- Each carries the 5 rule(s), the file's diff and the file itself.
- Room reserved for the findings: up to 512 tokens per file.
- 3 file(s) will not fit whole and are cut to the budget.
```

Tokens only, no cost: the price list belongs to your provider, and a made-up
figure would be worse than none. The estimate never contacts the provider, so
the dialog appears the moment the button is pressed.

An audit is the honest exception: it measures the repository first, and what
each section of the report is handed does not exist until that scan has run. It
reports the calls and the output exactly, and says the input is not knowable yet
rather than multiplying the per-call cap into a ceiling nobody reaches.

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
     and optionally set the context window size. With LM Studio selected there is
     also *Set up LM Studio for me...*, which installs LM Studio via winget,
     turns on developer mode and the background service, downloads
     `lmstudio-community/Qwen3.5-4B-GGUF@Q8_0` (~5 GB) and configures it for
     32,768 tokens with thinking off. It names every step before doing anything
     and skips whatever is already in place. It reads and writes LM Studio's own
     config files, so a future LM Studio may move them and break a step.
   - **Repositories:** add one or more git repos. *Add folder* scans for repos
     and picks up their submodules too, listing each one nested under the
     repository it belongs to — the same nesting the repository selector in the
     *Generate Commit Message* and *Tags* tabs shows.
   - **Agents:** audits of the selected repository, read-only. *Repository size
     audit* reports where the `.git` bytes went, what is reclaimable without a
     history rewrite, and which paths dominate every version ever committed;
     *Repository configuration audit* checks Git LFS coverage, whether line
     endings are decided in `.gitattributes` rather than per machine, and a
     dozen other things. Git measures, the configured provider writes the
     prose — and any figure it invents is rejected before the report shows it.
     Every run is recorded (beside `settings.json`, marked with the commit it
     describes) and each new one is compared with the last, so *Previous runs*
     answers whether anything actually improved.
   - **MCP Server:** offer the repositories, audits and commit-message
     generation to an MCP client over stdio. *Test server* starts it and reports
     what it answered; the buttons register it with Claude Desktop (a merge into
     `claude_desktop_config.json` that leaves every other setting alone) or with
     Claude Code (`claude mcp add`, user scope by default). Read-only unless
     *Allow write operations* is ticked **and** the server is registered again —
     the flag lives in the registered command, not in a file it re-reads.
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
