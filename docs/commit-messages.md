# Commit messages

The **Generate Commit Message** tab writes a Conventional Commits message for
what is staged, shows it beside the files it was written from, and lets you edit
it before committing.

## Transparency

The preview shows the message side by side with the staged files. Select a file
and its diff appears with **anything omitted from the prompt marked in red** — a
truncated hunk, or a file dropped by the noise filter. It is therefore obvious
what the model did *not* see, which matters most when a diff overflows the
budget. See [large diffs](large-diffs.md).

**View LLM calls** shows the exact prompt sent and the exact reply, per call.
For a map-reduce run that is every chunk summary as well as the synthesis.

## Templates

The prompt is a template, and templates are named. Each repository picks one on
the Generate tab; the rest use the default.

Three things, in three places, because they answer three questions.

**The default** is `default_template` in `static_user_settings.json`. It is
always offered and **a project cannot replace it** — that is the point of it. A
repository whose prompt turns out to be wrong should still leave you something
to fall back to without editing a file the whole team shares. The prompt is
written out in full, so it can be read and changed in the file as well as in
the Template tab.

**The named templates** are `prompt.templates` in the shared schema, because a
template decides what gets sent: a project whose commits follow a house style
can check the prompt that produces it into `repo_settings.json`. `user_settings.json`
ships one of its own — **Short**, a subject line and nothing else — so the file
arrives saying what an entry looks like.

A repository that ships templates **replaces** yours rather than adding to
them:

| | offered |
|---|---|
| nothing configured | Default, Short |
| your templates only | Default, yours |
| repository ships some | Default, **theirs** — yours are not offered |

That holds whichever settings are in force. A project that checked a prompt in
did so to be used, and choosing *User settings* to change a fetch depth is not
a decision about that. `"templates": []` in a repository is a project that has
not decided, not one that says none.

**Which one each repository uses** is `repo_templates` in
`static_user_settings.json` — a mapping from repository to template name. A
selection: it names one of the templates on offer and decides nothing about
what any of them say. A mapping to a template that is gone falls back to the
default rather than erroring.

The **Template** tab edits *your* library — new, duplicate, rename, delete,
import and export as JSON — and the Default entry, which writes to your own
settings. It never shows a repository's, because it is the editor for yours;
what a run is *offered* is the different question above.

Placeholders: `{branch}`, `{diffstat}`, `{diff}`.

## How long a message may be

Two conventions nobody wrote down but everybody follows: **50 characters** for
the subject as a soft target, **72** as the hard cap — past it, `git log
--oneline` and every web interface cut the subject without saying so, and the
last words are simply gone. The body has no such convention, but a model asked
for "a body" will write nine paragraphs about a two-line change, so there is a
cap there too, defaulting to **1000 characters**.

A limit told to a model is a request, not a guarantee, so both halves matter:

- The rules are **appended to whichever template is in use**, so a template
  saved last year is held to the limits set today, and changing the numbers
  actually changes something.
- The message is **measured under the editor**, live, as you type over it —
  `Subject 47/72 · body 312/1000` — turning amber with the reason when it runs
  over.

Nothing is ever shortened to fit. Cutting a subject at 72 characters produces
exactly the mangled subject the limit exists to prevent. Set any of the three to
`0` in **Advanced** to turn that rule off.

## When it comes back too long anyway

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
back. That matters because the default temperature is low, and re-sending an
identical prompt at a low temperature fairly reliably produces the identical
answer.

Declining keeps the message: a run you choose not to redo still produced
something. It is offered once, not in a loop — a model that ignored the
instruction once will ignore it again. Retries are billed under their own
feature in the usage pane, so "what did the retries cost me" has an answer.

## Previous runs

Every generated message is recorded per repository, newest first, with the
calls that produced it. Pin one to keep it past the limit. Select several to
delete them together; *Open* is for one at a time.

The limit is `commit.history_limit` — `0` keeps every one.
