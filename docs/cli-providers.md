# Using agent CLIs as inference providers — analysis

> **Built.** `claude` and `agy` ship as **experimental** providers; see
> `src/git_assistant/agent_cli/`. Verified end to end through the real client:
> `claude` 118 input tokens in 7.4 s, `agy` 17,112 in 14.5 s. Copilot is not
> shipped, for the reason below. The analysis is kept as written — it is why the
> flags in `client.py` are what they are.
>
> **Both are capped to one call at a time** (`Provider.max_parallel`). Four
> concurrent calls measured 8.0 s against ~7 s for a single one — the seconds
> are process start-up, not queueing, so concurrency buys nothing and costs four
> runtimes. Running that test also found a real bug: one `Popen` handle shared
> across threads, where a sibling's `finally` cleared it mid-read. The client now
> tracks a set of processes under a lock, so Cancel stops all of them.

**Question:** can Git Assistant drive the `claude`, `copilot` and `antigravity`
CLIs as backends, listed alongside LM Studio and the HTTP providers?

**Short answer:** `claude` yes, and it is genuinely good. `agy` (Antigravity)
yes, with one serious caveat about prompt overhead. `copilot` **no** — its
non-interactive mode does not work on this machine at all, and even if it did it
is missing the two things the app needs most.

Everything in the *Measured* section was run on this machine on 2026-08-06, not
recalled. All three CLIs were installed and logged in at the time.

## Summary

| | `claude` 2.1.72 | `agy` 1.1.10 | `copilot` 0.0.354 |
|---|---|---|---|
| non-interactive works | yes | yes | **no — exits 1, silently** |
| JSON output | yes | yes | not offered |
| token counts | yes | yes | no |
| dollar cost | **yes** | no | no |
| separate system prompt | **yes** | no | no |
| tools can be disabled | **`--tools ""`** | no (`--sandbox` only) | needs `--allow-all-tools` |
| list models offline | no | **`agy models`** | fixed list in `--help` |
| prompt overhead per call | **112 tok** | **~17,100 tok** | unknown |
| startup overhead | ~5 s | ~6 s | — |
| verdict | **build it** | **build it, with a warning** | **leave it out** |

---

## What the app actually needs

`git_assistant.llm.ChatClient` is four methods, and the whole feature turns on
how well a CLI answers them:

| | What it means for a CLI |
|---|---|
| `chat(model, system, user, max_tokens, temperature)` | one non-interactive invocation, **a separate system prompt**, and the reply on stdout |
| `context_length_for(model)` | **the hard one** — the diff splitter needs the window *before* it sends anything |
| `list_models()` | which models this login can reach |
| `ping()` | is it installed and logged in |

Plus two things the app relies on everywhere: **token counts** for the usage
pane and Langfuse, and **cancellation** mid-run.

---

## Measured

### `claude` — Claude Code 2.1.72, at `~/.local/bin/claude.exe`

One call, `--tools "" --output-format json --no-session-persistence
--setting-sources ""`:

| | Default system prompt | With `--system-prompt` |
|---|---|---|
| input tokens | 3 | 112 |
| **cache-creation tokens** | **3,408** | **0** |
| output tokens | 4 | 4 |
| **cost for "Reply with exactly: OK"** | **$0.012849** | **$0.000396** |

**`--system-prompt` replaces Claude Code's own harness prompt rather than adding
to it — a 32× cost difference on a trivial call.** Anything built here must pass
it, and must never fall back to the default prompt silently.

Wall clock: **7.65 s** for that trivial prompt (2.26 s of it inference). So
roughly **5 s of process startup per call**.

The JSON is complete and stable:

```json
{"type":"result","subtype":"success","is_error":false,"result":"OK",
 "total_cost_usd":0.000396,
 "usage":{"input_tokens":112,"output_tokens":4,"cache_creation_input_tokens":0},
 "modelUsage":{"claude-sonnet-4-6":{"contextWindow":200000,"maxOutputTokens":32000}}}
```

Everything the contract wants is there — including **real cost in dollars**,
which no HTTP provider in this app reports today.

Flags that matter: `-p`, `--output-format json`, `--system-prompt`, `--model`,
`--tools ""`, `--disable-slash-commands`, `--setting-sources ""`,
`--no-session-persistence`, `--max-budget-usd`, `--json-schema`.

### `agy` — Antigravity CLI 1.1.10, at `~\AppData\Local\agy\bin\agy.exe`

Not `antigravity` — the binary is `agy`, and `antigravity` is not on `PATH` at
all. Detection must look for the right name.

One call, `-p --output-format json --model gemini-3.6-flash-low
--disable-slash-commands`:

```json
{"conversation_id":"d4ff0b2c-…","status":"SUCCESS","response":"OK\n",
 "duration_seconds":2.276,"num_turns":1,
 "usage":{"input_tokens":17087,"output_tokens":5,"thinking_tokens":0,
          "cache_read_tokens":0,"total_tokens":17092}}
```

