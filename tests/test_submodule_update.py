"""Submodules brought to the newest master of their remote: fetched, stashed, switched.

Against real repositories: what is being tested is what git does to a checkout, and
a stub would only be this file's idea of that. Each test gets its own copy of a
remote and of a checkout of it, made once for the module and copied, because the
thirty git commands that make them were most of every test's time.
"""

import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from git_assistant import git_ops, submodule_update
from git_assistant.repo_config import FetchRules
from git_assistant.submodule_update import (
    BRANCH,
    STASH_MESSAGE,
    Outcome,
    Result,
    Target,
    update,
    update_all,
)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        creationflags=_NO_WINDOW,
        check=True,
    )


def _head(repo, rev="HEAD"):
    return _git(repo, "rev-parse", rev).stdout.strip()


def _commit(repo, name, text):
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"{name}: {text.strip()}")
    return _head(repo)


def _git_home(folder, patch):
    """An identity, and local folders allowed as submodule sources."""
    config = folder / "global.gitconfig"
    config.write_text(
        '[protocol "file"]\n\tallow = always\n'
        "[user]\n\tname = Test\n\temail = test@example.com\n"
        "[init]\n\tdefaultBranch = master\n",
        encoding="utf-8",
    )
    patch.setenv("GIT_CONFIG_GLOBAL", str(config))
    patch.setenv("GIT_CONFIG_NOSYSTEM", "1")


@pytest.fixture(autouse=True)
def git_home(tmp_path, monkeypatch):
    _git_home(tmp_path, monkeypatch)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """A remote with two commits on master, and a checkout of it at the first one --
    detached, as a submodule is checked out, with a local master at the second."""
    base = tmp_path_factory.mktemp("update-template")
    with pytest.MonkeyPatch.context() as patch:
        _git_home(base, patch)
        upstream = base / "upstream"
        upstream.mkdir()
        _git(upstream, "init", "-q", "-b", BRANCH)
        (upstream / "keep.txt").write_text("kept\n", encoding="utf-8")
        first = _commit(upstream, "f.txt", "one\n")
        second = _commit(upstream, "f.txt", "two\n")
        _git(base, "clone", "-q", str(upstream), str(base / "sub"))
        _git(base / "sub", "checkout", "-q", "--detach", first)
    return SimpleNamespace(base=base, first=first, second=second)


@pytest.fixture
def repos(template, tmp_path):
    upstream, sub = tmp_path / "upstream", tmp_path / "sub"
    shutil.copytree(template.base / "upstream", upstream)
    shutil.copytree(template.base / "sub", sub)
    _git(sub, "remote", "set-url", "origin", str(upstream))
    return SimpleNamespace(
        upstream=upstream, sub=sub, first=template.first, second=template.second
    )


def _stashes(repo):
    return _git(repo, "stash", "list", "--format=%gs").stdout.splitlines()


def _status(repo):
    return _git(repo, "status", "--porcelain", "--untracked-files=normal").stdout


# ---- one submodule -------------------------------------------------------------------
def test_a_detached_submodule_is_switched_to_the_newest_master_of_its_remote(repos):
    newest = _commit(repos.upstream, "f.txt", "three\n")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.UPDATED
    assert git_ops.head_branch(repos.sub) == BRANCH
    assert _head(repos.sub) == newest == outcome.now.hash
    assert (outcome.was_branch, outcome.was.hash) == ("", repos.first)
    assert not outcome.stashed and not outcome.note
    assert _git(repos.sub, "rev-parse", "--abbrev-ref", f"{BRANCH}@{{u}}").stdout.strip() == (
        f"origin/{BRANCH}"
    )
    # Its local master was behind, and was moved before it was checked out -- and
    # says in its reflog who moved it.
    moved = _git(repos.sub, "reflog", "show", "-1", "--format=%gs", f"refs/heads/{BRANCH}")
    assert moved.stdout.strip() == f"Git Assistant: fast-forward to origin/{BRANCH}"


def test_a_submodule_with_no_master_of_its_own_gets_one_tracking_the_remote_s(repos):
    """Tracking it whatever ``branch.autoSetupMerge`` says: it is where it came from."""
    _git(repos.sub, "branch", "-q", "-D", BRANCH)
    _git(repos.sub, "config", "branch.autoSetupMerge", "false")
    newest = _commit(repos.upstream, "f.txt", "three\n")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.UPDATED
    assert _head(repos.sub) == newest
    assert _git(repos.sub, "rev-parse", "--abbrev-ref", f"{BRANCH}@{{u}}").stdout.strip() == (
        f"origin/{BRANCH}"
    )


