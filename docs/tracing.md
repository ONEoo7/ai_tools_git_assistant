# Tracing (Langfuse)

Two records of every model call already exist and neither survives the day:
**View LLM calls** holds one run until the window closes, and the usage store
holds token counts without a word of what was sent. So *"why did last Tuesday's
review miss that"* cannot be answered — the prompt is gone.

**Advanced → Langfuse** sends every call to a [Langfuse](https://langfuse.com)
instance you run: one trace per run, one generation per call, named for what
that call was doing.

Off until you fill in a host, a public key and a secret key. **Both keys go to
the Windows Credential Manager**, never to a settings file.

## What travels

Every prompt here is built from your staged diffs, so **Send prompts and
responses** is a real choice. Unticked, a trace still carries the model, the
timings, the token counts and any error — and no source code.

The repository's **name** travels; its path does not.

Traces are filed under the account running the application, tagged
`git-assistant`, and carry the feature, the repository and the provider as
filterable metadata.

## It can never cost you a generation

A server that is down, misconfigured, or absent from the build produces a
missing trace and a line in the tab saying why — never a lost commit message.

**Test connection** on that tab uses what is on screen rather than what was last
saved, so the status keeps up with a field being filled in.

## Where the setting lives

`tracing` in the shared schema, so a team that traces one project and not
another can say so in that project's `repo_settings.json`. The Advanced tab
edits your own answer for every repository. See [settings](settings.md).

Implementation notes: `src/git_assistant/tracing/README.md`.
