"""What a run is about to cost, before it is allowed to start.

Every tab that spends tokens asks here first and shows the answer. The point is
not accuracy to the token -- it is that a review of forty files, or a diff that
has quietly become a map-reduce over fifteen chunks, says so *before* it runs
rather than afterwards in the usage table.

The arithmetic mirrors what each feature really does, and where it can it calls
the same helpers rather than a copy of them: the reviewer's own ``build_prompt``
sizes a review, and the commit estimate packs chunks with the same
``build_units_with_coverage``/``pack_units`` the generator uses. What it must
not do is contact the provider -- the dialog has to appear the moment the button
is pressed -- so the context window is the configured one rather than the
model's reported one, which is the only thing here that can be wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from git_assistant import git_ops, prompts, usage
from git_assistant.commit_generator import (
    DEFAULT_CONTEXT_WINDOW,
    MAP_OUTPUT_TOKENS,
    render_template,
)
from git_assistant.config import Settings
from git_assistant.diff_strategy import build_units_with_coverage, filter_files, pack_units, split_diff
from git_assistant.parallel import effective_parallel
from git_assistant.tokenizer import estimate_tokens, input_budget, reserved_output


@dataclass
class Estimate:
    """What one run is expected to send, and to be sent back."""

    feature: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""
    #: Line by line, where the numbers come from.
    lines: list[str] = field(default_factory=list)
    #: True when the input cannot be known before the run does its own work:
    #: an audit measures the repository first and only then writes about it, so
    #: the size of what each section is handed does not exist yet. Multiplying
    #: the per-call cap by the number of calls would be a "ceiling" nobody will
    #: reach, and a figure that frightens more than it informs.
    input_unknown: bool = False
    #: The most one call may carry, when the total is unknown.
    input_cap: int = 0
    #: Anything the user should know before agreeing (nothing marked, no rules).
    problem: str = ""

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def summary(self) -> str:
        if self.input_unknown:
            return (
                f"{self.calls:,} call(s), up to {self.output_tokens:,} tokens out. "
                "What goes in is not known until the repository has been "
                f"measured, and is capped at {self.input_cap:,} per call."
            )
        return (
            f"{self.calls:,} call(s), about {self.input_tokens:,} tokens in and "
            f"{self.output_tokens:,} out ({self.total:,} in total)."
        )


def _context(settings: Settings) -> int:
    """The window to plan against, without asking the provider.

    ``0`` means auto-detect, which only the provider can answer; the default is
    used instead, so an estimate is never a network round trip.
    """
    return settings.context_window or DEFAULT_CONTEXT_WINDOW


# ---- generating a commit message ---------------------------------------------------
def for_commit(settings: Settings) -> Estimate:
    """One call if the diff fits, otherwise one per chunk plus the final one."""
    out = Estimate(
        feature=usage.COMMIT,
        model=settings.active_model(),
        provider=settings.provider,
    )
    repo = settings.active_repo
    if not repo:
        out.problem = "No repository is selected."
        return out

    try:
        branch = git_ops.current_branch(repo)
        diffstat = git_ops.get_diffstat(repo, settings.diff_mode)
        raw = git_ops.get_diff(repo, settings.diff_mode)
    except git_ops.GitError as exc:
        out.problem = str(exc)
        return out
    if not raw.strip():
        mode = "staged" if settings.diff_mode == "cached" else "uncommitted"
        out.problem = f"No {mode} changes to describe in this repository."
        return out

    files, _dropped = filter_files(split_diff(raw), settings.ignore_globs)
    if not files:
        out.problem = "Every changed file was filtered out as noise."
        return out

    context = _context(settings)
    answer = reserved_output(context, settings.safety_margin)
    usable = input_budget(context, answer)
    template = settings.template_for_repo(repo)
    full = render_template(
        template, branch=branch, diffstat=diffstat, diff="\n".join(f.text for f in files)
    )
    full_tokens = estimate_tokens(prompts.COMMIT_SYSTEM) + estimate_tokens(full)

    if full_tokens <= usable:
        out.calls = 1
        out.input_tokens = full_tokens
        out.output_tokens = answer
        out.lines = [
            f"The whole diff fits: one call carrying {full_tokens:,} tokens.",
            f"Room reserved for the message: {answer:,} tokens.",
        ]
        return out

    # Too large: the same map-reduce the generator will run.
    workers = effective_parallel(settings, context)
    scaffold = prompts.MAP_SYSTEM + prompts.MAP_TEMPLATE.replace("{files}", "").replace(
        "{diff}", ""
    )
    budget = input_budget(context // workers, MAP_OUTPUT_TOKENS, estimate_tokens(scaffold))
    units, _omitted = build_units_with_coverage(files, budget, estimate_tokens)
    chunks = pack_units(units, budget, estimate_tokens)

    overhead = estimate_tokens(scaffold)
    map_in = sum(estimate_tokens(chunk) + overhead for chunk in chunks)
    # The notes are what the final call carries, and they are as long as the
    # chunks are allowed to answer.
    final_scaffold = prompts.COMMIT_SYSTEM + render_template(
        template, branch=branch, diffstat=diffstat, diff=""
    )
    final_in = min(usable, estimate_tokens(final_scaffold) + len(chunks) * MAP_OUTPUT_TOKENS)

    out.calls = len(chunks) + 1
    out.input_tokens = map_in + final_in
    out.output_tokens = len(chunks) * MAP_OUTPUT_TOKENS + answer
    out.lines = [
        f"The diff is larger than the window ({full_tokens:,} tokens against a "
        f"{usable:,} budget), so it is summarised in pieces.",
        f"{len(chunks)} chunk(s), {workers} at a time: {map_in:,} tokens in, "
        f"up to {len(chunks) * MAP_OUTPUT_TOKENS:,} out.",
        f"One final call to write the message: about {final_in:,} in, "
        f"up to {answer:,} out.",
    ]
    return out


# ---- reviewing files ----------------------------------------------------------------
def for_review(settings: Settings, repo: str, paths: list[str], table) -> Estimate:
    """One call per marked file, sized with the reviewer's own prompt builder."""
    from git_assistant.review import prompts as review_prompts
    from git_assistant.review import reviewer

    out = Estimate(
        feature=usage.REVIEW,
        model=settings.active_model(),
        provider=settings.provider,
    )
    if not repo:
        out.problem = "No repository is selected."
        return out
    if table is None or not table.rules:
        out.problem = "The selected rule table has no rules."
        return out
    if not paths:
        out.problem = "No files are marked for review."
        return out

    context = _context(settings)
    workers = max(1, min(effective_parallel(settings, context), len(paths)))
    share = context // workers
    # Sized from the marked count, as the run does before it knows which of them
    # survive; a file dropped after that only leaves more room, never less.
    scaffold = review_prompts.REVIEW_SYSTEM + review_prompts.render(
        review_prompts.REVIEW_TEMPLATE, rules="", path="", notes="", diff="", content=""
    )
    budget = input_budget(share, reviewer.REVIEW_OUTPUT_TOKENS, estimate_tokens(scaffold))

    try:
        candidates = {
            c.path: c
            for c in reviewer.staged_files(repo, settings.diff_mode, settings.ignore_globs)
        }
    except git_ops.GitError as exc:
        out.problem = str(exc)
        return out

    # The same set the run itself will take: a marked path that is no longer in
    # the diff, or is filtered as noise, is not a call.
    chosen = [candidates[p] for p in paths if p in candidates and candidates[p].reviewable]
    if not chosen:
        out.problem = (
            "None of the marked files are in the current diff. Refresh, and "
            "check they are staged."
        )
        return out

    total_in = 0
    partial = 0
    for candidate in chosen:
        path = candidate.path
        built = reviewer.build_prompt(
            path=path,
            diff=candidate.diff,
            content=git_ops.file_content(repo, path, settings.diff_mode),
            table=table,
            budget=budget,
        )
        total_in += estimate_tokens(review_prompts.REVIEW_SYSTEM) + estimate_tokens(built.user)
        if built.diff_truncated or not built.content_sent or built.content_truncated:
            partial += 1

    out.calls = len(chosen)
    out.input_tokens = total_in
    out.output_tokens = len(chosen) * reviewer.REVIEW_OUTPUT_TOKENS
    out.lines = [
        f"One call per marked file: {len(chosen)} file(s), {workers} at a time.",
        f"Each carries the {len(table.rules)} rule(s), the file's diff and the "
        "file itself.",
        f"Room reserved for the findings: up to "
        f"{reviewer.REVIEW_OUTPUT_TOKENS:,} tokens per file.",
    ]
    if partial:
        out.lines.append(
            f"{partial} file(s) will not fit whole and are cut to the budget."
        )
    out.lines.append(
        "A file whose answer cannot be read is asked once more, which is one "
        "extra call each."
    )
    return out


