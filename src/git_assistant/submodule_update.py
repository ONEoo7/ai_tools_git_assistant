"""A repository's submodules brought to the newest commit of their remote's master.

Asked for from the repository list, by right-clicking a repository's **Submodules**
row. Every submodule listed under it is brought up, a submodule inside another one
after the one it is in:

1. **Fetched**, from the remote its master tracks -- or ``origin``, or the only one
   there is -- as the Fetch button fetches it, by that submodule's own settings.
2. **Checked**, and left exactly as it was when the remote has no master; when its
   own master has commits the remote lacks *and* the remote has new ones, which is a
   merge or a rebase for somebody to decide; or when its checked-out commit is on no
   branch, and switching would leave it behind -- which git only warns about.
3. **Stashed**, when anything in it is changed or untracked: a stash of its own in
   each, named so it can be told apart from the user's.
4. **Switched** to master -- made from the remote's and tracking it, where there is
   no local one -- and brought forward to the remote's.

Nothing is changed in a submodule before every check has passed, and one whose fetch
fails is not touched at all. The submodules inside each are left where they are
whatever ``submodule.recurse`` says: they are listed too, and brought up in turn.

**A server that turns fetches away** -- fifty of them in a minute is what a limit on
connections is there to stop -- is asked again, after a wait that grows each time.
One refusal slows every fetch after it, not only the one refused: all of them wait
it out, and from then on they go one at a time, further apart after each refusal
since. See `Pace`. A fetch refused for good -- no such repository, no access to it --
is not asked again: the answer would be the same.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from git_assistant import git_ops, ratelimit, repo_config
from git_assistant.git_ops import CommitSummary, GitResult
from git_assistant.repo_config import FetchRules

#: The branch every submodule is brought to. Named in the menu, so it is not a
#: setting in disguise.
BRANCH = "master"

#: How what this leaves in a repository is signed: each stash, and the entry in a
#: branch's reflog for a branch it moved.
SIGNED = "Git Assistant"

#: What each stash is called. ``git stash list`` shows it after the branch it was
#: made on: ``stash@{0}: On dev/x: Git Assistant: ...``.
STASH_MESSAGE = f"{SIGNED}: put away before switching to the latest {BRANCH}"

#: Submodules brought up at once. A fetch spends most of its time waiting on a
#: server, so this is most of the speed -- and more than a few at a time, to one
#: server, starts to look like a flood.
AT_ONCE = 4

#: How many times a fetch the server turned away is tried in all.
TRIES = 5
#: The waits between them: up to this long after the first refusal, then twice
#: that, and so on to the longest. Each is jittered, so the fetches turned away
#: together do not all ask again together.
FIRST_WAIT = 5.0
LONGEST_WAIT = 60.0
#: Once a server has turned one away: at least this long from the end of one fetch
#: to the start of the next, twice that after each refusal since, up to the longest.
FIRST_SPACING = 1.0
LONGEST_SPACING = 10.0

#: What git says when asking again would get the same answer: no such repository,
#: no access to it, a host key nobody has accepted, credentials it cannot ask for.
#: Anything else a failed fetch says -- a connection reset, closed, refused or timed
#: out, a server too busy -- is worth asking again, a little later.
_FOR_GOOD = (
    "permission denied",
    "authentication failed",
    "access denied",
    "host key verification failed",
    "could not read username",
    "could not read password",
    "invalid username or password",
    "not found",  # "repository '...' not found", and a 404 with it
    "could not be found",
    "does not appear to be a git repository",
    "not a git repository",
    "does not exist",
    "no such file or directory",
)


class Result(Enum):
    UPDATED = "Updated"
    UP_TO_DATE = "Up to date"
    #: Left exactly as it was, for a reason the note gives.
    SKIPPED = "Skipped"
    #: Git refused part of the way; the note says what, and where that left it.
    FAILED = "Failed"
    #: Stopped before its turn came.
    NOT_RUN = "Not run"


@dataclass(frozen=True)
class Target:
    """A submodule to bring up, and the repository it is a submodule of."""

    path: str
    superproject: str = ""


@dataclass(frozen=True)
class Outcome:
    """What became of one submodule."""

    path: str
    result: Result
    #: The branch it was on, or "" for a checkout at a commit.
    was_branch: str = ""
    #: The commit it was on, where there was one to read.
    was: CommitSummary | None = None
    #: The commit it is on now; None when nothing moved it, or it could not be read.
    now: CommitSummary | None = None
    #: Its changes are in a new stash.
    stashed: bool = False
    #: Why it was skipped or failed, or something else worth knowing. The first line
    #: is the sentence; git's own words, where there are any, follow it.
    note: str = ""
    #: How many times it was fetched: more than once where the server turned it away.
    tries: int = 0


class Pace:
    """How fast fetches go: shared by every thread bringing one list of submodules up.

    As many at once as `update_all` runs, to begin with. The first fetch a server
    turns away says it takes fewer than that. From then on they go one at a time,
    `FIRST_SPACING` apart and twice as far after each refusal since -- and none at
    all until the wait after a refusal is up, for every thread rather than only the
    one refused. A limit is on everybody's requests: waiting it out alone while
    three others went on asking would only use it up again.

    Kept for as long as the window that asked is open, so fetching the failed ones
    again goes at the pace the server has been found to take.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        #: Held by the one fetch going, once they go one at a time.
        self._alone = threading.Lock()
        self._next_at = 0.0
        self._spacing = 0.0

    @property
    def slowed(self) -> bool:
        return self._spacing > 0

    def fetch(self, run: Callable[[], GitResult], turn: Callable[[], None] | None = None):
        """``run`` a fetch when its turn comes; ``turn`` is told just before."""
        if not self.slowed:
            self._wait()
            if turn is not None:
                turn()
            return run()
        with self._alone:
            self._wait()
            if turn is not None:
                turn()
            try:
                return run()
            finally:
                with self._lock:
                    self._next_at = max(self._next_at, ratelimit.monotonic() + self._spacing)

    def refused(self, wait: float) -> None:
        """A server turned a fetch away: nobody fetches for ``wait`` seconds, then slower."""
        with self._lock:
            self._spacing = min(LONGEST_SPACING, max(FIRST_SPACING, self._spacing * 2))
            self._next_at = max(self._next_at, ratelimit.monotonic() + wait)

    def _wait(self) -> None:
        with self._lock:
            delay = self._next_at - ratelimit.monotonic()
        if delay > 0:
            ratelimit.sleep(delay)


