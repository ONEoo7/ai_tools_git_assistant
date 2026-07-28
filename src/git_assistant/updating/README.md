# The trusted root

`root.json` in this directory is the TUF root this application trusts. It is
shipped inside the build rather than fetched, because fetching it would open a
trust-on-first-use window in which a network attacker could hand the
application a root of their own.

It declares **root at 3-of-5** and **targets at 2-of-3**, so a release is only
accepted if enough independent keys agree.

## What is here now: a development root

Produced by `scripts/ceremony.py --dev` in the distribution platform. The keys
are real and persist, so publishing and updating work end to end — unlike a
throwaway root, this one can actually sign releases.

It is not a production root, and the difference is custody rather than
cryptography:

- all five root keys were generated on one machine, in one sitting, by one
  operator;
- the keystores are unlocked by a known development password, with no key file;
- `offline.kdbx` is sitting on the same disk as everything else.

A production ceremony splits custody so no single person or machine ever holds
a threshold, uses a composite master key with the key file on separate media,
and keeps the offline store off the service host entirely. `ceremony.py`
enforces those rules when `ENV=production`; it cannot enforce the human part.

**Before shipping to anyone but yourself**, run the production ceremony and
replace this file.

## When updating is offered at all

Three conditions, all required, all in `UpdateConfig.unavailable_reason`:

1. There is an update URL — the repository root, not its `metadata` directory.
2. This is a **packaged build** (`sys.frozen`), not a source checkout.
3. `dist_client` is importable, so there is something to verify with.

### Where the URL comes from

Two sources, each with one owner.

**The build.** `update_url.txt` is written at package time from the
`UPDATE_URL` repository variable and bundled beside `root.json`. This is what
makes a fresh install work with no configuration at all. It has to be the build
rather than an environment variable, because **an installed desktop application
never sees a shell's environment** — it is launched from the Start Menu and
inherits the *user* environment, so a variable exported in a terminal reached a
checkout, where updating is refused, and reached nothing else.

**`update.json`**, in the platform config directory beside `settings.json`
(`%LOCALAPPDATA%\git-assistant\update.json` on Windows). Reach it from
**Settings → Advanced → Update service → Edit…**, which creates it from an
inert template if it is not there and opens it. That row also shows the address
currently in use and where it came from — "it is checking the wrong server" and
"it is not checking at all" are otherwise indistinguishable, since both show up
as the menu item simply being absent.

```json
{
  "url": "https://updates.example",
  "channel": "stable"
}
```

It exists so an installation can be pointed elsewhere when its usual service is
unreachable. A build whose only address is compiled in cannot recover when that
address dies.

Three things about it:

- **The application never rewrites it.** It is a separate file rather than a
  key in `settings.json` precisely because the application rewrites that one on
  every edit, and a file the application rewrites is a poor place to keep the
  thing that decides where its code comes from. The single write is creating
  the template on request, whose `url` is empty — so it overrides nothing, and
  an existing file is never touched. A hand-edited address cannot be clobbered,
  least of all at the moment it is needed.
- **A broken override does not fall back.** If `url` is missing a scheme or the
  JSON does not parse, updating is disabled and the reason says so. Falling
  back to the packaged address would hide the mistake behind the failure the
  user was trying to escape.
- **Editing it cannot change what is trusted.** The keys a release must be
  signed by are fixed by `root.json` above, so pointing this at a hostile
  server produces verification failures, not bad code. That is also why plain
  `http` is accepted: TUF signs the metadata and pins the target hashes, so a
  loopback deployment is a normal way to run this.

The second is the one worth explaining. Self-update replaces the files it is
running from; in a packaged build those files *are* the build, and in a `git
clone` they are a working tree with uncommitted work in it. There is
deliberately no environment variable to force it on in a checkout, because a
switch like that is one somebody eventually leaves on.

To exercise the updater during development, run a packaged build — or drive
`dist_client` directly, which is the layer that resolves the channel pointer
and checks every signature.

## Rotating it

Replacing this file changes what the *next* build trusts. Installations already
in the field keep trusting the root they shipped with and reach the new one by
following the root chain the repository serves — which is why root rotation is
signed by the old root as well as the new. Do not treat this file as a switch
that retires the previous root.

## Why 3-of-5 and not 3-of-3

The spare keys are the point. Issue exactly `threshold` keys and every one of
them is load-bearing forever: lose a single root key under 3-of-3 and root can
never be re-signed, so once it expires every installed client is permanently
unable to accept anything — and recovery would have to be signed by the keys
that are gone.