def test_changed_and_untracked_files_are_stashed_first_and_come_back_with_a_pop(repos):
    _commit(repos.upstream, "f.txt", "three\n")
    (repos.sub / "keep.txt").write_text("edited\n", encoding="utf-8")
    (repos.sub / "new.txt").write_text("brand new\n", encoding="utf-8")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.UPDATED and outcome.stashed
    assert _status(repos.sub) == ""
    assert _stashes(repos.sub) == [f"On (no branch): {STASH_MESSAGE}"]
    _git(repos.sub, "stash", "pop", "-q")
    assert (repos.sub / "keep.txt").read_text(encoding="utf-8") == "edited\n"
    assert (repos.sub / "new.txt").read_text(encoding="utf-8") == "brand new\n"


def test_ignored_files_are_not_work_and_stay_where_they_are(repos):
    (repos.sub / ".git" / "info" / "exclude").write_text("*.o\n", encoding="utf-8")
    (repos.sub / "build.o").write_text("object\n", encoding="utf-8")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.UPDATED and not outcome.stashed
    assert _stashes(repos.sub) == []
    assert (repos.sub / "build.o").exists()


def test_a_submodule_on_a_branch_of_its_own_is_switched_and_the_branch_is_kept(repos):
    _git(repos.sub, "switch", "-q", "-c", "feature")
    mine = _commit(repos.sub, "g.txt", "mine\n")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.UPDATED
    assert outcome.was_branch == "feature"
    assert git_ops.head_branch(repos.sub) == BRANCH
    assert _head(repos.sub, "refs/heads/feature") == mine


def test_master_checked_out_and_behind_is_brought_forward(repos):
    _git(repos.sub, "switch", "-q", BRANCH)
    newest = _commit(repos.upstream, "f.txt", "three\n")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.UPDATED
    assert (outcome.was_branch, outcome.was.hash) == (BRANCH, repos.second)
    assert _head(repos.sub) == newest


def test_master_at_the_newest_already_is_up_to_date(repos):
    _git(repos.sub, "switch", "-q", BRANCH)

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.UP_TO_DATE
    assert outcome.now.hash == outcome.was.hash == repos.second


def test_changes_are_stashed_even_where_master_is_already_the_newest(repos):
    """Every submodule it was asked to bring up ends up at master, and clean: its
    changes are in a stash there. The same for all of them, rather than some left
    with their changes and some not, depending on what their remote did."""
    _git(repos.sub, "switch", "-q", BRANCH)
    (repos.sub / "keep.txt").write_text("edited\n", encoding="utf-8")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.UP_TO_DATE and outcome.stashed
    assert _status(repos.sub) == ""


def test_a_master_that_has_gone_its_own_way_is_left_exactly_as_it_was(repos):
    """A merge or a rebase is somebody's decision -- and nothing is stashed for a
    submodule that is not going to be switched."""
    _git(repos.sub, "switch", "-q", BRANCH)
    mine = _commit(repos.sub, "g.txt", "mine\n")
    _commit(repos.upstream, "f.txt", "three\n")
    (repos.sub / "keep.txt").write_text("edited\n", encoding="utf-8")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.SKIPPED
    assert "1 commit origin does not" in outcome.note and "1 new" in outcome.note
    assert _head(repos.sub) == mine
    assert _status(repos.sub) == " M keep.txt\n"
    assert _stashes(repos.sub) == []


def test_a_master_with_commits_of_its_own_only_is_switched_to_and_that_is_said(repos):
    _git(repos.sub, "switch", "-q", BRANCH)
    mine = _commit(repos.sub, "g.txt", "mine\n")
    _git(repos.sub, "checkout", "-q", "--detach", repos.first)

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.UPDATED
    assert _head(repos.sub) == mine
    assert outcome.note == f"Its {BRANCH} has 1 commit not on origin yet."


def test_commits_on_no_branch_are_never_left_behind(repos):
    """Switching away from them leaves them to the reflog, which git only warns about."""
    orphan = _commit(repos.sub, "g.txt", "on no branch\n")
    (repos.sub / "keep.txt").write_text("edited\n", encoding="utf-8")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.SKIPPED
    assert outcome.note.startswith("1 commit here is on no branch")
    assert _head(repos.sub) == orphan
    assert _stashes(repos.sub) == [] and _status(repos.sub) == " M keep.txt\n"


