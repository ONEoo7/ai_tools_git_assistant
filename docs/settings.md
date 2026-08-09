# Settings files

Three files, and one question decides which a value belongs in:

> **Does it change what a run does?**

Yes — the diff mode, a branch pattern, how stale is stale — and it is a
**setting**. It lives in the shared schema, so a project can ship its answer and
a person can override it.

No — which audits are ticked, which report is on screen, which repository is
active — and it is a **selection**. It changes what is on screen and nothing
else, so it belongs to the person looking at the screen.

```
<config>/static_user_settings.json          yours: the account, the workspace, the selections
<config>/user_settings.json                 your answer for every repository
<repo>/.git_assistant/repo_settings.json    the project's answer, checked in
<config>/custom/<repo>/custom_repo_settings.json   your answer for one repository
```

The last three hold **exactly the same keys**. `static_user_settings.json` holds
none of them. Both invariants are enforced by tests, so they cannot drift back.

`<config>` is `%LOCALAPPDATA%\git-assistant` on Windows.

## Exactly one is in force

Not a blend. An earlier design merged them per key, so a repository file setting
only `fetch.depth` inherited the rest — which is convenient right up to the
moment somebody has to answer "where did this value come from", and the answer
is three files and a precedence rule.

**Active Settings**, in the bar above the tabs, says which one applies to the
active repository: *User*, *Repo* or *Custom*. Nobody having chosen, a repository
with a file of its own uses it. When a repository carries settings that nothing
is reading, the bar says so — a checked-in file being ignored is invisible
otherwise.

Underneath the file in force are the built-in constants, which are not a tier:
they fill in what the file does not say, so a hand-trimmed file still works.

### Editing them

The **Repositories & Settings** tab shows the file in a coloured editor.
**Editing:** chooses which of the three to *look at* — it does not change which
one applies. That separation exists because opening a colleague's checked-in
settings to read them should not put your repository onto them.

**Save** never edits the file you opened. A change goes to your **Custom**
settings for that repository, so an edit cannot rewrite a file a team shares,
and that repository switches onto it. When a Custom file already exists, you are
shown the difference and asked first.

**Compare & merge…** puts any two files side by side, key by key, and lets you
take either.

## JSONC

The files are JSON with comments, because they are meant to be opened and
changed by hand and plain JSON gives no way to say what a key is for.

```jsonc
{
  // These settings are overridden by repo_settings.json

  // Where a new branch's name comes from.
  "branch": {
    // This repository's own naming convention. {user} and {name} are filled in.
    "pattern": "{name}",
    ...
```

- `//` and `/* … */` are ignored.
- A comma left before a closing brace is tolerated — it is what you get when you
  delete the last entry of a list. A comma with *nothing* in front of it is still
  an error.
- A broken file reports the line and column of the file you have open, because
  comments are blanked rather than deleted.
- **Your own comments survive a save.** The editor writes back what you typed
  rather than re-rendering the data.

Every key carries a comment, and a test refuses a key that does not.

## What is in which

**`static_user_settings.json`** — the account (provider, models, temperatures,
Azure API version, MCP registration), the workspace (repositories, scan roots,
watched roots), the selections (which settings tier per repository, which audits
are ticked, which branch pattern, which template each repository uses, the
theme), and the **default commit-message template**, which is here because it is
the one thing a project's own settings cannot replace.

**The shared schema** — `branch`, `fetch`, `audit`, `commit`, `prompt`,
`review`, `model`, `tracing`. Between them: naming conventions, fetch depth,
audit rules, commit length limits and ignore globs, the named prompt templates,
review profiles, the context window and per-provider endpoints, and where a
trace goes.

`prompt.templates` is the one place the pick-one rule bends: a repository that
ships templates replaces yours whichever tier is in force. See
[commit messages](commit-messages.md#templates) for why.

**Never in any of them:** API keys and Langfuse keys. Those are in the Windows
Credential Manager — see [providers](providers.md#api-keys).

## Restoring

*Advanced → Shipped settings* holds a factory copy of the account settings and a
checksum over it. **Check again** says whether it is intact; **Restore shipped
settings** puts them back, keeping your repository list — a factory reset that
threw that away is one nobody presses.

If the factory copy itself has been tampered with, there is nothing left that
knows what it should have said, and the tab says to reinstall rather than
pretending otherwise.

The per-tier **Reset to defaults** button does the same job for one settings
file, from the built-in constants.
