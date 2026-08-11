"""Reviewing staged files against a rule table, one call per file.

One file per call, never several: findings are attributed to a file and a line,
and a call spanning three files invites the model to blur them. It also keeps
the tab legible -- one row per file, and the call that produced it is the one
sitting next to it in the calls pane.

Each call carries the rules, the file's diff, and the file as it will be after
the change. When they do not all fit, they are cut in that order of importance:
the rules are the question, the diff is what changed, and the content is
context for the diff. What was cut is said out loud four times over -- in the
prompt itself, on the file's row, above the findings, and in the stored run --
because a review of two thirds of a file that reports nothing looks exactly
like a clean one.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from git_assistant import git_ops, llm_log
from git_assistant.config import Settings
from git_assistant.diff_strategy import filter_files, split_diff, truncate_to_budget
from git_assistant.model_runtime import ModelRuntime
from git_assistant.parallel import CancelledError, effective_parallel, run_parallel
from git_assistant.review import languages, prompts
from git_assistant.review.parse import Finding, parse_findings, said_clean, salvage
from git_assistant.review.rules import RuleTable
from git_assistant.tokenizer import estimate_tokens, input_budget

#: Room for the answer. Findings are lines; a file that breaks a dozen rules
#: still fits, and anything longer than this is prose we did not ask for.
REVIEW_OUTPUT_TOKENS = 512

#: The most of one call's budget the rules may take. Beyond this a large table
#: would crowd out the code it is supposed to be checked against.
RULES_SHARE = 0.35

#: Below this there is no point sending file content at all.
MIN_CONTENT_TOKENS = 256

ProgressFn = Callable[[str], None]
CancelFn = Callable[[], bool]


def _noop(_: str) -> None:
    pass


def _never() -> bool:
    return False


# ---- what can be reviewed --------------------------------------------------------
@dataclass
class Candidate:
    """A changed file, and whether it can be reviewed at all."""

    path: str
    diff: str
    reviewable: bool = True
    reason: str = "staged"  # "staged" | "filtered"


def staged_files(repo: str, mode: str, ignore_globs: list[str]) -> list[Candidate]:
    """Every file in the current diff, noise included but marked as such.

    Filtered files are listed rather than left out: a file missing from the tab
    reads as a file with nothing wrong with it.
    """
    raw = git_ops.get_diff(repo, mode)
    if not raw.strip():
        return []
    all_files = split_diff(raw)
    _kept, dropped = filter_files(all_files, ignore_globs)
    dropped_set = set(dropped)
    return [
        Candidate(
            path=f.path,
            diff=f.text,
            reviewable=f.path not in dropped_set,
            reason="filtered" if f.path in dropped_set else "staged",
        )
        for f in all_files
    ]


# ---- what came back ----------------------------------------------------------------
@dataclass
class FileReview:
    """One file's review: what was found, and what the model was shown."""

    path: str
    findings: list[Finding] = field(default_factory=list)
    raw_reply: str = ""
    error: str = ""  # the call failed, or came back empty
    diff_truncated: bool = False
    content_truncated: bool = False
    content_sent: bool = True
    rules_sent: int = 0
    #: What this file was checked against. Per file, because a run spans
    #: several languages and each gets its own table at its own version.
    rules_total: int = 0
    table_name: str = ""
    language: str = ""
    version: str = ""
    retried: bool = False
    seconds: float = 0.0

    @property
    def clean(self) -> bool:
        """Nothing found, and the model actually said so."""
        return not self.findings and not self.error

    @property
    def partial(self) -> bool:
        return self.diff_truncated or self.content_truncated or not self.content_sent

    @property
    def rules_cut(self) -> bool:
        """Whether this file was judged against only part of its rules."""
        return bool(self.rules_total) and self.rules_sent < self.rules_total

    def note(self) -> str:
        """How this file's row is annotated in the tab."""
        if self.error:
            return "not reviewed"
        if self.diff_truncated:
            return "diff truncated - part of it never reached the model"
        if not self.content_sent:
            return "reviewed from the diff alone"
        if self.content_truncated:
            return "reviewed without the file's full content"
        return ""


