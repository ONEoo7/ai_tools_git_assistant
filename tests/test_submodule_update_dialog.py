"""The window that brings a repository's submodules to the latest master.

What is done to each submodule is tested in test_submodule_update, against real
repositories. Here it is replaced by outcomes chosen per path, and the worker is
run on the test's own thread: this is about what the window shows of them.
"""

import pytest

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from git_assistant import git_ops, submodule_update  # noqa: E402
from git_assistant.submodule_update import (  # noqa: E402
    BRANCH,
    STASH_MESSAGE,
    Outcome,
    Result,
    Target,
)
from git_assistant.ui import submodule_update_dialog as dialog_module  # noqa: E402
from git_assistant.ui.submodule_update_dialog import (  # noqa: E402
    FAILED_COLOUR,
    MUTED_COLOUR,
    NOW,
    RESULT,
    SKIPPED_COLOUR,
    SUBMODULE,
    WAS,
    SubmoduleUpdateDialog,
    result_label,
    result_tip,
    summarise,
)


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def _commit(short, subject="a subject"):
    return git_ops.CommitSummary(
        hash=short * 5, short=short, subject=subject, date="2026-09-01 10:00",
        author="A", authored="2026-09-01 10:00",
    )


#: What each submodule comes to, by path.
OUTCOMES = {
    "/top/libs/can": Outcome(
        "/top/libs/can", Result.UPDATED, "", _commit("1111111"), _commit("2222222", "CAN timing")
    ),
    "/top/libs/can/deep": Outcome(
        "/top/libs/can/deep", Result.UP_TO_DATE, BRANCH, _commit("3333333"), _commit("3333333"),
        stashed=True,
    ),
    "/top/ADC": Outcome(
        "/top/ADC", Result.SKIPPED, "dev/x", _commit("4444444"),
        note="origin has no master branch.",
    ),
    "/top/COM": Outcome(
        "/top/COM", Result.FAILED, "", _commit("5555555"),
        note="Could not fetch from origin.\nfatal: could not read Username",
    ),
}

CHAINS = [
    [Target("/top/libs/can", "/top"), Target("/top/libs/can/deep", "/top/libs/can")],
    [Target("/top/ADC", "/top")],
    [Target("/top/COM", "/top")],
]


@pytest.fixture
def inline(monkeypatch):
    """Outcomes by path, and the worker run where the test can wait for it."""
    monkeypatch.setattr(
        submodule_update, "update", lambda target, rules=None, **_how: OUTCOMES[target.path]
    )
    monkeypatch.setattr(dialog_module, "run_worker", lambda worker: worker.run())


def _rows(dialog, column):
    tree = dialog.tree
    return [tree.topLevelItem(i).text(column) for i in range(tree.topLevelItemCount())]


def test_it_opens_on_the_submodules_it_would_bring_up_and_does_nothing_yet(qapp, monkeypatch):
    monkeypatch.setattr(
        git_ops, "head_branch", lambda path: "dev/x" if path.endswith("ADC") else ""
    )
    dialog = SubmoduleUpdateDialog("/top", CHAINS)

    assert _rows(dialog, SUBMODULE) == ["libs/can", "libs/can/deep", "ADC", "COM"]
    assert _rows(dialog, WAS) == ["detached", "detached", "dev/x", "detached"]
    assert _rows(dialog, RESULT) == ["", "", "", ""]
    assert "4 submodules of top" in dialog.intro.text()
    assert not dialog.ran and dialog.progress.isHidden()
    # A return key pressed at a window nobody read does not rewrite fifty checkouts.
    assert dialog.close_btn.isDefault() and not dialog.update_btn.isDefault()


def test_update_brings_them_up_and_shows_what_became_of_each(qapp, inline):
    dialog = SubmoduleUpdateDialog("/top", CHAINS)

    dialog.update_btn.click()
    qapp.processEvents()

    assert dialog.ran
    assert _rows(dialog, WAS) == ["detached at 1111111", BRANCH, "dev/x", "detached at 5555555"]
    assert _rows(dialog, NOW) == [f"{BRANCH} at 2222222", f"{BRANCH} at 3333333", "", ""]
    assert _rows(dialog, RESULT) == [
        "Updated",
        "Up to date - changes stashed",
        "Skipped - origin has no master branch.",
        "Failed - Could not fetch from origin: could not read Username",
    ]
    rows = [dialog.tree.topLevelItem(i) for i in range(4)]
    assert "CAN timing" in rows[0].toolTip(NOW)
    assert "could not read Username" in rows[3].toolTip(RESULT)
    assert rows[2].foreground(RESULT).color() == SKIPPED_COLOUR
    assert rows[3].foreground(RESULT).color() == FAILED_COLOUR
    assert rows[0].foreground(RESULT).color() not in (SKIPPED_COLOUR, FAILED_COLOUR, MUTED_COLOUR)
    assert dialog.progress.value() == 4 and not dialog.progress.isHidden()
    assert dialog.status.text().startswith("1 updated, 1 up to date, 1 skipped, 1 failed.")
    assert "stashed in 1" in dialog.status.text()
    assert dialog.update_btn.isHidden()
    assert (dialog.close_btn.text(), dialog.close_btn.isEnabled()) == ("Close", True)