def test_two_commits_on_no_branch_are_counted_as_two(repos):
    _commit(repos.sub, "g.txt", "one\n")
    _commit(repos.sub, "g.txt", "two\n")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.SKIPPED
    assert outcome.note.startswith("2 commits here are on no branch")
    assert "leave them behind: put them on a branch first" in outcome.note


def test_a_remote_with_no_master_leaves_the_submodule_alone(repos):
    _git(repos.upstream, "branch", "-q", "-m", BRANCH, "main")
    (repos.sub / "keep.txt").write_text("edited\n", encoding="utf-8")

    outcome = update(Target(str(repos.sub)))

    assert (outcome.result, outcome.note) == (Result.SKIPPED, f"origin has no {BRANCH} branch.")
    assert _head(repos.sub) == repos.first and _stashes(repos.sub) == []


def test_a_fetch_that_fails_changes_nothing(repos, tmp_path):
    _git(repos.sub, "remote", "set-url", "origin", str(tmp_path / "gone"))
    (repos.sub / "keep.txt").write_text("edited\n", encoding="utf-8")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.FAILED
    first, _newline, detail = outcome.note.partition("\n")
    assert first == "Could not fetch from origin." and detail  # and git's own words
    assert _head(repos.sub) == repos.first and _stashes(repos.sub) == []


def test_a_submodule_with_no_remote_is_left_alone(repos):
    _git(repos.sub, "remote", "remove", "origin")

    outcome = update(Target(str(repos.sub)))

    assert (outcome.result, outcome.note) == (Result.SKIPPED, "It has no remote to fetch from.")


def test_master_is_brought_from_the_remote_it_tracks(repos, tmp_path):
    """Not origin, when it tracks another: that is where the user pulls it from."""
    mirror = tmp_path / "mirror"
    shutil.copytree(repos.upstream, mirror)
    newest = _commit(mirror, "f.txt", "only on the mirror\n")
    _git(repos.sub, "remote", "add", "mirror", str(mirror))
    _git(repos.sub, "config", f"branch.{BRANCH}.remote", "mirror")

    outcome = update(Target(str(repos.sub)))

    assert outcome.result is Result.UPDATED
    assert _head(repos.sub) == newest


def test_a_folder_that_is_not_checked_out_is_left_alone(tmp_path):
    (tmp_path / "empty").mkdir()

    outcome = update(Target(str(tmp_path / "empty")))

    assert (outcome.result, outcome.note) == (Result.SKIPPED, "There is no checkout of it here.")


def test_a_repository_git_will_not_work_in_is_reported_in_git_s_words(tmp_path):
    """Not as "no remote", which is what an unreadable configuration looks like."""
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / ".git").write_text(f"gitdir: {tmp_path / 'nowhere'}\n", encoding="utf-8")

    outcome = update(Target(str(broken)))

    assert outcome.result is Result.FAILED
    first, _newline, detail = outcome.note.partition("\n")
    assert first == "Git cannot work in it." and "not a git repository" in detail


@pytest.mark.parametrize("tags", [True, False])
def test_the_fetch_follows_the_submodule_s_own_fetch_rules(repos, tags):
    _commit(repos.upstream, "f.txt", "three\n")
    _git(repos.upstream, "tag", "v9")

    update(Target(str(repos.sub)), FetchRules(tags=tags))

    assert bool(_git(repos.sub, "tag", "--list", "v9").stdout.strip()) is tags


# ---- a submodule of a real repository --------------------------------------------------
def _submodule_of(tmp_path, source, where="libs/lib"):
    """A repository with ``source`` as a submodule at ``where``."""
    top = tmp_path / "top"
    top.mkdir()
    _git(top, "init", "-q", "-b", BRANCH)
    _commit(top, "top.txt", "top\n")
    _git(top, "submodule", "add", "-q", str(source), where)
    _git(top, "commit", "-q", "-m", f"add {where}")
    return top


