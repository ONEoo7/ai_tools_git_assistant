# Large diffs

A `git diff` can easily exceed a model's context window. Nothing here ever hard
-fails on size; the preview reports which strategy ran and the token counts
involved.

## 1. Noise filtering

Lock files, binaries, minified assets and any glob in `commit.ignore_globs` are
dropped before anything is counted. A compact `--stat` is always included, so
the model still sees the overall shape of a change even where it cannot see
every line of it.

### Keeping one of them anyway

The globs are always obeyed. Nothing is ever un-ignored automatically, because
nothing here can tell the difference between a file that is noise and a file
that is the point of the change — only you can.

That matters most for documents. Git will hand back a readable text diff for a
PDF when a `textconv` diff driver is configured for it, the usual one being
`pdftotext`:

```gitattributes
*.pdf diff=pdf
```
```gitconfig
[diff "pdf"]
    textconv = pdftotext
```

Where that is set up, a commit adding a standards document produces a thousand
lines of real prose. `uv.lock` produces a thousand lines of readable text too,
and its first pages say nothing a commit message needs. Inferring anything from
the file's type would get one of those two wrong.

So: **right-click the file in the staged files list and choose _Do not ignore_.**
It then contributes its **first `commit.include_lines` lines** — 200 by default,
which for a document is the title, the abstract and the table of contents. The
rest is replaced by a line saying how much was left behind, so the file does not
read as stopping mid-sentence. Right-click again to go back to ignoring it.

The choice is remembered **per repository**, so you decide about a given file
once. It is stored in `commit_includes` in `static_user_settings.json` — a list
of paths, not a rule: `commit.ignore_globs` is untouched, and a repository that
ships its own ignore list still gets it obeyed exactly.

Two things it will not do. A PDF with no `textconv` driver is a
`Binary files ... differ` stub with no text to take, so the menu says so instead
of offering a choice that would change nothing. And `commit.include_lines` of
`0` means no cap — the whole file — since un-ignoring it is already the opt-in.

## 2. Budget

One **context window** (input + output combined) split by the **safety margin**:

```
output      = window × margin
diff budget = window − output
```

Because output is carved out of the same window, input + output can never exceed
it. Set the window on *Connection & Model* to match the context length the model
is actually loaded with, or leave it at `0` to auto-detect the model's maximum
where the provider reports one. The same tab shows the resulting split live.

When several audits run at once, the window is divided between them — a run of
three does not each plan against the whole thing.

## 3. Single shot

If the whole prompt fits the budget, it is one call. Most changes are.

## 4. Map-reduce

Otherwise the diff is split per file, then per hunk, then hard-truncated if a
single hunk is still too big.

- **Map** — each budget-sized chunk is summarised.
- **Reduce** — the notes are condensed if they overflow in turn.
- **Synthesis** — a final message is written from the notes.

The map and reduce calls are independent, so they run **in parallel** — four at
a time by default, `model.parallel_calls`, and never more than the provider's own
limit allows. `1` is sequential.

## Before it spends anything

Pressing *Generate*, *Review* or *Run* prices the run first: how many calls, and
roughly how many tokens in and out. The estimate names the strategy it is
pricing, so a map-reduce run says so before it starts rather than afterwards.

A review prices per file and says which files will not fit whole. An audit
without narration sends nothing and says that instead of a number.