def worth_asking_again(result: GitResult) -> bool:
    """Whether a failed fetch could go another way a little later. See `_FOR_GOOD`."""
    said = f"{result.stderr}\n{result.stdout}".lower()
    return not result.ok and not any(marker in said for marker in _FOR_GOOD)


def update(
    target: Target,
    rules: FetchRules | None = None,
    *,
    pace: Pace | None = None,
    waiting: Callable[[str], None] | None = None,
) -> Outcome:
    """Bring one submodule to the newest ``BRANCH`` of its remote. See the module.

    ``pace`` is shared by every submodule of a run, so a refusal slows them all;
    ``waiting`` is told when a fetch the server turned away is waiting to be tried
    again, and when it is.
    """
    path = target.path
    rules = rules if rules is not None else FetchRules()
    branch = git_ops.head_branch(path)
    was = git_ops.commit_summary(path, "HEAD")
    tries = 0

    def outcome(result: Result, note: str = "", **rest) -> Outcome:
        return Outcome(path, result, branch, was, note=note, tries=tries, **rest)

    if not git_ops.has_git_dir(path):
        return outcome(Result.SKIPPED, "There is no checkout of it here.")
    # The first thing git is asked here, so a repository it will not work in --
    # over who owns it, say -- is reported in git's words, not as "no remote".
    # Not asked again after the fetch: a file changed meanwhile is carried into
    # master by the switch, or makes git refuse it, and either way nothing is lost.
    changes = git_ops.changes_to_stash(path)
    if not changes.ok:
        return outcome(Result.FAILED, _said(changes, "Git cannot work in it."))
    remote = git_ops.pull_remote(path, BRANCH)
    if not remote:
        return outcome(Result.SKIPPED, "It has no remote to fetch from.")
    fetched, tries = _fetch(path, remote, rules, pace if pace is not None else Pace(), waiting)
    if not fetched.ok:
        return outcome(Result.FAILED, _said(fetched, f"Could not fetch from {remote}."))

    theirs = f"refs/remotes/{remote}/{BRANCH}"
    newest = git_ops.resolve_commit(path, theirs)
    if not newest:
        return outcome(Result.SKIPPED, f"{remote} has no {BRANCH} branch.")
    mine = git_ops.resolve_commit(path, f"refs/heads/{BRANCH}")
    ahead = behind = 0
    if mine:
        counts = git_ops.ahead_behind(path, f"refs/heads/{BRANCH}", theirs)
        if counts is None:
            return outcome(Result.FAILED, f"Could not compare {BRANCH} with {remote}/{BRANCH}.")
        ahead, behind = counts
        if ahead and behind:
            return outcome(
                Result.SKIPPED,
                f"Its {BRANCH} has {_commits(ahead)} {remote} does not, and {remote} has "
                f"{behind} new: merge or rebase it yourself.",
            )
    if not branch and was is not None:
        alone = git_ops.commits_on_no_branch(path, kept=_recorded(target))
        if alone is None:
            return outcome(
                Result.FAILED, "Could not tell whether the commit it is on is on a branch."
            )
        if alone:
            it, are = ("it", "is") if alone == 1 else ("them", "are")
            return outcome(
                Result.SKIPPED,
                f"{_commits(alone)} here {are} on no branch, and switching would leave "
                f"{it} behind: put {it} on a branch first.",
            )

    stashed = False
    if changes.stdout.strip():
        put = git_ops.stash_changes(path, STASH_MESSAGE)
        if not put.ok:
            return outcome(Result.FAILED, _said(put, "Could not stash its changes."))
        stashed = True

    if branch != BRANCH:
        if mine and behind:
            # Moved before it is checked out, so the files are written once.
            moved = git_ops.move_branch(
                path,
                BRANCH,
                newest,
                expected=mine,
                why=f"{SIGNED}: fast-forward to {remote}/{BRANCH}",
            )
            if not moved.ok:
                return outcome(
                    Result.FAILED,
                    _said(moved, f"Could not bring {BRANCH} up to {remote}/{BRANCH}."),
                    stashed=stashed,
                )
        if mine:
            switched = git_ops.switch_branch(path, BRANCH, leave_submodules=True)
        else:
            switched = git_ops.track_branch(path, BRANCH, remote, leave_submodules=True)
        if not switched.ok:
            return outcome(
                Result.FAILED,
                _said(switched, f"Could not switch to {BRANCH}."),
                stashed=stashed,
            )
    elif behind:
        forward = git_ops.fast_forward(path, theirs)
        if not forward.ok:
            return outcome(
                Result.FAILED,
                _said(forward, f"On {BRANCH}, but could not bring it up to {remote}/{BRANCH}."),
                stashed=stashed,
            )

    now = git_ops.commit_summary(path, "HEAD")
    if now is None:
        return outcome(
            Result.FAILED, f"On {BRANCH}, but its commit could not be read.", stashed=stashed
        )
    same = branch == BRANCH and was is not None and was.hash == now.hash
    note = f"Its {BRANCH} has {_commits(ahead)} not on {remote} yet." if ahead else ""
    return outcome(
        Result.UP_TO_DATE if same else Result.UPDATED, note, now=now, stashed=stashed
    )