def test_the_commit_its_repository_records_is_not_counted_as_left_behind(repos, tmp_path):
    """A submodule is checked out at the commit its repository records, and that
    commit need not be on any branch of the remote any more: the branch it was made
    on is often deleted once merged. The repository can check it out again."""
    _git(repos.upstream, "switch", "-q", "-c", "feature")
    recorded = _commit(repos.upstream, "g.txt", "on a feature\n")
    _git(repos.upstream, "switch", "-q", BRANCH)
    top = _submodule_of(tmp_path, repos.upstream)
    lib = top / "libs" / "lib"
    _git(lib, "checkout", "-q", "--detach", recorded)
    _git(top, "add", "libs/lib")
    _git(top, "commit", "-q", "-m", "record the feature")
    _git(repos.upstream, "branch", "-q", "-D", "feature")  # merged, say, and deleted

    alone = update(Target(str(lib)))
    assert alone.result is Result.SKIPPED  # without its repository, it is on no branch

    outcome = update(Target(str(lib), str(top)))

    assert outcome.result is Result.UPDATED
    assert git_ops.head_branch(lib) == BRANCH


@pytest.fixture(scope="module")
def nested(tmp_path_factory):
    """A library with a submodule of its own, which its newest commit moves on; and a
    checkout of the library at the commit before that, with ``submodule.recurse`` on.

    The remotes are read and never written, so each test copies the checkout alone.
    """
    base = tmp_path_factory.mktemp("update-nested")
    with pytest.MonkeyPatch.context() as patch:
        _git_home(base, patch)
        inner = base / "inner"
        inner.mkdir()
        _git(inner, "init", "-q", "-b", BRANCH)
        first_inner = _commit(inner, "i.txt", "one\n")
        lib = _submodule_of(base, inner, "deps/inner")
        old = _head(lib)
        second_inner = _commit(inner, "i.txt", "two\n")
        _git(lib / "deps" / "inner", "pull", "-q", "origin", BRANCH)
        _git(lib, "commit", "-q", "-am", "bump inner")
        checkout = base / "checkout"
        _git(base, "clone", "-q", str(lib), str(checkout))
        _git(checkout, "checkout", "-q", "--detach", old)
        _git(checkout, "submodule", "update", "-q", "--init")
        _git(checkout, "config", "submodule.recurse", "true")
    return SimpleNamespace(
        checkout=checkout, old=old, newest=_head(lib), first=first_inner, second=second_inner
    )


def _copy_of(nested, tmp_path):
    checkout = tmp_path / "checkout"
    shutil.copytree(nested.checkout, checkout)
    return checkout


@pytest.mark.parametrize("start", ["switched to", "made from the remote's", "brought forward"])
def test_the_submodules_inside_are_left_where_they_are_whatever_submodule_recurse_says(
    nested, tmp_path, start
):
    """They are listed under the row too, and brought up in their turn -- after their
    own checks, which git doing it first would have gone ahead of. However master is
    come to: a local one switched to, one made from the remote's, or the one checked
    out brought forward."""
    checkout = _copy_of(nested, tmp_path)
    if start == "made from the remote's":
        _git(checkout, "branch", "-q", "-D", BRANCH)
    elif start == "brought forward":
        _git(checkout, "-c", "submodule.recurse=false", "switch", "-q", "-C", BRANCH, nested.old)
    assert _head(checkout / "deps" / "inner") == nested.first

    outcome = update(Target(str(checkout)))

    assert outcome.result is Result.UPDATED
    assert _head(checkout) == nested.newest
    assert _head(checkout / "deps" / "inner") == nested.first != nested.second


def test_a_submodule_inside_that_has_moved_is_not_something_to_stash(nested, tmp_path):
    """Its own commit is its own repository's business; git's stash never takes it."""
    checkout = _copy_of(nested, tmp_path)
    _git(checkout / "deps" / "inner", "checkout", "-q", "--detach", nested.second)

    outcome = update(Target(str(checkout)))

    assert outcome.result is Result.UPDATED and not outcome.stashed
    assert _stashes(checkout) == []
    assert _head(checkout / "deps" / "inner") == nested.second


# ---- what it is made of -----------------------------------------------------------------
def test_a_branch_is_never_fast_forwarded_over_commits_of_its_own(repos):
    _git(repos.sub, "switch", "-q", BRANCH)
    mine = _commit(repos.sub, "g.txt", "mine\n")
    _commit(repos.upstream, "f.txt", "three\n")
    _git(repos.sub, "fetch", "-q", "origin")

    assert not git_ops.fast_forward(repos.sub, f"origin/{BRANCH}").ok
    assert _head(repos.sub) == mine


