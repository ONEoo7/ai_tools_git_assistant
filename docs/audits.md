# Audits

Four read-only audits of the selected repository. Nothing is deleted, moved or
checked out by the application.

**Git measures; the model narrates.** Every figure in a report comes from git,
and any number the model invents is rejected before the report is shown. Turn
narration off and the report is the measurements alone, with no request made at
all.

Each audit is a card carrying the settings only *it* reads: fast mode belongs to
the size audit, the stale-branch rules to the consistency audit, and neither is
shown under the other. Tick as many as you want and press **Run** — they run
side by side, never more at once than the provider allows, with the context
window divided between them.

Ticking says what runs. Clicking a card says whose report is on screen, because
a run of three leaves three reports.

## Size

Where the `.git` directory's bytes went.

- Leftover garbage separated from real history, so "what can I reclaim without
  rewriting anything" has an answer.
- The paths that dominate every version ever committed — not the working tree,
  the whole history.
- **Fast mode** skips the per-file history breakdown, which is the slow part on
  a large repository. The audit reads every object in history otherwise, and can
  take minutes.
- `audit.large_file_mb` is the size at which a file is worth flagging.

## Configuration

What the repository carries with it, rather than what one machine happens to
have configured:

- Git LFS coverage — whether the large and binary files are actually routed
  through it.
- Whether line endings are decided in `.gitattributes` rather than per machine.
- A dozen more checks of the same kind.

## Consistency

Two questions that only get asked once a project has been running a while.

### What can I delete?

Branches accumulate, and `git branch` shows no difference between one merged a
year ago and one holding work somebody put down.

- **Stale and merged** — the commits are already on the default branch, so
  deleting the branch loses nothing. These get a `git branch -d` block to read
  and run. `-d`, never `-D`: that is git's own refusal to lose work, and it is
  why the block can be offered at all.
- **Stale and unmerged** — listed with their age and upstream, and **no command
  is offered**, because the branch is the only copy. A branch whose upstream
  shows `(gone)` was probably squash-merged; git cannot tell that from abandoned
  work, and neither can this.
- **Protected** — what the rules spared, so the rules can be seen working. The
  default branch is protected whether or not it is listed.

The rules are on the card: how many months counts as stale (6), whether unmerged
branches may ever be proposed (no), whether unpushed work is kept (yes), and the
names and globs to spare (`main, master, develop, trunk, release/*, hotfix/*`).

### What is it pinned to?

The submodule half reads **the selected repository only**. It lists every
submodule declared, the commit each is pinned to, and where the working tree has
wandered off that commit.

- **The version is the commit the parent pins**, read from its `HEAD` tree and
  described by tags — not whatever is checked out locally. A working tree that
  has drifted off its pin is reported as *drift*: it is invisible until somebody
  else clones and gets something different.
- **A submodule is identified by its remote URL, not its path.** One dependency
  vendored at both `vendor/lib` and `third_party/lib` is one dependency, and if
  the two paths are pinned to different commits the audit says so.
  `git@host:owner/repo.git` and `https://host/owner/repo` are the same place.

## Metrics

Lines in everything the repository tracks, grouped by file type: how much of it
is code, how much is configuration, how much is generated. It reads
`git ls-files`, so `.gitignore` is respected and binaries are skipped.

## Previous runs

Every run is recorded per repository and stamped with the commit it describes.
**Compare** two of them and the report says which checks were fixed or
regressed and which measurements moved — which is the only way to answer
whether anything actually improved.

Each new run is compared with the last automatically. Pin a run to keep it past
`audit.history_limit`; `0` keeps every one.