Wall clock **8.33 s** (2.28 s of it inference), so ~6 s of startup.

**An alias is not a fixed model.** `claude` has no way to list models — `claude
models` is a *prompt*, not a subcommand — so its aliases (`sonnet`, `opus`,
`haiku`) are what it is asked for. Measured on this machine, the same
`--model sonnet` was served once by `claude-sonnet-4-6` and once by
`claude-haiku-4-5-20251001`: Claude Code routes per call. So the app records
every call under the model that *actually answered* — otherwise a month's
spending files entirely under "sonnet" — and the model list labels an alias
`last: <model>` rather than claiming an equivalence that is not there.

**`agy models` lists the models offline, instantly** — the best answer to
`list_models()` of the three, and it costs nothing:

```
gemini-3.6-flash-high/medium/low   gemini-3.5-flash-high/medium/low
gemini-3.1-pro-high/low            claude-sonnet-4-6
claude-opus-4-6-thinking           gpt-oss-120b-medium
```

**The caveat, and it is a big one: 17,087 input tokens to say "OK".** There is
no `--system-prompt` to replace the harness prompt the way `claude` allows, so
that overhead is paid on *every single call*. A 40-file code review would spend
roughly **680,000 tokens on overhead alone**, before a line of anyone's diff.
It also eats the context window: whatever `context_length_for` reports, ~17 k of
it is gone before the prompt starts.

Also useful: `--json-schema` (structured output), `--effort low|medium|high`,
`--print-timeout` (default 5 m). Tools cannot be turned off — only `--sandbox`
(terminal restrictions) and `--dangerously-skip-permissions` exist.

### `copilot` — GitHub Copilot CLI 0.0.354, at `%APPDATA%\npm\copilot.cmd`

Installed and **now authenticated**, and it still does not work non-interactively.

| invocation | exit | stdout | stderr |
|---|---|---|---|
| `--version` | 0 | 26 bytes | 0 |
| `-p "…"` | **1** | **0** | **0** |
| `-p "…" --allow-all-tools` | **1** | **0** | **0** |
| `-p "…" --allow-all-tools --no-color` | **1** | **0** | **0** |

Tried from Git Bash and from PowerShell, with and without `stdin` redirected to
`/dev/null`, with and without every flag combination above. Its own session log
stops two lines in:

```
[INFO] Starting Copilot CLI: 0.0.354
[INFO] Node.js version: v24.13.0
```

…and nothing further. **It exits 1 having written nothing anywhere.** I could
not determine the cause — it may want a real console, or it may be a Node 24
incompatibility. What matters for this decision is that it is not merely
awkward: an application cannot depend on a backend that fails silently and gives
it nothing to report to the user.

Even if that were fixed, `--help` shows the two disqualifying gaps:

1. **No `--system-prompt`.** The app builds every prompt as system + user; here
   they would have to be flattened into one blob, and every prompt in the
   codebase would need a second form.
2. **No JSON output.** No token counts, no cost, no reliable "where does the
   reply start" — the usage pane and Langfuse would both stay empty for it.

And `--allow-all-tools` is *required* for non-interactive mode, inverting the
default posture from `claude`'s. `--deny-tool` takes precedence, so it could be
fenced, but starting from "everything allowed" is the wrong end.

Models, if it ever runs: `claude-sonnet-4.5`, `claude-sonnet-4`,
`claude-haiku-4.5`, `gpt-5`.

---

## The limitations, in the order they would bite

**1. Five to six seconds of process startup, per call.** The review path is *one
call per file* and fans out `parallel_calls` (default 4) at a time. A 40-file
review is 40 process launches — call it two extra minutes of pure startup.
Commit messages over a large diff are map-reduce, so the same applies per chunk.
This is the single biggest practical difference from an HTTP provider, and it is
not fixable from this side.

**1b. `agy` pays ~17,100 tokens of harness prompt on every call**, with no way
to replace it. That is the same shape of problem `claude` has *and solves* with
`--system-prompt` (3,408 → 112). For a one-call commit message it is an
irrelevance; for a 40-file review it is ~680,000 tokens of overhead. If `agy` is
offered, the estimate window must include it, or the figure the user approves
will be wrong by an order of magnitude.

**2. These are agents, not completion endpoints.** They can read files, run
commands and edit code. `claude --tools ""` disables that outright and I verified
the call still works. **`agy` has no equivalent** — only `--sandbox` and
`--dangerously-skip-permissions` — so it would be run without its permission
prompts being answerable, in a repository, by a background thread. Copilot needs
`--allow-all-tools` to run non-interactively at all. Given this app runs inside
your repositories and its prompts contain your diffs, that is a different risk
class from an HTTP POST. Whatever flags end up fencing this need a test that
fails loudly if they are ever dropped, and `agy` needs a decision about which
directory it is launched in — an empty temporary one, not the repository, is the
safe answer.