def test_a_branch_that_moved_since_it_was_looked_at_is_not_moved(repos):
    assert not git_ops.move_branch(
        repos.sub, BRANCH, repos.first, expected=repos.first, why="test"
    ).ok
    assert _head(repos.sub, f"refs/heads/{BRANCH}") == repos.second


def test_a_kept_commit_the_repository_does_not_have_is_passed_over(repos):
    assert git_ops.commits_on_no_branch(repos.sub, kept=("0123456789" * 4,)) == 0


# ---- many submodules ------------------------------------------------------------------
def _recording(monkeypatch, *, raise_for=(), block=None):
    """``update`` replaced: each target is noted as it begins and ends, and succeeds."""
    events, lock = [], threading.Lock()
    active, most = [0], [0]

    def fake(target, rules=None, **_how):
        with lock:
            events.append(("begin", target.path))
            active[0] += 1
            most[0] = max(most[0], active[0])
        if block is not None:
            block(target)
        with lock:
            active[0] -= 1
            events.append(("end", target.path))
        if target.path in raise_for:
            raise OSError(f"{target.path} exploded")
        return Outcome(target.path, Result.UPDATED)

    monkeypatch.setattr(submodule_update, "update", fake)
    return events, most


def test_every_submodule_is_brought_up_and_the_outcomes_come_back_in_order(monkeypatch):
    events, _most = _recording(monkeypatch)
    chains = [[Target(f"/r/{n}")] for n in "abcdef"]
    heard = []

    outcomes = update_all(chains, finished=lambda row, outcome: heard.append((row, outcome.path)))

    assert [outcome.path for outcome in outcomes] == [f"/r/{n}" for n in "abcdef"]
    assert sorted(heard) == [(i, f"/r/{n}") for i, n in enumerate("abcdef")]
    assert len(events) == 12


def test_one_inside_another_is_brought_up_after_it(monkeypatch):
    events, _most = _recording(monkeypatch)
    chains = [
        [Target("/r/a"), Target("/r/a/in", "/r/a"), Target("/r/a/in/deeper", "/r/a/in")],
        [Target("/r/b"), Target("/r/b/in", "/r/b")],
        [Target("/r/c")],
    ]

    outcomes = update_all(chains, at_once=3)

    ended = [path for kind, path in events if kind == "end"]
    for chain in chains:
        paths = [target.path for target in chain]
        assert [path for path in ended if path in paths] == paths
        for inner, outer in zip(paths[1:], paths):
            assert events.index(("begin", inner)) > events.index(("end", outer))
    assert [outcome.path for outcome in outcomes] == [
        "/r/a", "/r/a/in", "/r/a/in/deeper", "/r/b", "/r/b/in", "/r/c"
    ]


def test_the_first_is_brought_up_alone_so_a_password_is_asked_for_once(monkeypatch):
    import time

    # The first one takes a while, so any other begun beside it would be seen to.
    events, _most = _recording(
        monkeypatch, block=lambda target: time.sleep(0.2 if target.path == "/r/a" else 0)
    )
    chains = [[Target(f"/r/{n}")] for n in "abcde"]

    update_all(chains, at_once=4)

    assert events[:2] == [("begin", "/r/a"), ("end", "/r/a")]


def test_no_more_are_brought_up_at_once_than_allowed(monkeypatch):
    """Each waits a moment for one more than allowed to join it. Only more at once
    than allowed could ever come, so the most seen at once is what is allowed."""
    crowd = threading.Condition()
    state = {"now": 0, "most": 0}

    def fake(target, rules=None, **_how):
        with crowd:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
            crowd.notify_all()
            crowd.wait_for(lambda: state["now"] > 2, timeout=0.25)
            state["now"] -= 1
        return Outcome(target.path, Result.UPDATED)

    monkeypatch.setattr(submodule_update, "update", fake)

    update_all([[Target(f"/r/{n}")] for n in range(9)], at_once=2)

    assert state["most"] == 2


def test_once_stopped_none_is_begun_and_each_says_so(monkeypatch):
    events, _most = _recording(monkeypatch)
    stopped = threading.Event()
    heard = []

    def finished(row, outcome):
        heard.append(outcome.result)
        stopped.set()

    outcomes = update_all(
        [[Target(f"/r/{n}")] for n in "abcd"],
        finished=finished,
        is_cancelled=stopped.is_set,
    )

    assert [outcome.result for outcome in outcomes] == [Result.UPDATED] + [Result.NOT_RUN] * 3
    assert events == [("begin", "/r/a"), ("end", "/r/a")]
    assert sorted(heard, key=lambda result: result.value) == sorted(
        [outcome.result for outcome in outcomes], key=lambda result: result.value
    )


