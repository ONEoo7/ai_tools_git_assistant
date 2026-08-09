# Committer identities

The **Commit as** dropdown above the tabs shows the `user.email` the **active
repository** will stamp on its next commit, and lets you switch it. The
**Identities** tab manages the list.

- Selecting one runs the equivalent of `git config --local user.name/user.email`
  **in that repository**, so it outranks your global config and applies to
  commits made from any tool, not just this one.
- The dropdown always reflects git's own answer, resolved the way git resolves
  it — repo config, `includeIf` conditional includes, then the global fallback.
  The note beside it says whether the identity is *set for this repository* or
  *inherited from global git config*.
- An identity git reports but you have not saved is shown as
  `someone@example.com (not saved)` rather than being quietly swapped for a
  saved one.

The saved identity is also where `{user}` in a
[branch pattern](branches-and-tags.md) comes from.

## Signing keys

An identity can carry an optional `user.signingkey`. It is written when you
select that identity and **cleared** when you select one without a key —
otherwise a commit ends up authored by one identity and signed by another's key,
which every forge reports as *Unverified*.

If `commit.gpgsign` is on but no key resolves, the readout says **signing key
missing** rather than letting the next commit fail at the git level.

## Pushing is not the same as committing

`user.email` decides how a commit is **labelled**. It has no effect on **which
credential pushes**. You can commit as your personal identity and still push
with work credentials; git will not complain, and the forge attributes the
commit to whoever owns the email.

The right-hand readout says what will actually authenticate:

| Readout | Meaning |
|---|---|
| `push: github.com as ONEoo7` | A username is pinned, in the remote URL or `credential.…username` |
| `push: github.com` *(amber)* | **One credential serves every account on this host** — the identity you pick does not change who you push as |
| `push: SSH to github.com (default key)` *(amber)* | Your default SSH key, whichever identity is selected |
| `push: SSH to github-personal (key from SSH config)` | A host alias, so the key is chosen per alias |

The amber cases have a tooltip with the fix — `credential.<host>.useHttpPath`
for HTTPS, or a `Host` alias with its own `IdentityFile` for SSH.

This is read from configuration only. Asking the credential helper would give a
firmer answer and can pop an authentication prompt, which is not acceptable
while merely redrawing a window.

## Where they live

`committer_identities.json`, in the config folder — its own file, so it can be
moved between machines without dragging along a server address or a list of
local repository paths.

```json
{
  "version": 1,
  "identities": [
    { "name": "Work", "email": "me@work.example", "signingkey": "" },
    { "name": "Personal", "email": "me@personal.example", "signingkey": "ABC123" }
  ]
}
```

On first run the file is created from your **global git identity**, so the list
is never empty for no reason. It is written even when git has nothing to offer,
so an intentionally emptied list is not refilled on the next start.

**Export / Import** writes and merges that file. Import *merges*: it never
deletes identities that exist only on this machine, and an email already present
is left alone rather than overwritten, so an old export cannot silently rename
the identity you are using.

Nothing about "which identity is current" is stored by the application — git is
the single source of truth, so the two cannot drift apart. The trade-off is that
a **fresh clone** starts on your global identity again, because the pin lived in
the old clone's `.git/config`. Use git's own `includeIf` for a rule that survives
re-cloning.