def test_a_result_that_overtakes_the_word_its_submodule_began_keeps_its_place(qapp):
    """They come from different threads, so they can arrive either way round."""
    dialog = SubmoduleUpdateDialog("/top", CHAINS)
    dialog._running = dialog.ran = True

    dialog._on_done(2, OUTCOMES["/top/ADC"])
    dialog._on_begun(2)
    dialog._on_begun(0)

    assert _rows(dialog, RESULT)[:3] == ["Working...", "", "Skipped - origin has no master branch."]
    assert dialog.status.text() == "1 of 4 submodules done."


def test_the_whole_answer_fills_in_rows_whose_own_result_is_still_on_its_way(qapp):
    dialog = SubmoduleUpdateDialog("/top", CHAINS)
    dialog._running = dialog.ran = True
    dialog._on_done(0, OUTCOMES["/top/libs/can"])

    dialog._on_finished([OUTCOMES[target.path] for chain in CHAINS for target in chain])
    dialog._on_done(0, OUTCOMES["/top/libs/can"])  # and the late one, arriving after all

    assert _rows(dialog, RESULT)[0] == "Updated"
    assert dialog.progress.value() == 4
    assert dialog.status.text().startswith("1 updated, 1 up to date, 1 skipped, 1 failed.")


def test_closing_it_while_it_runs_stops_it_and_keeps_it_open_to_say_how_far_it_got(qapp):
    class Worker:
        cancelled = False

        def cancel(self):
            self.cancelled = True

    dialog = SubmoduleUpdateDialog("/top", CHAINS)
    dialog.show()
    worker = dialog._worker = Worker()
    dialog._running = dialog.ran = True

    dialog.reject()

    assert worker.cancelled and dialog.isVisible()
    assert (dialog.close_btn.text(), dialog.close_btn.isEnabled()) == ("Stopping...", False)
    assert dialog.status.text().startswith("Stopping")

    dialog._on_finished([OUTCOMES["/top/libs/can"]] + [Outcome(p, Result.NOT_RUN) for p in "abc"])
    assert dialog.status.text() == "1 updated, 3 not run."
    # The ones stopped before their turn can have it yet.
    assert not dialog.retry_btn.isHidden() and dialog.retry_btn.text() == "Retry failed (3)"
    dialog.reject()
    assert not dialog.isVisible()


def test_cancel_before_anything_is_done_closes_it_having_done_nothing(qapp):
    dialog = SubmoduleUpdateDialog("/top", CHAINS)
    dialog.show()

    dialog.close_btn.click()

    assert not dialog.isVisible() and not dialog.ran


def test_a_row_says_where_stashed_changes_went_and_how_to_get_them_back():
    stashed = Outcome(
        "/s", Result.UPDATED, stashed=True, note="Its master has 1 commit not on origin yet."
    )

    assert result_label(stashed) == (
        "Updated - changes stashed - Its master has 1 commit not on origin yet."
    )
    assert STASH_MESSAGE in result_tip(stashed) and "git stash pop" in result_tip(stashed)


def test_the_summary_counts_each_kind_of_result_in_order_and_where_the_stashes_are():
    outcomes = [
        Outcome("/a", Result.FAILED),
        Outcome("/b", Result.UPDATED, stashed=True),
        Outcome("/c", Result.UPDATED, stashed=True),
        Outcome("/d", Result.NOT_RUN),
    ]

    assert summarise(outcomes) == (
        "2 updated, 1 failed, 1 not run. Changes were stashed in 2: git stash pop, in "
        "each of them, brings them back."
    )
    assert summarise([Outcome("/a", Result.UP_TO_DATE)]) == "1 up to date."
    assert summarise([]) == "Nothing was done."


# ---- for real: a thread of its own, and repositories -----------------------------------
def _git(cwd, *args):
    import subprocess
    import sys

    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        check=True,
    ).stdout.strip()