def test_one_submodule_s_surprise_is_not_the_others(monkeypatch):
    _recording(monkeypatch, raise_for={"/r/b"})

    outcomes = update_all([[Target(f"/r/{n}")] for n in "abc"])

    assert [outcome.result for outcome in outcomes] == [
        Result.UPDATED, Result.FAILED, Result.UPDATED
    ]
    assert outcomes[1].note == "/r/b exploded"


def test_each_is_told_when_it_begins(monkeypatch):
    _recording(monkeypatch)
    begun = []

    update_all(
        [[Target("/r/a"), Target("/r/a/in", "/r/a")], [Target("/r/b")]], started=begun.append
    )

    assert sorted(begun) == [0, 1, 2]


def test_each_submodule_is_fetched_by_its_own_settings(monkeypatch):
    from git_assistant import repo_config

    asked = []
    chosen = FetchRules(shallow=True, depth=3, prune=False, tags=False)

    def resolve(path, tier):
        asked.append((path, tier))
        return repo_config.RepoSettings(fetch=chosen)

    monkeypatch.setattr(repo_config, "resolve", resolve)
    settings = SimpleNamespace(settings_tier=lambda path: f"tier of {path}")

    assert submodule_update.fetch_rules(settings)("/r/a") is chosen
    assert asked == [("/r/a", "tier of /r/a")]


# ---- a server that turns fetches away -----------------------------------------------------
_RESET = (
    "kex_exchange_identification: read: Connection reset by peer\n"
    "fatal: Could not read from remote repository.\n\n"
    "Please make sure you have the correct access rights\nand the repository exists.\n"
)


def _turned_away(monkeypatch, times, said=_RESET):
    """``git_ops.fetch`` refused the first ``times`` times it is asked, then real."""
    real, left = git_ops.fetch, [times]

    def fetch(path, **how):
        if left[0]:
            left[0] -= 1
            return git_ops.GitResult(ok=False, stdout="", stderr=said, returncode=128)
        return real(path, **how)

    monkeypatch.setattr(git_ops, "fetch", fetch)


def test_a_fetch_the_server_turned_away_is_tried_again_after_a_wait(repos, monkeypatch, slept):
    _turned_away(monkeypatch, 2)
    newest = _commit(repos.upstream, "f.txt", "three\n")
    heard = []

    outcome = update(Target(str(repos.sub)), waiting=heard.append)

    assert (outcome.result, outcome.tries) == (Result.UPDATED, 3)
    assert _head(repos.sub) == newest
    # Up to five seconds, then up to ten: each wait longer than the last could be.
    assert len(slept) == 2 and 2.5 <= slept[0] <= 5 and 5 <= slept[1] <= 10
    assert heard == [
        "The fetch failed: waiting to try again (2 of 5)",
        "Fetching again (try 2 of 5)",
        "The fetch failed: waiting to try again (3 of 5)",
        "Fetching again (try 3 of 5)",
    ]


def test_a_fetch_refused_for_good_is_not_asked_again(repos, monkeypatch, slept):
    _turned_away(
        monkeypatch, 1, "git@server: Permission denied (publickey).\n" + _RESET.split("\n", 1)[1]
    )

    outcome = update(Target(str(repos.sub)))

    assert (outcome.result, outcome.tries) == (Result.FAILED, 1)
    assert "Permission denied" in outcome.note
    assert slept == []


def test_it_gives_up_after_so_many_tries_having_changed_nothing(repos, monkeypatch, slept):
    """And however many tries there are, no wait is longer than the longest: eight,
    here, which without one would be waiting minutes by the end."""
    monkeypatch.setattr(submodule_update, "TRIES", 8)
    _turned_away(monkeypatch, 99)
    (repos.sub / "keep.txt").write_text("edited\n", encoding="utf-8")

    outcome = update(Target(str(repos.sub)))

    assert (outcome.result, outcome.tries) == (Result.FAILED, 8)
    assert "Connection reset by peer" in outcome.note
    assert len(slept) == 7
    assert all(wait <= submodule_update.LONGEST_WAIT for wait in slept)
    assert slept[-1] >= submodule_update.LONGEST_WAIT / 2  # grown as far as it may
    assert _head(repos.sub) == repos.first and _stashes(repos.sub) == []


