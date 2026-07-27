# The trusted root

`root.json` in this directory is the TUF root this application trusts. It is
shipped inside the build rather than fetched, because fetching it would open a
trust-on-first-use window in which a network attacker could hand the
application a root of their own.

## The file currently here is a placeholder

It came from a throwaway local repository used to verify the update path end to
end. Its signing keys existed only in memory and are gone, so it is useless to
an attacker — but it is equally useless to you: nothing you publish for real
will verify against it.

**Replace it before any release**, with `repo/metadata/root.json` from the
distribution service — the one produced by the key ceremony.

The release workflow bundles this file, so a build cut without replacing it
will ship a root that trusts nothing. That fails closed, which is the right
direction, but it fails silently from the user's point of view: update checks
simply start reporting errors.

## Rotating it

Replacing this file changes what the *next* build trusts. Installations already
in the field keep trusting the root they shipped with, and reach the new one by
following the root chain the repository serves — which is why root rotation is
signed by the old root as well as the new one. Do not treat this file as a
switch that retires the previous root.
