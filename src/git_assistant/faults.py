"""Keeping a record of a start-up that did not finish.

Written after a packaged build died during winget validation with nothing to
show for it but a Windows Error Reporting bucket:

    P1: GitAssistant.exe   P4: Qt6Core.dll
    P8: c0000409           P9: 0000000000000007

``0xc0000409`` is ``STATUS_STACK_BUFFER_OVERRUN``, but the subcode is what says
what happened: **7 is ``FAST_FAIL_FATAL_APP_EXIT``**, which is ``abort()``. Qt
calls ``abort()`` from one place in normal operation -- ``qFatal()`` -- so the
process was terminated *deliberately* by Qt, immediately after printing a
sentence saying why.

And that sentence went nowhere. A windowed build has no console, this
application installed no message handler, and Qt aborts before anything else
runs. So the most useful thing in the whole event was thrown away, and what
reached the bug report was a fault address nobody can symbolise.

That is what this module is for. It is not a fix for any particular crash; it
is the difference between "it crashed" and "it said why". Everything here runs
before ``QApplication`` exists, so it must depend on nothing that needs one.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE = "startup.log"

#: Trimmed to this when it grows past it. Small: this is the last few launches,
#: not a history, and a log nobody can open in Notepad is a log nobody reads.
MAX_BYTES = 256 * 1024

#: Qt message types, by their integer value. Named here rather than imported,
#: so nothing in this module needs QtCore to be importable to write a line.
_QT_LEVELS = {0: "DEBUG", 1: "WARNING", 2: "CRITICAL", 3: "FATAL", 4: "INFO"}


def log_path() -> Path:
    """Beside settings.json, so "Open config folder" reaches it."""
    from platformdirs import user_config_dir

    from git_assistant.config import APP_NAME

    return Path(user_config_dir(APP_NAME, appauthor=False)) / LOG_FILE


def write(line: str) -> None:
    """Append one line. Never raises, and never blocks start-up.

    Opened and closed per line rather than held: this exists to survive a
    process that is about to be killed by ``abort()``, and a buffered handle
    would be exactly the thing that loses the last message.
    """
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > MAX_BYTES:
            _trim(path)
        when = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        with open(path, "a", encoding="utf-8", errors="replace") as handle:
            handle.write(f"{when} {line}\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        return  # a diagnostic that can break start-up is worse than none


def _trim(path: Path) -> None:
    """Keep the newest half. Cheap, and good enough for a launch log."""
    try:
        kept = path.read_text(encoding="utf-8", errors="replace").splitlines()
        path.write_text(
            "\n".join(kept[len(kept) // 2 :]) + "\n", encoding="utf-8"
        )
    except OSError:
        return


# ---- what Qt says on its way out ------------------------------------------------
def _level(mode) -> str:
    """The message type as a word, whatever shape Qt handed it over in.

    PyQt6 passes a ``QtMsgType`` enum member, and ``int()`` refuses one --
    which cost the first version of this module the very message it was written
    to capture: the handler raised, and the fatal went unrecorded exactly as
    before. Hence ``.value`` first, and a fallback that cannot raise.
    """
    raw = getattr(mode, "value", mode)
    try:
        return _QT_LEVELS.get(int(raw), str(mode))
    except (TypeError, ValueError):
        return str(mode)


def _on_qt_message(mode, context, message) -> None:
    """Qt's own logging, captured to the file.

    ``qFatal`` reaches here and then ``abort()``s, so the line has to be on
    disk before this returns -- which is why `write` flushes and fsyncs rather
    than trusting a buffer to be drained by an orderly exit that is not coming.

    Nothing in here may raise. An exception on this path loses the message and
    replaces it with a traceback about the logger, which is worse than silence
    because it looks like the answer.
    """
    try:
        level = _level(mode)
    except Exception:
        level = "MESSAGE"
    where = ""
    try:
        if context is not None and context.file:
            where = f" ({context.file}:{context.line})"
    except Exception:
        where = ""
    write(f"Qt {level}: {message}{where}")


def _on_exception(kind, value, tb) -> None:
    write("Unhandled exception:\n" + "".join(traceback.format_exception(kind, value, tb)))
    sys.__excepthook__(kind, value, tb)


def pin_plugin_path() -> str:
    """Tell Qt where its platform plugins are, when this is a packaged build.

    The first thing `QApplication` does is load the platform plugin, and
    failing to is the most common reason a Qt application aborts on a machine
    it has never run on before. PyInstaller normally arranges this; saying it
    again costs nothing and removes a way for it to be wrong.

    An existing value is left alone -- someone who set it meant it.
    """
    frozen = getattr(sys, "_MEIPASS", "")
    if not frozen or os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH"):
        return os.environ.get("QT_QPA_PLATFORM_PLUGIN_PATH", "")
    for parts in (
        ("PyQt6", "Qt6", "plugins", "platforms"),
        ("PyQt6", "Qt", "plugins", "platforms"),
        ("platforms",),
    ):
        candidate = Path(frozen).joinpath(*parts)
        if candidate.is_dir():
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(candidate)
            return str(candidate)
    write(f"No Qt platform plugin directory found under {frozen}")
    return ""


def install() -> None:
    """Start recording. Call before ``QApplication`` and before any widget.

    Before, because the failure this was written for happens *inside*
    ``QApplication``'s constructor: a handler installed afterwards would be
    installed by a line that never runs.
    """
    try:
        from PyQt6.QtCore import qInstallMessageHandler

        qInstallMessageHandler(_on_qt_message)
    except Exception as exc:  # a build without Qt has bigger problems
        write(f"Could not install the Qt message handler: {exc}")
    sys.excepthook = _on_exception
    write(f"--- starting {_version()} ({'packaged' if getattr(sys, 'frozen', False) else 'source'})")
    pinned = pin_plugin_path()
    if pinned:
        write(f"Qt platform plugins: {pinned}")


def _version() -> str:
    try:
        from git_assistant import __version__

        return f"git-assistant {__version__}"
    except Exception:
        return "git-assistant"