@pytest.mark.parametrize(
    ("said", "again"),
    [
        (_RESET, True),
        ("ssh: connect to host 172.28.17.13 port 22: Connection timed out\n", True),
        ("ssh: connect to host 172.28.17.13 port 22: Connection refused\n", True),
        ("Connection closed by 172.28.17.13 port 22\n", True),
        ("fatal: unable to access 'https://h/': The requested URL returned error: 429\n", True),
        ("remote: Too many requests\n", True),
        ("git@h: Permission denied (publickey).\n", False),
        ("remote: ERROR: The project you were looking for could not be found\n", False),
        ("fatal: repository 'https://h/r.git/' not found\n", False),
        ("fatal: '/gone' does not appear to be a git repository\n", False),
        ("Host key verification failed.\n", False),
        ("fatal: Authentication failed for 'https://h/r.git/'\n", False),
    ],
)
def test_what_is_worth_asking_again(said, again):
    result = git_ops.GitResult(ok=False, stdout="", stderr=said, returncode=128)

    assert submodule_update.worth_asking_again(result) is again


def test_a_fetch_that_worked_is_not_asked_again():
    done = git_ops.GitResult(ok=True, stdout="", stderr="Connection reset by peer", returncode=0)

    assert not submodule_update.worth_asking_again(done)


def _fetches_at_once(pace, count, *, meet=2):
    """Run ``count`` fetches at ``pace`` on threads of their own: the most at once.

    Each waits a moment for ``meet`` of them to be fetching together, so as many as
    the pace lets through at once are seen to be.
    """
    crowd = threading.Condition()
    state = {"now": 0, "most": 0}

    def fetch():
        with crowd:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
            crowd.notify_all()
            crowd.wait_for(lambda: state["now"] >= meet, timeout=0.25)
            state["now"] -= 1
        return git_ops.GitResult(ok=True, stdout="", stderr="", returncode=0)

    threads = [threading.Thread(target=pace.fetch, args=(fetch,)) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return state["most"]


def test_before_any_refusal_fetches_go_side_by_side_and_wait_for_nothing(slept):
    pace = submodule_update.Pace()

    assert _fetches_at_once(pace, 2) == 2
    assert slept == [] and not pace.slowed


def test_after_a_refusal_every_fetch_waits_it_out_and_then_they_go_one_at_a_time(slept):
    """A limit is on everybody's requests: three more asking while one waited would
    only use it up again."""
    pace = submodule_update.Pace()

    pace.refused(7.0)

    assert _fetches_at_once(pace, 3) == 1
    assert slept == [7.0, submodule_update.FIRST_SPACING, submodule_update.FIRST_SPACING]


def test_each_refusal_spaces_them_further_apart_up_to_the_longest(slept):
    ok = git_ops.GitResult(ok=True, stdout="", stderr="", returncode=0)
    pace = submodule_update.Pace()
    pace.refused(0)
    pace.refused(0)

    pace.fetch(lambda: ok)
    pace.fetch(lambda: ok)
    assert slept == [2 * submodule_update.FIRST_SPACING]

    for _ in range(10):
        pace.refused(0)
    pace.fetch(lambda: ok)
    pace.fetch(lambda: ok)
    assert slept[-1] == submodule_update.LONGEST_SPACING


def test_every_submodule_of_a_run_keeps_one_pace_and_says_what_it_waits_for(monkeypatch):
    paces, heard = [], []

    def fake(target, rules=None, *, pace=None, waiting=None):
        paces.append(pace)
        waiting(f"waiting in {target.path}")
        return Outcome(target.path, Result.UPDATED)

    monkeypatch.setattr(submodule_update, "update", fake)
    given = submodule_update.Pace()

    update_all(
        [[Target("/r/a"), Target("/r/a/in", "/r/a")], [Target("/r/b")]],
        waiting=lambda index, text: heard.append((index, text)),
        pace=given,
    )

    assert all(pace is given for pace in paces) and len(paces) == 3
    assert sorted(heard) == [
        (0, "waiting in /r/a"), (1, "waiting in /r/a/in"), (2, "waiting in /r/b")
    ]

    paces.clear()
    update_all([[Target("/r/a")], [Target("/r/b")]], waiting=lambda *_: None)
    assert paces[0] is paces[1] is not given  # a pace of its own, shared by the run