# ---- auditing a repository ------------------------------------------------------------
def for_audit(settings: Settings, agent_id: str, *, narrate: bool) -> Estimate:
    """The narration only: measuring a repository costs nothing but time."""
    from git_assistant.agents import prompts as agent_prompts

    out = Estimate(
        feature=usage.AUDIT,
        model=settings.active_model(),
        provider=settings.provider,
    )
    outlines = agent_prompts.OUTLINES.get(agent_id, {})
    if not narrate or not outlines:
        out.lines = [
            "Nothing is sent to the model: the report is written from the "
            "measurements alone."
        ]
        return out

    context = _context(settings)
    answer = reserved_output(context, settings.safety_margin)
    per_call = input_budget(context, answer)

    out.calls = len(outlines)
    out.output_tokens = sum(o.max_tokens() for o in outlines.values())
    # Deliberately not a number: what each section is handed is its own
    # measurements, and those do not exist until the scan has run.
    out.input_unknown = True
    out.input_cap = per_call
    out.lines = [
        f"One call per section of the report: {len(outlines)} section(s).",
        "The repository is measured first, and that costs nothing; only the "
        "prose is written by the model.",
        f"Each section is handed its own measurements, up to {per_call:,} "
        "tokens, and answers in at most "
        f"{max(o.max_tokens() for o in outlines.values()):,}.",
        "A section that quotes a figure nobody measured is asked again, which "
        "is one extra call.",
    ]
    return out
