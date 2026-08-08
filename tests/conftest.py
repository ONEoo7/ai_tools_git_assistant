"""Rules that hold for every test here.

The only one so far is about modal dialogs, and it exists because of what
happens without it: a test that reaches ``QMessageBox.question`` and answers
nothing does not fail. It waits, for about thirty seconds, and then Windows
returns a default and the test carries on and passes. Thirty seconds bought
nothing, said nothing, and the suite quietly got slower every time another one
was added.

So a modal that nobody has arranged an answer for is an error, named and
immediate. A test that means to open one says what it answers -- which every
test here that opens one already does.
"""

from __future__ import annotations

import pytest

#: Every store that writes into the user's config directory. A test that runs
#: against the real one leaves files in it -- which happened, and left two
#: settings directories in AppData named after repositories that never existed.
#: Each module here reads `user_config_dir` at call time, which is what makes
#: redirecting it enough.
_STORES = (
    "git_assistant.repo_config",
    "git_assistant.settings_backup",
    "git_assistant.commit_history",
    "git_assistant.usage",
    "git_assistant.review.rule_files",
    "git_assistant.review.rules",
    "git_assistant.review.history",
    "git_assistant.agents.history",
    "git_assistant.identities",
)


@pytest.fixture(autouse=True)
def _config_dir_is_never_the_real_one(tmp_path, monkeypatch):
    """Point every store at a directory this test owns.

    Tests that want to look at what was written patch this themselves and are
    unaffected; this is the floor, so that forgetting to is a test that writes
    somewhere harmless rather than into the user's settings.
    """
    import importlib

    home = tmp_path / "config-root"
    for name in _STORES:
        try:
            module = importlib.import_module(name)
        except ImportError:  # an optional dependency this run does not have
            continue
        if hasattr(module, "user_config_dir"):
            monkeypatch.setattr(
                module, "user_config_dir", lambda *a, **k: str(home)
            )

#: The blocking entry points. Each returns a value the caller acts on, which is
#: exactly why a test that does not choose one is not testing anything.
_MODALS = ("question", "warning", "critical", "information", "about")


@pytest.fixture(autouse=True)
def _no_unanswered_dialogs(monkeypatch):
    """Make an unanswered modal fail the test that opened it."""
    widgets = pytest.importorskip("PyQt6.QtWidgets", reason="Qt is optional here")
    box = widgets.QMessageBox

    def refuse(name):
        def opened(*args, **kwargs):
            raise AssertionError(
                f"QMessageBox.{name} was opened with nothing to answer it. "
                "A test that expects this dialog should say what it answers:\n"
                f"    monkeypatch.setattr(QMessageBox, {name!r}, "
                "lambda *a, **k: QMessageBox.StandardButton.Yes)"
            )

        return opened

    for name in _MODALS:
        if hasattr(box, name):
            monkeypatch.setattr(box, name, staticmethod(refuse(name)))
