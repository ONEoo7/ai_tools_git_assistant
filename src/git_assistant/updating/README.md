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

**The build.** `update_url.txt` sits beside `root.json` in this directory and
is **committed**, so every build carries an address by construction. It has to
be the build rather than an environment variable, because **an installed
desktop application never sees a shell's environment** — it is launched from
the Start Menu and inherits the *user* environment, so a variable exported in a
terminal reached a checkout, where updating is refused, and reached nothing
else.

It is committed rather than generated because the generated version failed
open. The address came only from a `UPDATE_URL` repository variable, and an
unset variable produced a build with no updater at all — silent, and from the
outside indistinguishable from the feature being broken. That shipped twice.
`vars.UPDATE_URL` still overrides the committed value for a fork or a staging
pipeline, and a build with neither is now a build failure.

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
  "channel": "stable",
  "check_interval_minutes": 240
}
```

It exists so an installation can be pointed elsewhere when its usual service is
unreachable. A build whose only address is compiled in cannot recover when that
address dies.

### How often it checks

At startup, then on a timer, so an application left running for days notices a
release without anyone opening a window. `check_interval_minutes` sets the
period; the default is 240 and the floor is 1.

The floor is a floor rather than a fixed value because "check every ten
seconds" is a reasonable thing to want while testing a deployment and an
unreasonable thing to leave switched on. Every check is a full metadata walk —
the root chain, timestamp, snapshot, the delegated role, then the pointer — so
ten seconds is roughly 8,600 of them per machine per day, essentially all of
which find nothing. Releases are minutes-to-days apart; checking faster than
they are published buys latency nobody perceives at a cost the server pays.

A bad value is clamped, never rejected: wanting faster checks is a preference,
and refusing the whole file over it would turn that into "updating is off".

An automatic check that finds a version it has already offered this session
says nothing more — the window's readout still updates, it just stops
interrupting. The tray's **Check for updates…** always answers, though, even
for a version already declined; a button that does nothing because of a
decision made an hour ago is indistinguishable from a broken one.

There is no push channel, deliberately. The edge is a static file server with
no application behind it, and clients are anonymous by construction — the
install id is generated locally and never sent, so a staged rollout is
evaluated on the client and there is no per-client response to forge. Having
the service notify clients would mean it tracking who runs what, which is a
larger change than the latency is worth.

### Three more things about the file

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
