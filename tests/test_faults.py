"""The start-up log, written because a crash report had nothing else in it.

A packaged build died during winget validation inside Qt6Core with
``c0000409`` / subcode ``7`` -- ``FAST_FAIL_FATAL_APP_EXIT``, which is
``abort()``, which in Qt means ``qFatal()``. Qt printed a sentence saying why
and then killed the process; the build was windowed, so the sentence went to a
stream that does not exist, and the bug report was a fault offset.

These tests are all one requirement: **whatever Qt says on its way out has to
be on disk before the process dies.**
"""

import sys

import pytest

pytest.importorskip("PyQt6.QtCore")

from PyQt6.QtCore import QtMsgType  # noqa: E402

from git_assistant import faults  # noqa: E402

#: The real thing Qt hands the handler. Passing a plain ``3`` here is what let
#: the first version of this module ship a handler that raised on every real
#: message -- ``int()`` refuses a ``QtMsgType``.
FATAL = QtMsgType.QtFatalMsg
WARNING = QtMsgType.QtWarningMsg


@pytest.fixture(autouse=True)
def log(tmp_path, monkeypatch):
    """Never the real config directory."""
    written = tmp_path / faults.LOG_FILE
    monkeypatch.setattr(faults, "log_path", lambda: written)
    return written


def _read(log) -> str:
    return log.read_text(encoding="utf-8") if log.exists() else ""


# ---- the message that mattered ------------------------------------------------
def test_a_qt_fatal_reaches_the_file(log):
    """The one this exists for: qFatal, then abort, then nothing else runs."""
    faults._on_qt_message(FATAL, None, "could not find or load the Qt platform plugin")

    assert "FATAL" in _read(log)
    assert "Qt platform plugin" in _read(log)


def test_it_is_on_disk_before_the_handler_returns(log):
    """Qt aborts the moment this returns, so a buffered write loses it."""
    faults._on_qt_message(FATAL, None, "fatal")
    assert "fatal" in _read(log), "still buffered when the process would have died"


def test_ordinary_qt_warnings_are_kept_too(log):
    faults._on_qt_message(WARNING, None, "QFont::setPointSize: Point size <= 0")
    assert "WARNING" in _read(log)


def test_an_unknown_level_is_recorded_rather_than_dropped(log):
    faults._on_qt_message(99, None, "something new")
    assert "something new" in _read(log)


def test_the_enum_qt_actually_passes_is_understood():
    """Not an int. `int(QtMsgType.QtFatalMsg)` raises, and a handler that
    raises loses the message it exists to keep -- which is what happened."""
    assert faults._level(FATAL) == "FATAL"
    assert faults._level(WARNING) == "WARNING"


def test_a_level_of_any_shape_at_all_still_writes_the_message(log):
    faults._on_qt_message(object(), None, "the message survives")
    assert "the message survives" in _read(log)


def test_a_context_object_that_misbehaves_does_not_lose_the_message(log):
    class Awkward:
        @property
        def file(self):
            raise RuntimeError("no")

    faults._on_qt_message(FATAL, Awkward(), "the important part")

    assert "the important part" in _read(log)


# ---- python-level crashes ------------------------------------------------------
def test_an_unhandled_exception_is_recorded(log, monkeypatch):
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: None)
    try:
        raise ValueError("boom")
    except ValueError:
        faults._on_exception(*sys.exc_info())

    assert "Unhandled exception" in _read(log) and "boom" in _read(log)


def test_the_default_hook_still_runs(log, monkeypatch):
    """Recording it must not swallow it: the console build still prints."""
    seen = []
    monkeypatch.setattr(sys, "__excepthook__", lambda *a: seen.append(a))
    try:
        raise ValueError("boom")
    except ValueError:
        faults._on_exception(*sys.exc_info())

    assert seen


# ---- it can never be the reason start-up fails -----------------------------------
def test_a_log_that_cannot_be_written_is_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(
        faults, "log_path", lambda: (_ for _ in ()).throw(OSError("read only"))
    )
    faults.write("something")  # must not raise


def test_installing_never_raises(monkeypatch, log):
    monkeypatch.setattr(
        "PyQt6.QtCore.qInstallMessageHandler",
        lambda h: (_ for _ in ()).throw(RuntimeError("no")),
    )
    faults.install()
    assert "Could not install" in _read(log)


def test_installing_records_which_build_this_is(log):
    faults.install()
    assert "starting git-assistant" in _read(log)
    assert "source" in _read(log), "a packaged build says 'packaged' instead"


# ---- the plugin path -------------------------------------------------------------
def test_a_source_checkout_is_left_alone(monkeypatch):
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.delenv("QT_QPA_PLATFORM_PLUGIN_PATH", raising=False)

    assert faults.pin_plugin_path() == ""
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" not in __import__("os").environ


def test_a_packaged_build_is_told_where_its_plugins_are(monkeypatch, tmp_path):
    """Failing to load the platform plugin is the commonest reason a Qt app
    aborts on a machine it has never run on."""
    where = tmp_path / "PyQt6" / "Qt6" / "plugins" / "platforms"
    where.mkdir(parents=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delenv("QT_QPA_PLATFORM_PLUGIN_PATH", raising=False)

    assert faults.pin_plugin_path() == str(where)


def test_a_value_somebody_set_is_not_overruled(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("QT_QPA_PLATFORM_PLUGIN_PATH", "C:/theirs")

    assert faults.pin_plugin_path() == "C:/theirs"


def test_a_build_with_no_plugins_at_all_says_so(monkeypatch, tmp_path, log):
    """Which is a finding: that build cannot start, and now the log knows."""
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delenv("QT_QPA_PLATFORM_PLUGIN_PATH", raising=False)

    faults.pin_plugin_path()

    assert "No Qt platform plugin directory" in _read(log)


# ---- the file stays readable ---------------------------------------------------------
def test_it_does_not_grow_without_bound(log, monkeypatch):
    monkeypatch.setattr(faults, "MAX_BYTES", 200)
    for i in range(200):
        faults.write(f"line {i}")

    assert log.stat().st_size < 4000
    assert "line 199" in _read(log), "the newest is what is kept"


def test_a_clean_exit_is_recognisable_as_one(log):
    """So a log that stops mid-start-up reads as a log that stopped."""
    faults.write("Exited normally (0).")
    assert "Exited normally" in _read(log)


# ---- it is installed early enough --------------------------------------------------------
def test_the_handler_is_installed_before_qapplication_is_built():
    """Installed afterwards, it would be installed by a line that never runs:
    the failure happens inside QApplication's constructor."""
    from pathlib import Path

    source = Path(faults.__file__).with_name("app.py").read_text(encoding="utf-8")
    body = source[source.index("def main("):]
    assert body.index("faults.install()") < body.index("QApplication(sys.argv)")
