"""Swapping a file into place while something else is holding it open.

On Windows `os.replace` onto a file another process has open -- an antivirus, the
search indexer, a second copy of the application -- fails with a PermissionError
that clears in milliseconds. These are what every store's saves wait out.
"""

import os

import pytest

from git_assistant import atomic


class Held:
    """An `os.replace` refused the first ``refusals`` times, with ``error``.

    Holds the real one: `os.replace` itself is what is patched.
    """

    def __init__(self, refusals, error=None):
        self.refusals = refusals
        self.error = error or PermissionError(13, "Access is denied")
        self.calls = 0
        self._real = os.replace

    def __call__(self, src, dst):
        self.calls += 1
        if self.calls <= self.refusals:
            raise self.error
        return self._real(src, dst)


@pytest.fixture
def waits(monkeypatch):
    """The waits, recorded rather than lived."""
    seen = []
    monkeypatch.setattr(atomic, "sleep", seen.append)
    return seen


def _files(tmp_path):
    destination = tmp_path / "store.json"
    destination.write_text("old", encoding="utf-8")
    tmp = tmp_path / "store.json.abcd.tmp"
    tmp.write_text("new", encoding="utf-8")
    return tmp, destination


def test_a_destination_held_open_for_a_moment_is_waited_out(tmp_path, monkeypatch, waits):
    tmp, destination = _files(tmp_path)
    held = Held(refusals=2)
    monkeypatch.setattr(os, "replace", held)

    atomic.replace_atomically(tmp, destination)

    assert destination.read_text(encoding="utf-8") == "new"
    assert held.calls == 3 and waits == [atomic.REPLACE_BACKOFF, 2 * atomic.REPLACE_BACKOFF]
    assert not tmp.exists()


def test_it_gives_up_after_about_a_second_and_leaves_nothing_behind(
    tmp_path, monkeypatch, waits
):
    tmp, destination = _files(tmp_path)
    held = Held(refusals=99)
    monkeypatch.setattr(os, "replace", held)

    with pytest.raises(PermissionError):
        atomic.replace_atomically(tmp, destination)

    assert held.calls == atomic.REPLACE_ATTEMPTS
    assert 0.5 < sum(waits) <= 1.0  # long enough for a scan, short enough to wait on
    assert destination.read_text(encoding="utf-8") == "old"
    assert not tmp.exists()


def test_anything_but_a_hold_is_not_tried_again(tmp_path, monkeypatch, waits):
    """The disk full says the same the second time."""
    tmp, destination = _files(tmp_path)
    held = Held(refusals=99, error=OSError(28, "No space left on device"))
    monkeypatch.setattr(os, "replace", held)

    with pytest.raises(OSError, match="No space left"):
        atomic.replace_atomically(tmp, destination)

    assert held.calls == 1 and waits == []
    assert not tmp.exists()


def test_a_file_is_written_whole_or_not_at_all(tmp_path, monkeypatch, waits):
    destination = tmp_path / "store.json"
    destination.write_text("old", encoding="utf-8")

    atomic.write_atomically(destination, "new")
    assert destination.read_text(encoding="utf-8") == "new"

    monkeypatch.setattr(os, "replace", Held(refusals=99))
    with pytest.raises(PermissionError):
        atomic.write_atomically(destination, "newer")

    assert destination.read_text(encoding="utf-8") == "new"
    assert [p.name for p in tmp_path.iterdir()] == ["store.json"]


def test_a_temporary_file_that_could_not_be_written_is_not_left_either(
    tmp_path, monkeypatch
):
    destination = tmp_path / "store.json"
    real = type(destination).write_text

    def half_written(self, text, *args, **kwargs):
        real(self, text[:2], *args, **kwargs)
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(type(destination), "write_text", half_written)

    with pytest.raises(OSError):
        atomic.write_atomically(destination, "a long text")

    assert list(tmp_path.iterdir()) == []
