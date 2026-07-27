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
