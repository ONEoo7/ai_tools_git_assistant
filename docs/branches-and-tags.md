# Branches and tags

## Naming a new branch

Two cards, because they are not two values of one setting.

**New Branch** creates exactly what you type. Slashes are kept, so
`feature/login` is a branch called `feature/login`. This is the default,
because most branches are not part of anybody's convention.

**New Branch from patterns** fills in a convention:

```
dev/rem/{user}/{name}
test/rem/{user}/{name}
```

`{name}` is what you type. `{user}` is who you are, resolved in this order:

1. typed into the **User** field on the card,
2. `branch.user` in the settings in force,
3. the saved [identity](identities.md) matching the email git is configured with
   here,
4. what git says `user.name` is.

The identity wins over git's own name because it is the one you curated and the
one the *Commit as* row is showing — a commit stamped with that email should not
put a second spelling of the same person into a branch name.

The patterns are a **setting**, `branch.patterns`, so a project can ship its
own conventions in `repo_settings.json`; a repository's own `branch.pattern` is
offered first. Which one you have selected is *your* choice, remembered per
repository and kept out of the shared file. See [settings](settings.md).

## What git will actually accept

The full name is shown before the branch is created — `Will create:
dev/rem/Stefan-Ghitescu/login-form` — and what is shown is what git takes.
Typed text is made safe first: spaces and punctuation become hyphens, and the
rules that are about a slash-separated *piece* rather than the whole name are
applied too.

| typed | created |
|---|---|
| `JIRA-412 fix login` | `JIRA-412-fix-login` |
| `index.lock` | `index` — a piece may not end with `.lock` |
| `a/.hidden` | `a/hidden` — nor begin with a dot |
| `release.` | `release` |
| `a..b` | `a.b` — `..` is a ref git refuses |
| `///` | nothing. The prefix alone is not the branch you asked for |

That last one matters with a pattern: a name that slugs away to nothing would
leave `dev/rem/<who>`, which is a real branch name and the wrong one — and one
that would then block every `dev/rem/<who>/…` for ever, for the reason below.

### Directory conflicts

Git keeps refs as paths, so `dev` is a file and `dev/rem/x` needs `dev` to be a
directory. They cannot both exist:

```
fatal: cannot lock ref 'refs/heads/dev/rem/x': 'refs/heads/dev' exists
```

That message is perfectly clear and arrives at the worst possible moment —
after the name was offered and the button was pressed. So the conflict is
checked from the branch list already on screen, while the name is being typed:

> Cannot create: 'dev' is already a branch, so nothing can be created under
> 'dev/'. Rename or delete it first.

*Create branch* is disabled while that stands. The other direction gets its own
wording, because it is a different problem: `dev/rem` when `dev/rem/x` exists is
already a folder of branches and cannot be one itself.

One thing worth knowing on Windows: git treats `dev/rem/Stefan` and
`dev/rem/stefan` as different, and NTFS does not. Two spellings of `{user}` fold
into whichever directory exists first.

## Branch actions

**Switch** checks out the selected branch — git refuses with uncommitted
changes, and says so. **Push** publishes it, setting the upstream on the first
push when `branch.push_sets_upstream` is on. **Delete** removes it; a branch
whose commits are on no other branch is refused by git, and deleting it anyway
has to be confirmed.

**Fetch** uses this repository's own settings: `fetch.shallow` and `fetch.depth`
for how much history to ask for, `fetch.prune` to drop local copies of branches
that are gone from the remote, `fetch.tags` to bring tags along.

## Tags

The **New version** group proposes the next tag from the tags already there:
major, minor, patch, or a name you type. Give it a message and it is annotated;
leave it empty and it is lightweight. Existing tags are listed newest first —
click one to push it.