**3. `context_length_for` cannot be answered before the first call.** The whole
diff strategy — single-shot or map-reduce, how many chunks, what to truncate —
depends on knowing the window up front. `claude` reports `contextWindow: 200000`
in the *result* of a call. The workaround is a small shipped table of model →
window, which is a maintenance burden that goes stale silently.

**4. No temperature.** Neither CLI exposes it. The per-model temperature setting
would be inert for these providers, and should say so the way the Anthropic
provider already does.

**5. Authentication is out of band, and the app cannot help.** No API key field,
nothing to store in the Credential Manager. The app can detect "installed but
not logged in" — which is exactly the state `copilot` is in here — and say so,
and that is all it can do.

**6. `claude` refuses to run inside another Claude Code session.** The
`CLAUDECODE` environment variable triggers it; this blocked my first attempt.
The app must strip `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` from the child
environment, or it breaks for exactly the users most likely to want it.

**7. Terms of service — your call, not mine.** A Claude subscription and a
Copilot seat are licensed for use through their own clients. Driving one as a
generic backend for a third-party application is, at best, not clearly
sanctioned, and the vendors can both detect it and revoke access. I am flagging
it as a constraint on the decision, not making the decision.

**8. Windows launcher shapes.** `claude` is a real `.exe`; `copilot` is
`copilot.cmd` / `copilot.ps1` from npm. Verified: Python 3.13's `subprocess` can
spawn the `.cmd` directly, no shell needed. `.ps1` cannot be spawned directly
and must not be the one we pick.

**9. Cancellation.** The app's Cancel must kill a process *tree* — these CLIs
spawn children. On Windows that means a job object or `taskkill /T`, not
`Popen.kill()`.

---

## The PATH problem you flagged — worse than it looks

`irm ... | iex` and `winget install` write `HKCU\Environment\Path`, then
broadcast `WM_SETTINGCHANGE`. **A process already running never sees it.**
`os.environ["PATH"]` in the app is a snapshot taken at launch, and no amount of
re-reading `os.environ` will refresh it.

Two things are needed, and both are cheap:

- **Re-read the registry after an install**: `HKCU\Environment` and
  `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment`, expand
  `REG_EXPAND_SZ`, join, and use *that* for the lookup — not the inherited
  environment.
- **Probe the known install locations directly** as a fallback, because they are
  deterministic: `~\.local\bin\claude.exe`, `%APPDATA%\npm\copilot.cmd`. This is
  what makes detection work even when the registry read fails.

The same refreshed value has to go into the child's environment, or the CLI's
own subprocesses will not find what they need either.

---

## What building it would take

| Piece | Size |
|---|---|
| `cli_client.py` — the four methods over `subprocess`, JSON parse, usage, cancellation | ~250 lines |
| `providers.py` entries with a `cli` kind, and a per-CLI argument recipe | ~60 lines |
| `cli_detect.py` — PATH-from-registry, known locations, version and auth probe | ~120 lines |
| Install button + progress, in the idiom of the existing LM Studio setup | ~150 lines |
| Model → context-window table, shipped | small, and a standing liability |
| Tests: no real process spawned, argument recipes pinned, tool-disabling flags pinned | ~200 lines |

The LM Studio setup flow (`lmstudio_setup.py`, and its button in Connection &
Model) is a close precedent for the install half: it already downloads, runs an
installer, reports progress and re-probes afterwards.

---

## Recommendation

**Do `claude`.** It answers all four contract methods, reports tokens *and* real
cost, takes a genuine system prompt, and can be stripped of tools. The per-call
latency is the price, and for commit messages — one call, or a handful — it is
an acceptable one.

**Do `agy`, second, and make its overhead visible.** It works, its JSON is clean,
and `agy models` is the best model listing of the three. But ~17,100 tokens per
call with no way to shrink it makes it a fine choice for commit messages and a
poor one for reviews, and the app should say so rather than let someone discover
it on a bill. Nothing goes into it that does not also warn.

**Leave `copilot` out.** Not a judgement about the product — its `-p` mode
returns nothing and exits 1 on this machine, and that is the end of the enquiry.
Should it start working, it still lacks a system prompt and any token
accounting, which would make it the only provider whose usage pane is
permanently blank. Revisit if both change.

**Suggested order:** detection and the provider list first (it is useful on its
own, and honest about what is missing), then the `claude` client, then `agy` on
the same client with a different argument recipe, then the install button.

### Three questions worth settling before any code

1. **Latency on reviews.** A 40-file review through a CLI is minutes of startup
   alone, and through `agy` it is also ~680 k tokens of overhead. Do we cap CLI
   providers to the commit path, warn in the estimate window, or let it be slow?
2. **The context-window table.** Ship one and accept it going stale, or make
   CLI providers always use the conservative `context_window` from Settings and
   never auto-detect?
3. **Where `agy` is launched.** It is workspace-aware and its tools cannot be
   disabled. Running it in an empty temporary directory keeps it away from the
   repository; running it in the repository would let it read files the prompt
   never mentioned. The first is the safe default and costs nothing.
