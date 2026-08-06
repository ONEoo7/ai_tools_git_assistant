# Langfuse tracing

Sends every call to the model to a [Langfuse](https://langfuse.com) instance you
run: the prompt, the reply, the model, the timing and the token counts, grouped
into one trace per run.

**Off until you turn it on**, under **Settings → Advanced → Langfuse**.

## Why this exists

Two records of a model call already exist, and neither survives the day.

- **View LLM Calls** holds one run, in memory, until the window closes.
- **LLM usage** (`llm_usage.json`) holds tokens per provider, model and feature
  — forever, but without a word of what was sent.

So *"why did last Tuesday's review of that file miss the obvious thing"* cannot
be answered at all: the prompt that produced it no longer exists anywhere.
Langfuse is the durable, searchable version of the first record, which is why
the answer was to send traces to it rather than grow a third store here.

## What is sent

One trace per run — a commit generation, a code review, an audit, or a call from
an MCP client — named after the feature. Inside it, one **generation** per call
to the model, named after what that call was doing (`summarizing a chunk`,
`reviewing a file`, `writing the message`).

Each generation carries:

| | |
|---|---|
| model | as it was asked for |
| input | the system and user messages, as a chat |
| output | the reply |
| usage | **the provider's own token counts, or none at all** |
| level | `ERROR` with the message, when the call failed |

And on the trace:

| Field | Value | Filterable in Langfuse |
|---|---|---|
| `userId` | the account running the application (`getpass.getuser()`) | yes |
| tags | `git-assistant` | yes |
| metadata | `feature`, `repository`, `provider` | yes — `langfuse.trace.metadata.*` |
| `release` | this build's version | yes |
| `environment` | the field in Advanced | yes |
| `service.name` | `git-assistant` | **no** — see below |

`service.name` is set through `OTEL_SERVICE_NAME`, with `setdefault`: someone
running this under a collector that already names the service meant it. Langfuse
puts resource attributes under `metadata.resourceAttributes` and documents them
as not queryable, so it is there for anything downstream that speaks OTEL, and
the **tag** carries the same answer where it can be searched on.

The user, the tags and the trace metadata are applied through
`propagate_attributes` around *every* span rather than once around the run.
That context lives in an OpenTelemetry contextvar, which does not cross a thread
pool — and a review fans out over one, so a single application at the top would
reach the first span and none of the rest.

**Not the repository's path.** A path is `D:\work\<client>\...`, and who you
work for is not something tracing a code review needs to know.

**Not the git committer, for the user.** That is a property of a repository and
can differ per project, and it is an email address — more of a person than a
record of a token count needs. The login name the process runs under is the
answer to "who ran this". When the platform will not say, the field is left
absent: a trace attributed to `unknown` looks like a real account.

**Not an estimated token count.** `usage` marks its own estimate as one because
a bill is not settled against a guess; a number arriving in Langfuse unmarked
would be read as measured. When the provider reports nothing, the generation
carries no usage rather than a plausible one.

### Withholding the prompts

Every prompt this application builds is made from your staged diffs and file
contents. Unticking **Send prompts and responses** omits `input` and `output`
entirely — *omits*, not blanks: an empty prompt in Langfuse reads as a call made
with an empty prompt, which is a different and more alarming thing. Everything
else still goes, so the traces remain useful as timings and cost.

## Where the keys live

Both in the Windows Credential Manager, as `git-assistant:langfuse` and
`git-assistant:langfuse-public`, beside the provider API keys. Neither is in
`settings.json`, and `tests/test_llm_clients.py` asserts that no settings field
is named like a credential — with no exceptions, so the rule stays checkable.

The public key would have been defensible in a settings file: it is the half of
the Basic auth pair Langfuse's own documentation puts in browser code. Keeping
the pair in one place is worth more than that argument.

The two are handled differently on screen, which is the honest difference
between them: the **public** key is read back and displayed, so nobody has to
remember which one is configured; the **secret** is write-only — the box is an
input for a new key, never a display of the stored one — and is never shown,
logged, or quoted back in an error. `tracer._reason` strips it from any
exception text before that text reaches the screen.

Removing one removes both: half a credential pair is not a configuration.

## What it may not do

**It may not cost you a generation.** A commit message that took forty seconds
must not be lost because an observability server was restarting. So:

- the SDK is imported inside a `try` — a build without it is a working build;
- the client is built on demand, and every call into it is inside
  `except Exception`;
- `TracingClient.chat` returns the model's answer on every path.

The tests in `tests/test_tracing.py` are mostly variations on that one rule.

**And silence may not look like success.** The other half: `tracer.status()` is
this module's account of itself, shown in the tab beside a **Test connection**
button that does one real round trip.

## How it hooks in

One line, in `llm.build_client`. That function is already called once per run —
once per commit generation, once per review, once per audit, once per MCP tool
call — so a client and a run were already the same thing, and the trace boundary
needed no new plumbing. It also means the tray, the tabs and the MCP server are
all traced identically, for the same reason `usage.record` lives inside the
provider clients rather than in the UI.

The run ends when `tracing.close(client)` is called, in a `finally` at those
four sites. A path that misses it still produces a trace — the generations end
on their own — with no span wrapped around them. A worse trace, never a wrong
one.

## Packaging

OpenTelemetry resolves its exporters through entry points, which PyInstaller
does not follow, so all three `.spec` files use `collect_all` for `langfuse` and
`opentelemetry` rather than naming them as hidden imports. The MCP server's own
analysis includes them too: unlike `openpyxl`, it does generate commit messages.