def update_all(
    chains: list[list[Target]],
    *,
    rules_for: Callable[[str], FetchRules] | None = None,
    started: Callable[[int], None] | None = None,
    finished: Callable[[int, Outcome], None] | None = None,
    waiting: Callable[[int, str], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
    at_once: int = AT_ONCE,
    pace: Pace | None = None,
) -> list[Outcome]:
    """Bring up every target: each chain in order, the chains side by side.

    Outcomes come back in the order the targets were given, chain after chain.
    ``started``, ``finished`` and ``waiting`` are told each target's place in that
    order -- as it begins, as it ends, and what its fetch is waiting for when the
    server turned it away -- from whichever thread did it. Once ``is_cancelled``
    says so, a target not yet begun is not begun: it comes back as ``NOT_RUN``.

    The first chain runs on its own. A fetch that needs a password asks for it
    once, and the fetches after it find it remembered, rather than every one of
    them asking at the same moment. Every fetch keeps the one ``pace``: a new one
    unless given, as a window fetching the failed ones again gives the one it had.
    """
    pace = pace if pace is not None else Pace()
    numbered: list[list[tuple[int, Target]]] = []
    count = 0
    for chain in chains:
        if chain:
            numbered.append(list(enumerate(chain, start=count)))
            count += len(chain)
    outcomes: list[Outcome | None] = [None] * count

    def run(chain: list[tuple[int, Target]]) -> None:
        for number, target in chain:
            if is_cancelled is not None and is_cancelled():
                outcome = Outcome(target.path, Result.NOT_RUN)
            else:
                if started is not None:
                    started(number)
                try:
                    outcome = update(
                        target,
                        rules_for(target.path) if rules_for else None,
                        pace=pace,
                        waiting=_for_one(waiting, number),
                    )
                except Exception as exc:  # one submodule's surprise is not the others'
                    outcome = Outcome(target.path, Result.FAILED, note=str(exc) or repr(exc))
            outcomes[number] = outcome
            if finished is not None:
                finished(number, outcome)

    if numbered:
        run(numbered[0])
    rest = numbered[1:]
    if rest:
        with ThreadPoolExecutor(max_workers=max(1, min(at_once, len(rest)))) as pool:
            list(pool.map(run, rest))
    return [outcome for outcome in outcomes if outcome is not None]


def _for_one(
    waiting: Callable[[int, str], None] | None, number: int
) -> Callable[[str], None] | None:
    """``waiting``, for the one target at ``number``: told only what it waits for."""
    if waiting is None:
        return None
    return lambda text: waiting(number, text)


def _fetch(
    path: str,
    remote: str,
    rules: FetchRules,
    pace: Pace,
    waiting: Callable[[str], None] | None,
) -> tuple[GitResult, int]:
    """Fetch; and again, after a wait, while the server turns it away. And the tries."""

    def say(text: str) -> None:
        if waiting is not None:
            waiting(text)

    def fetch() -> GitResult:
        return git_ops.fetch(
            path,
            remote=remote,
            depth=rules.effective_depth(),
            prune=rules.prune,
            tags=rules.tags,
        )

    fetched = GitResult(ok=False, stdout="", stderr="", returncode=1)
    for tries in range(1, TRIES + 1):
        again = f"Fetching again (try {tries} of {TRIES})"
        fetched = pace.fetch(fetch, turn=(lambda: say(again)) if tries > 1 else None)
        if fetched.ok or tries == TRIES or not worth_asking_again(fetched):
            return fetched, tries
        pace.refused(ratelimit.backoff(tries - 1, base=FIRST_WAIT, cap=LONGEST_WAIT))
        say(f"The fetch failed: waiting to try again ({tries + 1} of {TRIES})")
    return fetched, TRIES


def fetch_rules(settings) -> Callable[[str], FetchRules]:
    """How each submodule is fetched: as the Fetch button would, by its own settings."""

    def rules_for(path: str) -> FetchRules:
        return repo_config.resolve(path, settings.settings_tier(path)).fetch

    return rules_for


def _recorded(target: Target) -> tuple[str, ...]:
    """The commit the superproject records for ``target``, as a tuple of one or none."""
    if not target.superproject:
        return ()
    try:
        inside = os.path.relpath(target.path, target.superproject)
    except ValueError:  # another drive: not inside it at all
        return ()
    recorded = git_ops.recorded_commit(target.superproject, Path(inside).as_posix())
    return (recorded,) if recorded else ()


def _commits(count: int) -> str:
    return f"{count} commit" + ("" if count == 1 else "s")


def _said(result: GitResult, sentence: str) -> str:
    """``sentence``, and then what git said about it, where it said anything."""
    detail = (result.stderr.strip() or result.stdout.strip()).strip()
    return f"{sentence}\n{detail}" if detail else sentence
