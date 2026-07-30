"""Which optional subsystems this particular build was assembled with.

There are two installers. The ordinary one can update itself; the "no-update"
one cannot, because the code that would do it is not in the bundle at all.

That distinction is deliberate rather than a configuration toggle. The
self-updater downloads an executable and runs it, and an unsigned binary that
can do that is, behaviourally, indistinguishable from a dropper -- which is how
this application kept being quarantined. A build for an environment that will
not tolerate that has to *not contain the capability*, not merely decline to
use it: a switch someone can flip back is not an answer to "can this program
download and execute something".

The flag is therefore derived from whether the package is present, never from a
constant someone edits. The capability and the answer to "do we have the
capability" cannot drift apart, because they are the same fact.
"""

from __future__ import annotations

from importlib.util import find_spec


def _has_updater() -> bool:
    # find_spec raises rather than returning None for some import problems, and
    # a build that cannot answer the question is one without a usable updater.
    try:
        return find_spec("git_assistant.updating") is not None
    except (ImportError, ValueError):
        return False


#: True when the self-updater is part of this build.
UPDATES_SUPPORTED = _has_updater()

#: Shown wherever the UI would otherwise offer an update control.
NO_UPDATES_NOTE = (
    "This build has no self-updater. Install a new version over it to upgrade."
)
