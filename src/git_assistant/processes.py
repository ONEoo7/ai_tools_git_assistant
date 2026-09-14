"""Ending a child process together with everything it started.

On Windows, killing a process ends that one process. Nothing goes with it:
children are not killed with their parent, and a process group does not change
that -- ``CREATE_NEW_PROCESS_GROUP`` decides which processes a console's Ctrl+C
and Ctrl+Break reach, not which ones die together.

That matters because the process this application starts is often not the one
doing the work:

- the git on PATH is a launcher: ``Git\\cmd\\git.exe`` runs the real git as its
  child;
- a CLI installed with npm is a ``.cmd`` shim: ``cmd.exe`` runs it, and it runs
  ``node``;
- a virtual environment's ``python.exe`` runs the interpreter it was made from.

Killing the process that was started leaves the real one running -- and still
holding the output pipes, so whatever waits for that output goes on waiting, and
a Cancel that was pressed never comes back.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
from pathlib import Path

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def killable() -> dict:
    """``Popen`` arguments for a child that `kill_tree` can end completely.

    On POSIX, a session of its own: its process group is how the processes it
    starts are found again. Windows needs nothing for that -- ``taskkill``
    follows parent ids -- so there this only keeps a console window from
    flashing up.
    """
    if sys.platform == "win32":
        return {"creationflags": _NO_WINDOW}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """End ``proc`` and every process it started. Never raises.

    Only a process that is still running is touched. Once one has exited its id
    no longer names it for certain, and a tree walked from somebody else's id
    would end somebody else's processes.

    ``taskkill`` is named by its full path, so that nothing else called taskkill
    earlier on PATH runs instead. On POSIX the whole process group is signalled,
    which reaches the children of a process started with `killable`.
    """
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        system = Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [str(system / "taskkill.exe"), "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                creationflags=_NO_WINDOW,
                timeout=15,
            )
    else:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()
