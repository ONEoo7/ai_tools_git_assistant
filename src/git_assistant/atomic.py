"""Replacing a file whole, even while something else is reading it.

The stores here write a temporary file beside the real one and swap it into place,
so a write cut short leaves the old file rather than half of the new one. On
Windows the swap -- ``os.replace`` onto a file that exists -- fails with a
PermissionError while any other process has the destination open: routinely an
antivirus or the search indexer, reading a file written milliseconds earlier, or a
second copy of this application. It clears in tens of milliseconds, so the answer
is to wait and swap again, not to treat a scanner's timing as a failed save.

Measured on one machine: about 0.75% of writes hit it. Often enough to have shown
up as flaky tests -- and, before the store that keeps generated commit messages
waited, to lose a click's worth of work now and then without saying so.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

#: Sleep, as a module attribute so tests can watch it instead of living it.
sleep = time.sleep

#: Tries in all, and the first wait between them, doubling: a little under a second
#: of waiting before giving up, which a scan's hold on a file is well inside.
REPLACE_ATTEMPTS = 6
REPLACE_BACKOFF = 0.03


def replace_atomically(tmp: Path, destination: Path) -> None:
    """`os.replace`, tried again while something else has the destination open.

    Only a PermissionError is tried again: that is what the hold looks like. Anything
    else -- the disk full, the folder gone -- would say the same the second time.

    Raises:
        OSError: if it never succeeded. The temporary file is removed first --
            leaving one beside the destination every time this loses is how a
            directory fills up with ``*.tmp``.
    """
    delay = REPLACE_BACKOFF
    for remaining in range(REPLACE_ATTEMPTS - 1, -1, -1):
        try:
            os.replace(tmp, destination)
            return
        except PermissionError:
            if not remaining:
                _discard(tmp)
                raise
            sleep(delay)
            delay *= 2
        except OSError:
            _discard(tmp)
            raise


def write_atomically(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` whole or not at all: beside it, then swapped in.

    Raises:
        OSError: if it could not be written; ``path`` is then as it was, and no
            temporary file is left behind.
    """
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
    except OSError:
        _discard(tmp)
        raise
    replace_atomically(tmp, path)


def _discard(tmp: Path) -> None:
    try:
        Path(tmp).unlink(missing_ok=True)
    except OSError:
        pass  # the original failure is the one worth reporting