@dataclass
class ReviewRun:
    """One review of one repository, against one profile.

    ``table_name`` is the profile the run was made with and
    ``table_fingerprint`` covers every table it used, so a stored review still
    notices that the rules were rewritten under it. What each individual file
    was checked against is on its own ``FileReview``.
    """

    repo_path: str
    table_name: str = ""
    table_fingerprint: str = ""
    rules_total: int = 0
    rules_sent: int = 0
    provider: str = ""
    model: str = ""
    diff_mode: str = "cached"
    context_window: int = 0
    started_at: str = ""
    head: str = ""
    branch: str = ""
    dirty: bool = False
    staged_total: int = 0  # files in the diff, reviewed or not
    files: list[FileReview] = field(default_factory=list)
    #: Every exchange with the model, when the client was a recording one.
    calls: list = field(default_factory=list)
    #: What the judge made of this run, when there was one. The scores are per
    #: file and `judge_score` is their mean, over the files that were actually
    #: scored -- a file whose judge failed is left out rather than counted as a
    #: zero. See git_assistant.review.judge.
    judge_provider: str = ""
    judge_model: str = ""
    judge_scores: list = field(default_factory=list)
    judge_score: float = 0.0
    judge_failed: int = 0
    #: How long the reviewer's calls took for the files that were scored, added
    #: up. Per call rather than wall clock, so it compares models rather than
    #: how many ran at once.
    judged_seconds: float = 0.0

    def judged(self) -> bool:
        return bool(self.judge_model and self.judge_scores)

    def findings(self) -> list[Finding]:
        return [f for review in self.files for f in review.findings]

    def failed(self) -> list[FileReview]:
        return [r for r in self.files if r.error]

    def rules_truncated(self) -> list[FileReview]:
        """Files judged against only part of their rules.

        Per file: with one table per language, "8 of 40 rules sent" would be
        true of neither the Python files nor the C++ ones.
        """
        return [r for r in self.files if r.rules_cut]

    def headline(self) -> dict:
        """The few numbers a list of runs needs, so drawing it opens no file."""
        return {
            "files": len(self.files),
            "findings": len(self.findings()),
            "clean": sum(1 for r in self.files if r.clean),
            "failed": len(self.failed()),
        }

    def summary(self) -> str:
        counts = self.headline()
        parts = [
            f"{counts['findings']} finding(s) in {counts['files']} file(s)",
            f"{counts['clean']} clean",
        ]
        if counts["failed"]:
            parts.append(f"{counts['failed']} not reviewed")
        cut = self.rules_truncated()
        if cut:
            parts.append(f"{len(cut)} file(s) got only part of their rules")
        return " - ".join(parts)


# ---- fitting one call ----------------------------------------------------------------
def fit_rules(table: RuleTable, budget: int) -> tuple[str, int]:
    """As many whole rules as fit, and how many that was.

    Whole rules only. ``truncate_to_budget`` keeps a head and a tail, which is
    right for a diff and wrong here: half a rule is a rule the model applies
    wrongly, and it would be the same half every call.
    """
    lines: list[str] = []
    used = 0
    for index, rule in enumerate(table.rules):
        line = rule.line()
        cost = estimate_tokens(line)
        if used + cost > budget and index > 0:
            break
        lines.append(line)
        used += cost
    omitted = len(table.rules) - len(lines)
    if omitted:
        lines.append(
            f"[{omitted} of {len(table.rules)} rules omitted to fit the model's "
            "context window]"
        )
    return "\n".join(lines), len(lines) - (1 if omitted else 0)


def _language_line(language: str, version: str) -> str:
    """What the file is, when that is known.

    Nothing is said when it is not: "version: unknown" only invites the model
    to speculate about which one it is looking at.
    """
    if not language or language == languages.label_of(languages.ANY):
        return ""
    return prompts.render(
        prompts.LANGUAGE_LINE,
        language=language,
        version=f" ({version})" if version else "",
    )


def _notes(diff_truncated: bool, content_truncated: bool, content_sent: bool) -> str:
    """What the model is told it is not being shown.

    Without this it reports "no violations" over a third of a file with total
    confidence, and there is nothing in the answer to show that is what happened.
    """
    parts = []
    if diff_truncated:
        parts.append("part of the diff below was cut to fit")
    if not content_sent:
        parts.append("the file's full content is not shown")
    elif content_truncated:
        parts.append("the file's content below was cut to fit")
    if not parts:
        return ""
    return "Note: " + "; ".join(parts) + ". Judge only what you can see.\n"


@dataclass
class _Prompt:
    user: str
    diff_truncated: bool
    content_truncated: bool
    content_sent: bool
    rules_sent: int