def _real_checkouts(tmp_path, monkeypatch):
    """A remote, three checkouts of it at its first commit -- one with a new file in
    it -- and the remote's newest commit, which they have yet to fetch."""
    config = tmp_path / "global.gitconfig"
    config.write_text("[user]\n\tname = Test\n\temail = test@example.com\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", BRANCH)
    (upstream / "f.txt").write_text("one\n", encoding="utf-8")
    _git(upstream, "add", "-A")
    _git(upstream, "commit", "-q", "-m", "one")
    checkouts = []
    for name in ("a", "b", "c"):
        _git(tmp_path, "clone", "-q", str(upstream), str(tmp_path / "top" / name))
        _git(tmp_path / "top" / name, "checkout", "-q", "--detach", "HEAD")
        checkouts.append(tmp_path / "top" / name)
    (checkouts[1] / "new.txt").write_text("untracked\n", encoding="utf-8")
    (upstream / "f.txt").write_text("two\n", encoding="utf-8")
    _git(upstream, "commit", "-q", "-am", "two")
    return checkouts, _git(upstream, "rev-parse", "HEAD")


def _run_to_the_end(dialog):
    import time

    from PyQt6.QtTest import QTest

    dialog.start()
    deadline = time.monotonic() + 60
    while dialog.close_btn.text() != "Close" and time.monotonic() < deadline:
        QTest.qWait(50)
    QTest.qWait(50)  # and any result still on its way from the thread that made it


def test_on_its_own_thread_against_real_repositories(qapp, tmp_path, monkeypatch):
    """What the rest of this file runs on the test's thread, run as the window runs it:
    a thread of its own, fetching from several more, each result sent back across."""
    checkouts, newest = _real_checkouts(tmp_path, monkeypatch)
    dialog = SubmoduleUpdateDialog(
        str(tmp_path / "top"), [[Target(str(path), "")] for path in checkouts]
    )

    _run_to_the_end(dialog)

    assert _rows(dialog, RESULT) == ["Updated", "Updated - changes stashed", "Updated"]
    assert all(_git(path, "rev-parse", "HEAD") == newest for path in checkouts)
    assert dialog.status.text() == (
        "3 updated. Changes were stashed in 1: git stash pop, in that submodule, brings "
        "them back."
    )


def test_a_server_that_turns_each_fetch_away_once_is_asked_again(
    qapp, tmp_path, monkeypatch, slept
):
    """As above, with every fetch refused the first time -- the waits recorded rather
    than lived, so what is left is threads, signals and the rows they fill in."""
    import threading

    checkouts, newest = _real_checkouts(tmp_path, monkeypatch)
    real, refused, lock = git_ops.fetch, set(), threading.Lock()

    def once_refused(path, **how):
        with lock:
            first = str(path) not in refused
            refused.add(str(path))
        if first:
            return git_ops.GitResult(
                ok=False, stdout="", stderr="Connection reset by peer\n", returncode=128
            )
        return real(path, **how)

    monkeypatch.setattr(git_ops, "fetch", once_refused)
    dialog = SubmoduleUpdateDialog(
        str(tmp_path / "top"), [[Target(str(path), "")] for path in checkouts]
    )

    _run_to_the_end(dialog)

    assert _rows(dialog, RESULT) == [
        "Updated - 2 tries",
        "Updated - changes stashed - 2 tries",
        "Updated - 2 tries",
    ]
    assert all(_git(path, "rev-parse", "HEAD") == newest for path in checkouts)
    assert dialog.status.text().endswith("3 had to be fetched more than once.")
    assert slept and dialog.retry_btn.isHidden()


# ---- trying again ------------------------------------------------------------------------
def test_the_worker_passes_on_what_each_waits_for_and_fetches_at_the_pace_given(
    qapp, monkeypatch
):
    from git_assistant.ui.workers import SubmoduleUpdateWorker

    given, heard, paces = submodule_update.Pace(), [], []

    def fake(target, rules=None, *, pace=None, waiting=None):
        paces.append(pace)
        waiting("Fetching again (try 2 of 5)")
        return Outcome(target.path, Result.UPDATED)

    monkeypatch.setattr(submodule_update, "update", fake)
    worker = SubmoduleUpdateWorker([[Target("/top/ADC", "/top")]], pace=given)
    worker.waiting.connect(lambda index, text: heard.append((index, text)))

    worker.run()
    qapp.processEvents()

    assert heard == [(0, "Fetching again (try 2 of 5)")]
    assert paces == [given]


def test_a_fetch_waiting_to_be_tried_again_says_so_on_its_row(qapp):
    dialog = SubmoduleUpdateDialog("/top", CHAINS)
    dialog._running = dialog.ran = True

    dialog._on_waiting(3, "The fetch failed: waiting to try again (2 of 5)")
    assert _rows(dialog, RESULT)[3] == "The fetch failed: waiting to try again (2 of 5)"

    dialog._on_done(3, OUTCOMES["/top/COM"])
    dialog._on_waiting(3, "Fetching again (try 3 of 5)")  # late, from another thread
    assert _rows(dialog, RESULT)[3].startswith("Failed")


