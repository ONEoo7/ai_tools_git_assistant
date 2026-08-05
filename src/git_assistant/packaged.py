"""Finding a file that ships inside the application.

A packaged build unpacks the payload somewhere under ``sys._MEIPASS``; a source
checkout has it beside the module that uses it. Getting that right for one file
and wrong for the next is how a build ends up behaving differently from the
checkout it was made from, so it is answered once, here.
"""

from __future__ import annotations

import sys
from pathlib import Path


def data_file(*parts: str) -> Path | None:
    """The bundled file at ``git_assistant/<parts>``, or ``None`` if it is absent.

    ``parts`` is the path *inside the package*, so ``data_file("resources",
    "icon.ico")`` finds ``src/git_assistant/resources/icon.ico`` from source and
    ``<_MEIPASS>/git_assistant/resources/icon.ico`` when frozen.

    Frozen first, then the checkout: a build must not read a stray file from a
    source tree that happens to be beside it. Falling through to the checkout
    matters anyway, because a spec that forgets a ``datas`` entry then fails in
    the build rather than silently in the application.
    """
    candidates = []
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        candidates.append(Path(frozen).joinpath("git_assistant", *parts))
    candidates.append(Path(__file__).resolve().parent.joinpath(*parts))
    return next((c for c in candidates if c.is_file()), None)
