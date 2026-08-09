# Large diffs

A `git diff` can easily exceed a model's context window. Nothing here ever hard
-fails on size; the preview reports which strategy ran and the token counts
involved.

## 1. Noise filtering

Lock files, binaries, minified assets and any glob in `commit.ignore_globs` are
dropped before anything is counted. A compact `--stat` is always included, so
the model still sees the overall shape of a change even where it cannot see
every line of it.

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