def build_prompt(
    *,
    path: str,
    diff: str,
    content: str,
    table: RuleTable,
    budget: int,
    language: str = "",
    version: str = "",
) -> _Prompt:
    """One file's user prompt, cut to ``budget`` in order of importance."""
    rules_text, rules_sent = fit_rules(table, max(1, int(budget * RULES_SHARE)))
    left = budget - estimate_tokens(rules_text)

    # The diff may take everything the rules left. Content is what gets
    # sacrificed to keep it whole, not the other way round: the content is
    # there to explain the diff, and explaining half a diff is worth less.
    diff_sent = truncate_to_budget(diff, max(1, left), estimate_tokens)
    diff_truncated = diff_sent != diff

    left -= estimate_tokens(diff_sent)
    content_truncated = False
    if content and left >= MIN_CONTENT_TOKENS:
        content_sent = truncate_to_budget(content, left, estimate_tokens)
        content_truncated = content_sent != content
    else:
        content_sent = ""

    notes = _notes(diff_truncated, content_truncated, bool(content_sent))
    user = prompts.render(
        prompts.REVIEW_TEMPLATE,
        rules=rules_text,
        path=path,
        language=_language_line(language, version),
        notes=notes,
        diff=diff_sent,
        content=content_sent or "(not shown)",
    )
    return _Prompt(
        user=user,
        diff_truncated=diff_truncated,
        content_truncated=content_truncated,
        content_sent=bool(content_sent),
        rules_sent=rules_sent,
    )


# ---- the run --------------------------------------------------------------------------
def review(
    settings: Settings,
    client,
    *,
    plan,
    progress: ProgressFn = _noop,
    is_cancelled: CancelFn = _never,
    judge_client=None,
) -> ReviewRun:
    """Carry out ``plan``: one call per reviewable file, with its own rules.

    The plan decided which files, which language, which version and which rules
    (see ``review.plan``). Nothing is decided again here -- what was shown in
    the window before the run is what the run does.
    """
    repo = plan.repo
    if not repo:
        raise ValueError("No repository is selected.")
    if not settings.active_model():
        raise ValueError("No model is selected. Open Settings and pick one.")

    chosen = plan.reviewable()
    if not chosen:
        raise ValueError(
            "Nothing in this plan can be reviewed: no marked file has rules "
            "that apply to it."
        )

    runtime = ModelRuntime(settings, client)
    context = runtime.context_window()
    # Never more slots than there are files: the window is divided between the
    # requests actually in flight, and reviewing two files four-ways would
    # throw away half the context each of them could have had.
    workers = max(1, min(effective_parallel(settings, context), len(chosen)))
    share = context // workers
    scaffold = prompts.REVIEW_SYSTEM + prompts.render(
        prompts.REVIEW_TEMPLATE, rules="", path="", notes="", diff="", content=""
    )
    budget = input_budget(share, REVIEW_OUTPUT_TOKENS, estimate_tokens(scaffold))

    head = git_ops._run(repo, ["rev-parse", "HEAD"])
    run = ReviewRun(
        repo_path=repo,
        table_name=plan.profile,
        table_fingerprint=plan.fingerprint(),
        rules_total=sum(len(f.table.rules) for f in chosen if f.table),
        rules_sent=0,  # summed from the files once they come back
        provider=settings.provider,
        model=settings.active_model(),
        diff_mode=settings.diff_mode,
        context_window=context,
        started_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        head=head.stdout.strip() if head.ok else "",
        branch=git_ops.current_branch(repo),
        dirty=git_ops.has_uncommitted_changes(repo),
        staged_total=len(plan.files),
    )

    # One phase for the whole fan-out: the calls run on several threads, and a
    # phase set per call would be a race for no gain -- they are all the same.
    if hasattr(client, "phase"):
        client.phase = llm_log.REVIEW

    def review_one(file):
        return _review_file(settings, client, repo=repo, file=file, budget=budget)

    reviewed = run_parallel(
        chosen,
        review_one,
        workers=workers,
        cold_start=runtime.is_cold(),
        progress=progress,
        is_cancelled=is_cancelled,
        label="reviewing",
    )
    # The prompt travels beside the review rather than on it: `FileReview` is
    # `asdict`-ed straight into the stored run, and history deliberately keeps
    # no prompts -- forty of them a run is text nobody reads twice.
    run.files = [one.review for one in reviewed]
    run.rules_sent = sum(r.rules_sent for r in run.files)

    if plan.judged():
        _judge_run(
            run,
            reviewed,
            judge=plan.judge,
            client=judge_client if judge_client is not None else client,
            workers=workers,
            progress=progress,
            is_cancelled=is_cancelled,
        )
    # Files the plan could not review are recorded too: one left out of the
    # results entirely reads as a file with nothing wrong with it.
    run.files += [
        FileReview(path=f.path, error=f.skipped or "not reviewed")
        for f in plan.skipped()
    ]
    return run


@dataclass
class _Reviewed:
    """One file's review, and the prompt that produced it.

    The prompt is what a judge has to be shown, and it must not end up on
    `FileReview` -- that is stored, and storing prompts is exactly what
    `review.history` refuses to do.
    """

    review: FileReview
    prompt: str = ""