def test_retry_failed_brings_up_again_those_that_failed_and_only_those(qapp, inline, monkeypatch):
    asked, paces = [], []

    def now_it_works(target, rules=None, *, pace=None, waiting=None):
        asked.append(target.path)
        paces.append(pace)
        if target.path == "/top/COM" and len(asked) > 4:
            return Outcome("/top/COM", Result.UPDATED, "", _commit("5555555"), _commit("6666666"))
        return OUTCOMES[target.path]

    monkeypatch.setattr(submodule_update, "update", now_it_works)
    dialog = SubmoduleUpdateDialog("/top", CHAINS)
    dialog.update_btn.click()
    qapp.processEvents()
    assert dialog.retry_btn.text() == "Retry failed (1)" and not dialog.retry_btn.isHidden()
    before = _rows(dialog, RESULT)[:3]
    first = dialog._worker

    dialog.retry_btn.click()
    qapp.processEvents()
    # A word from the first run, arriving after all: its rows were other rows.
    first.done.emit(0, OUTCOMES["/top/COM"])
    first.waiting.emit(0, "Fetching again (try 2 of 5)")

    assert asked[4:] == ["/top/COM"]
    assert _rows(dialog, RESULT) == [*before, "Updated"]
    assert _rows(dialog, NOW)[3] == f"{BRANCH} at 6666666"
    assert dialog.status.text().startswith("2 updated, 1 up to date, 1 skipped.")
    assert dialog.retry_btn.isHidden()
    # At the pace the first run found the server to take.
    assert len({id(pace) for pace in paces}) == 1 and paces[0] is not None


def test_a_word_from_a_run_since_replaced_is_passed_over(qapp):
    """The window has started another run by then, whose rows are other rows. Told
    by the run each signal is bound to, as the window binds them."""
    from functools import partial

    from PyQt6.QtCore import QObject, pyqtSignal

    class Worker(QObject):
        done = pyqtSignal(int, object)
        waiting = pyqtSignal(int, str)

    dialog = SubmoduleUpdateDialog("/top", CHAINS)
    dialog._running = dialog.ran = True
    dialog._run_id = 2  # the run going now
    before, now = Worker(), Worker()
    for worker, run in ((before, 1), (now, 2)):
        worker.done.connect(partial(dialog._on_done, run=run))
        worker.waiting.connect(partial(dialog._on_waiting, run=run))

    before.waiting.emit(0, "Fetching again (try 2 of 5)")
    before.done.emit(0, OUTCOMES["/top/libs/can"])
    assert _rows(dialog, RESULT)[0] == "" and dialog.outcomes[0] is None

    now.waiting.emit(1, "Fetching again (try 2 of 5)")
    now.done.emit(0, OUTCOMES["/top/libs/can"])
    assert _rows(dialog, RESULT)[:2] == ["Updated", "Fetching again (try 2 of 5)"]


def test_a_failed_row_says_what_git_said_went_wrong_not_only_that_it_failed():
    from git_assistant.ui.submodule_update_dialog import headline

    ssh = (
        "Could not fetch from origin.\n"
        "kex_exchange_identification: read: Connection reset by peer\n"
        "fatal: Could not read from remote repository.\n\n"
        "Please make sure you have the correct access rights\nand the repository exists."
    )
    assert headline(ssh) == (
        "Could not fetch from origin: kex_exchange_identification: read: Connection reset by peer"
    )
    only_boilerplate = ssh.replace(
        "kex_exchange_identification: read: Connection reset by peer\n", ""
    )
    assert headline(only_boilerplate) == "Could not fetch from origin."
    assert headline("Git cannot work in it.\nfatal: not a git repository: x") == (
        "Git cannot work in it: not a git repository: x"
    )
    assert headline("Could not switch.\nhint: do this\nerror: Your local changes") == (
        "Could not switch: Your local changes"
    )
    assert headline("origin has no master branch.") == "origin has no master branch."
    assert result_label(Outcome("/s", Result.FAILED, note=ssh, tries=5)) == (
        "Failed - Could not fetch from origin: kex_exchange_identification: read: Connection "
        "reset by peer - 5 tries"
    )


def test_the_summary_says_how_many_had_to_be_fetched_more_than_once():
    outcomes = [
        Outcome("/a", Result.UPDATED, tries=3),
        Outcome("/b", Result.UPDATED, tries=1),
        Outcome("/c", Result.FAILED, tries=5),
    ]

    assert summarise(outcomes) == "2 updated, 1 failed. 2 had to be fetched more than once."
