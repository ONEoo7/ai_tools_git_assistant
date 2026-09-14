"""The tray adding repositories that appear in a folder it watches.

Driven through the real worker thread: the scan and the merge agree on the
shape of what passes between them only if both halves actually run.
"""

import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant.config import RepoEntry, Settings  # noqa: E402
from git_assistant.ui import tray as tray_module  # noqa: E402

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _repo(path):
    path.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", str(path), "init", "-q"],
        capture_output=True,
        creationflags=_NO_WINDOW,
        check=True,
    )
    return path


def _wait_until(qapp, done, what, slot_errors, timeout=30.0):
    """Pump events until ``done()``: the worker reports back through the loop."""
    deadline = time.monotonic() + timeout
    while not done() and not slot_errors:
        assert time.monotonic() < deadline, f"{what} never reported back"
        qapp.processEvents()
        time.sleep(0.01)
    qapp.processEvents()  # the rest of a slot that settled `done` first
    _raise_first(slot_errors)


@pytest.fixture
def slot_errors(monkeypatch):
    """Exceptions raised inside Qt slots, kept so the test can fail on them.

    With Python's own ``sys.excepthook`` in place, PyQt ends the process when a
    slot raises: no traceback, no test name, and nothing after it runs. Given a
    hook of anyone else's it calls that instead.
    """
    caught = []
    monkeypatch.setattr(sys, "excepthook", lambda *exc: caught.append(exc))
    return caught


def _raise_first(caught):
    if caught:
        _kind, error, trace = caught[0]
        raise error.with_traceback(trace)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def settings(monkeypatch):
    s = Settings()
    s.save = lambda: None  # never touch the real config file
    monkeypatch.setattr(Settings, "load", staticmethod(lambda: s))
    return s


@pytest.fixture
def tray(qapp, settings, monkeypatch):
    """A TrayApp with no update check, no real watcher and no balloons."""
    monkeypatch.setattr(tray_module, "unavailable_reason", lambda: "not in tests")
    monkeypatch.setattr(tray_module.TrayApp, "_refresh_watcher", lambda self: None)
    app = tray_module.TrayApp(qapp)
    app.notes = []
    monkeypatch.setattr(app, "_notify", lambda title, text: app.notes.append(text))
    return app


def _watched(qapp, tray, folder, slot_errors):
    tray._on_watched_change(str(folder))
    _wait_until(
        qapp, lambda: not tray._watch_workers, "the watched-folder scan", slot_errors
    )


def test_repositories_that_appear_in_a_watched_folder_are_added(
    qapp, tray, settings, tmp_path, slot_errors
):
    _repo(tmp_path / "alpha")
    _repo(tmp_path / "beta")

    _watched(qapp, tray, tmp_path, slot_errors)

    assert sorted(Path(r.path).name for r in settings.repos) == ["alpha", "beta"]
    assert tray.notes == [f"Auto-added 2 new repo(s) from {tmp_path}."]


def test_a_repository_already_listed_is_not_added_again(
    qapp, tray, settings, tmp_path, slot_errors
):
    _repo(tmp_path / "alpha")
    _repo(tmp_path / "beta")
    settings.repos = [RepoEntry(str(tmp_path / "alpha"))]

    _watched(qapp, tray, tmp_path, slot_errors)

    assert sorted(Path(r.path).name for r in settings.repos) == ["alpha", "beta"]
    assert tray.notes == [f"Auto-added 1 new repo(s) from {tmp_path}."]


def test_watching_a_folder_runs_no_git_at_all(
    qapp, tray, settings, tmp_path, monkeypatch, slot_errors
):
    """Finding repositories is a walk of the disk, and that is all it takes.

    This used to ask git about every repository it found -- its remote's owner
    and whether it was blocked -- and a folder of fifty clones was fifty git
    processes, for an owner that is no longer shown and a flag it ignored.
    """
    _repo(tmp_path / "alpha")
    _repo(tmp_path / "beta")
    spawned = []
    # Recorded rather than failed on the spot: this runs on the worker thread,
    # where an exception is the worker's to report and not this test's.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(a[0]))

    _watched(qapp, tray, tmp_path, slot_errors)

    assert spawned == []
    assert len(settings.repos) == 2