def _judge_run(
    run: ReviewRun,
    reviewed: list["_Reviewed"],
    *,
    judge,
    client,
    workers: int,
    progress: ProgressFn,
    is_cancelled: CancelFn,
) -> None:
    """Score each answer, and record what the judge thought of the run.

    A second pass rather than a second call inside the first: the reviewer's
    fan-out is sized to the reviewer's context window, and the judge is usually
    a different model with a different one. Running it separately also means a
    judge that falls over costs the run its scores and nothing else -- the
    findings are already in `run.files` by the time this is reached.

    Files whose review failed outright are not scored. There is no answer to
    grade, and a zero for them would file the reviewer's crash as its opinion.
    """
    from git_assistant.review import judge as judge_mod

    if hasattr(client, "phase"):
        client.phase = llm_log.JUDGE

    gradeable = [one for one in reviewed if not one.review.error and one.prompt]
    if not gradeable:
        return

    def judge_one(item: "_Reviewed"):
        try:
            reply = client.chat(
                model=judge.model,
                system=prompts.JUDGE_SYSTEM,
                user=judge_mod.build_prompt(
                    judge.prompt, prompt=item.prompt, reply=item.review.raw_reply
                ),
                max_tokens=judge_mod.JUDGE_OUTPUT_TOKENS,
                temperature=judge.temperature,
            )
        except CancelledError:
            raise
        except Exception as exc:
            return judge_mod.Verdict(error=f"{type(exc).__name__}: {exc}")
        return judge_mod.parse_verdict(reply)

    verdicts = run_parallel(
        gradeable,
        judge_one,
        workers=workers,
        # The judge is a different model and may well be cold, but the reviewer
        # has already paid for one slow first call and the honest place to warm
        # a second model is its own first call, not a serialised pass.
        cold_start=False,
        progress=progress,
        is_cancelled=is_cancelled,
        label="scoring",
        prefix="judging: ",
    )

    # `run_parallel` keeps input order, so a verdict belongs to the file at the
    # same index. Pairing them is what lets the time and the score describe the
    # same set of files: a mean time over files that were never scored would
    # not be about the number beside it.
    scored = [(item, one) for item, one in zip(gradeable, verdicts) if one.scored]

    run.judge_provider = judge.provider
    run.judge_model = judge.model
    run.judge_scores = [one.score for _, one in scored]
    run.judge_score = judge_mod.mean_of(verdicts)
    run.judge_failed = sum(1 for one in verdicts if not one.scored)
    # Per-call time, summed -- not wall clock. The files are reviewed several
    # at a time, so the elapsed time of a run says more about how many workers
    # it had than about the model; the time each call took does not.
    run.judged_seconds = round(sum(item.review.seconds for item, _ in scored), 3)


def _review_file(
    settings: Settings,
    client,
    *,
    repo: str,
    file,
    budget: int,
) -> _Reviewed:
    """One file, one call -- and one retry when the answer is unreadable.

    ``file`` is a ``plan.FilePlan``: it already carries the language, the
    version and the rules this file is checked against.

    Every failure is caught and recorded rather than raised: one file's broken
    call must not throw away what the other thirty found.
    """
    path = file.path
    table = file.table
    content = git_ops.file_content(repo, path, settings.diff_mode)
    built = build_prompt(
        path=path,
        diff=file.candidate.diff,
        content=content,
        table=table,
        budget=budget,
        language=file.language_label(),
        version=file.version_label(),
    )
    outcome = FileReview(
        path=path,
        diff_truncated=built.diff_truncated,
        content_truncated=built.content_truncated,
        content_sent=built.content_sent,
        rules_sent=built.rules_sent,
        rules_total=len(table.rules),
        table_name=table.name,
        language=file.language,
        version=file.version,
    )

    started = time.monotonic()

    def ask(user: str) -> str:
        return client.chat(
            model=settings.active_model(),
            system=prompts.REVIEW_SYSTEM,
            user=user,
            max_tokens=REVIEW_OUTPUT_TOKENS,
        )

    try:
        reply = ask(built.user)
        outcome.raw_reply = reply
        outcome.findings = parse_findings(reply, table, path)
        if not outcome.findings and not said_clean(reply):
            if not (reply or "").strip():
                # Not a clean file: a file nobody looked at.
                outcome.error = "the model returned nothing"
            else:
                outcome.retried = True
                retry = built.user + prompts.render(
                    prompts.REVIEW_RETRY_SUFFIX, reply=" ".join(reply.split())[:200]
                )
                second = ask(retry)
                outcome.raw_reply = second or reply
                outcome.findings = parse_findings(second, table, path)
                if not outcome.findings and not said_clean(second):
                    outcome.findings = [salvage(second or reply, path)]
    except CancelledError:
        raise
    except Exception as exc:
        outcome.error = f"{type(exc).__name__}: {exc}"
    outcome.seconds = time.monotonic() - started
    return _Reviewed(review=outcome, prompt=built.user)
