"""Orchestrates commit-message generation with the hybrid overflow strategy.

Flow:
1. Read diff + diffstat + branch from the active repo.
2. Filter noise files.
3. If the whole prompt fits the input budget -> single-shot generation.
4. Otherwise -> map-reduce: summarize each budget-sized chunk ("map"), condense
   the notes if they themselves overflow ("reduce"), then synthesize the final
   Conventional-Commits message from the notes.

Rendering avoids ``str.format`` so literal braces in a diff never raise.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from git_assistant import commit_style, git_ops, llm_log, prompts
from git_assistant.config import Settings
from git_assistant.diff_strategy import (
    build_units_with_coverage,
    filter_files,
    pack_units,
    split_diff,
    truncate_to_budget,
)
from git_assistant.llm import ModelInfo
from git_assistant.lmstudio_client import LMStudioClient
from git_assistant.parallel import (
    MIN_PARALLEL_CONTEXT,
    CancelledError,
    effective_parallel,
    per_request_context,
    run_parallel,
)
from git_assistant.tokenizer import (
    estimate_tokens,
    input_budget,
    reserved_output,
)

DEFAULT_CONTEXT_WINDOW = 8192
MAP_OUTPUT_TOKENS = 384
MAX_REDUCE_DEPTH = 3

ProgressFn = Callable[[str], None]
CancelFn = Callable[[], bool]

# MIN_PARALLEL_CONTEXT and CancelledError now live in git_assistant.parallel,
# which the code reviewer fans out through as well. They are re-exported here
# because that is where the settings tab and the workers import them from.
__all__ = [
    "CancelledError",
    "CommitGenerator",
    "FileCoverage",
    "GenerationResult",
    "MIN_PARALLEL_CONTEXT",
    "render_template",
]


@dataclass
class FileCoverage:
    """What of a file's diff actually reached the model."""

    path: str
    lines: list[str]  # the file's diff lines (keepends)
    omitted: set[int]  # indices of lines NOT sent to the model
    # "sent"       - raw diff went to the model verbatim (single-shot)
    # "summarized" - fully sent, but only via a map-reduce summary pass
    # "truncated"  - part of the file never reached the model
    # "filtered"   - dropped as noise before the prompt was built
    reason: str

    @property
    def omitted_count(self) -> int:
        return len(self.omitted)

    @property
    def fully_sent(self) -> bool:
        return not self.omitted


@dataclass
class Retry:
    """Everything needed to write the message again, and nothing more.

    The *last* prompt of the run, kept verbatim. For a single-shot generation
    that is the whole thing; for a map-reduce one it is the synthesis prompt --
    the notes, already summarised -- so asking again costs one call instead of
    fifteen, and the chunks are never re-read.

    A retry differs from the attempt before it by its prompt, not by its
    sampling: the reason the first answer was rejected is quoted back into it.
    That matters at a low temperature, where asking the same question again
    would fairly reliably produce the same answer.
    """

    system: str
    user: str
    max_tokens: int
    #: How many calls the original run took, so the offer can say what is being
    #: skipped: "1 call, not the 14 that produced this".
    calls_before: int = 1

    def with_note(self, note: str) -> str:
        """The prompt again, with why the last answer would not do."""
        return f"{self.user.rstrip()}\n\n{note}\n" if note else self.user

    def input_tokens(self, note: str = "") -> int:
        return estimate_tokens(self.system) + estimate_tokens(self.with_note(note))


@dataclass
class GenerationResult:
    message: str
    strategy: str  # "single-shot" | "map-reduce"
    context_window: int
    input_budget: int
    input_tokens: int
    num_chunks: int = 1
    dropped_files: list[str] = field(default_factory=list)
    file_coverage: list[FileCoverage] = field(default_factory=list)
    #: Every exchange with the model, when the client was a recording one.
    calls: list = field(default_factory=list)
    #: Chunks whose summary came back empty, so their changes reached the final
    #: message through nothing at all.
    blank_notes: int = 0
    #: What it would take to ask for the message again; see ``Retry``.
    retry: Retry | None = None


def render_template(template: str, *, branch: str, diffstat: str, diff: str) -> str:
    """Substitute placeholders without str.format (safe for braces in code)."""
    return (
        template.replace("{branch}", branch)
        .replace("{diffstat}", diffstat)
        .replace("{diff}", diff)
    )


def _noop(_: str) -> None:  # default progress sink
    pass


def _never() -> bool:
    return False


class CommitGenerator:
    def __init__(self, settings: Settings, client: LMStudioClient) -> None:
        self.settings = settings
        self.client = client

    def _template(self) -> str:
        """The prompt template for the active repository, plus the length rules.

        Each project can carry its own; repositories without one fall back to
        the default template.

        The length rules are appended here rather than written into the
        templates, so a template someone saved last year is held to the limits
        set today -- and so every path that renders the template (the single
        shot, the final synthesis, and both scaffolds the budget is measured
        against) counts them. See git_assistant.commit_style.
        """
        return commit_style.with_rules(
            self.settings.template_for_repo(self.settings.active_repo),
            commit_style.Limits.of(self.settings),
        )

    # ---- what the server says about the model ------------------------------
    def _model_report(self) -> ModelInfo | None:
        """The listing entry for the selected model, looked up once per run.

        Both the context window and the load state come from the same listing,
        and a generator is built per generation, so one call answers both.
        """
        if not self._model_looked_up:
            self._model_looked_up = True
            wanted = self.settings.active_model()
            try:
                self._model = next(
                    (m for m in self.client.list_models() if m.id == wanted), None
                )
            except Exception:
                self._model = None
        return self._model

    def _model_is_cold(self) -> bool:
        """True when the server has the model available but not loaded yet.

        A provider that cannot report this (hosted models, or a listing that
        failed) is treated as ready: there is nothing to wait for.
        """
        model = self._model_report()
        return model is not None and not model.loaded

    # ---- budget helpers ----------------------------------------------------
    def _detected_context(self) -> int | None:
        """Model's real loaded context, or None if it can't be determined."""
        model = self._model_report()
        if model is not None:
            return model.max_context_length
        try:
            return self.client.context_length_for(self.settings.active_model())
        except Exception:
            return None

    def _context_window(self) -> int:
        s = self.settings
        detected = self._detected_context()
        if s.context_window and s.context_window > 0:
            # Never plan beyond the model's real context: a larger value would
            # just make LM Studio silently truncate our prompt. Clamp.
            if detected and s.context_window > detected:
                return detected
            return s.context_window
        return detected or DEFAULT_CONTEXT_WINDOW

    def _reserved_output(self, context: int) -> int:
        """Tokens carved out of the window for the model's reply."""
        return reserved_output(context, self.settings.safety_margin)

    def _usable(self, context: int) -> int:
        """Tokens available for the whole input prompt (final generation)."""
        return input_budget(context, self._reserved_output(context))

    # ---- public entry point ------------------------------------------------
    def generate(
        self,
        *,
        progress: ProgressFn = _noop,
        is_cancelled: CancelFn = _never,
    ) -> GenerationResult:
        s = self.settings
        repo = s.active_repo
        if not repo:
            raise ValueError("No active repository is selected.")
        if not s.active_model():
            raise ValueError("No model is selected. Open Settings and pick one.")

        progress("Reading git diff...")
        branch = git_ops.current_branch(repo)
        diffstat = git_ops.get_diffstat(repo, s.diff_mode)
        raw_diff = git_ops.get_diff(repo, s.diff_mode)
        if not raw_diff.strip():
            mode = "staged" if s.diff_mode == "cached" else "uncommitted"
            raise ValueError(f"No {mode} changes to describe in this repository.")

        all_files = split_diff(raw_diff)
        files, dropped = filter_files(all_files, s.ignore_globs)
        if not files:
            raise ValueError(
                "All changed files were filtered out as noise "
                "(lockfiles/binaries). Adjust ignore globs in Settings."
            )
        filtered_diff = "\n".join(f.text for f in files)
        # Files removed by the noise filter never reach the model at all.
        filtered_coverage = [
            FileCoverage(
                path=f.path,
                lines=f.text.splitlines(keepends=True),
                omitted=set(range(len(f.text.splitlines(keepends=True)))),
                reason="filtered",
            )
            for f in all_files
            if f.path in set(dropped)
        ]

        context = self._context_window()
        usable = self._usable(context)
        out_tokens = self._reserved_output(context)
        # Concurrency is bounded by the context: parallel slots share the window.
        self._workers = self.effective_parallel(context)
        # A model the server has not loaded yet cannot take a fan-out; see
        # _run_parallel. Read before any request, since the first one loads it.
        self._cold_start = self._model_is_cold()

        # Does the full single-shot prompt fit?
        full_prompt = render_template(
            self._template(), branch=branch, diffstat=diffstat, diff=filtered_diff
        )
        full_tokens = estimate_tokens(prompts.COMMIT_SYSTEM) + estimate_tokens(
            full_prompt
        )
        self._check_cancel(is_cancelled)

        if full_tokens <= usable:
            progress("Diff fits context - generating (single-shot)...")
            self._phase(llm_log.SINGLE)
            message = self.client.chat(
                model=s.active_model(),
                system=prompts.COMMIT_SYSTEM,
                user=full_prompt,
                max_tokens=out_tokens,
            )
            # Single-shot: every kept file was sent in full.
            coverage = [
                FileCoverage(
                    path=f.path,
                    lines=f.text.splitlines(keepends=True),
                    omitted=set(),
                    reason="sent",
                )
                for f in files
            ] + filtered_coverage
            return GenerationResult(
                message=message,
                strategy="single-shot",
                context_window=context,
                input_budget=usable,
                input_tokens=full_tokens,
                dropped_files=dropped,
                file_coverage=coverage,
                retry=Retry(
                    system=prompts.COMMIT_SYSTEM,
                    user=full_prompt,
                    max_tokens=out_tokens,
                ),
            )

        # --- Overflow: map-reduce ------------------------------------------
        requested = max(1, int(s.parallel_calls or 1))
        capped = (
            f" (parallel capped {requested}->{self._workers} to fit context)"
            if self._workers < requested
            else ""
        )
        progress(
            f"Diff too large ({full_tokens} > {usable} tokens) - "
            f"switching to map-reduce{capped}..."
        )
        notes, omitted_by_path = self._map(
            files, context, branch, diffstat, progress, is_cancelled
        )
        notes = self._reduce_if_needed(
            notes, context, branch, diffstat, progress, is_cancelled
        )

        combined = "\n".join(notes)
        # Ensure the final prompt fits by hard-truncating the notes if needed.
        final_scaffold = prompts.COMMIT_SYSTEM + render_template(
            self._template(), branch=branch, diffstat=diffstat, diff=""
        )
        content_budget = max(256, usable - estimate_tokens(final_scaffold))
        combined = truncate_to_budget(combined, content_budget, estimate_tokens)

        final_prompt = render_template(
            self._template(), branch=branch, diffstat=diffstat, diff=combined
        )
        progress("Synthesizing final commit message...")
        self._check_cancel(is_cancelled)
        self._phase(llm_log.FINAL)
        message = self.client.chat(
            model=s.active_model(),
            system=prompts.COMMIT_SYSTEM,
            user=final_prompt,
            max_tokens=out_tokens,
        )
        coverage = []
        for f in files:
            omitted = omitted_by_path.get(f.path, set())
            coverage.append(
                FileCoverage(
                    path=f.path,
                    lines=f.text.splitlines(keepends=True),
                    omitted=omitted,
                    # Even when nothing is dropped, map-reduce reaches the model
                    # as a summary rather than the raw diff.
                    reason="truncated" if omitted else "summarized",
                )
            )
        coverage += filtered_coverage
        return GenerationResult(
            message=message,
            strategy="map-reduce",
            context_window=context,
            input_budget=usable,
            input_tokens=estimate_tokens(prompts.COMMIT_SYSTEM)
            + estimate_tokens(final_prompt),
            num_chunks=self._last_chunk_count,
            dropped_files=dropped,
            file_coverage=coverage,
            blank_notes=self._blank_notes,
            # The synthesis prompt only. Asking again re-reads no chunk and
            # re-summarises nothing: the notes are already in this prompt, and
            # they are what the message was written from.
            retry=Retry(
                system=prompts.COMMIT_SYSTEM,
                user=final_prompt,
                max_tokens=out_tokens,
                calls_before=1 + self._last_chunk_count,
            ),
        )

    # ---- parallel execution ------------------------------------------------
    def _run_parallel(
        self,
        items: list,
        fn: Callable,
        progress: ProgressFn,
        is_cancelled: CancelFn,
        label: str,
    ) -> list:
        """Apply ``fn`` to every item, up to ``parallel_calls`` at a time."""
        try:
            return run_parallel(
                items,
                fn,
                workers=self._workers,
                cold_start=self._cold_start,
                progress=progress,
                is_cancelled=is_cancelled,
                label=label,
                prefix="map-reduce: ",
            )
        finally:
            # Whatever branch ran, a call has come back by now, so the model is
            # loaded. Cleared here rather than inside the helper, which must
            # stay a function of its arguments.
            self._cold_start = False

    # ---- map / reduce ------------------------------------------------------
    _last_chunk_count: int = 0
    _workers: int = 1  # set from the context window before any parallel work
    _cold_start: bool = False  # the model still has to be loaded server-side
    _blank_notes: int = 0  # chunks whose summary came back empty
    _model: ModelInfo | None = None
    _model_looked_up: bool = False

    def effective_parallel(self, context: int) -> int:
        """How many requests we may safely run at once for this context size."""
        return effective_parallel(self.settings, context)

    def per_request_context(self, context: int) -> int:
        """Context a single request may use when running concurrently."""
        return per_request_context(self.settings, context)

    def _map_budget(self, context: int, scaffold: str) -> int:
        overhead = estimate_tokens(scaffold)
        # Map summaries are short, so reserve only a small fixed output here to
        # keep each chunk as large as possible - but stay inside this request's
        # share of the context when several run in parallel.
        return input_budget(
            self.per_request_context(context), MAP_OUTPUT_TOKENS, overhead
        )

    def _map(
        self, files, context, branch, diffstat, progress, is_cancelled
    ) -> tuple[list[str], dict[str, set[int]]]:
        scaffold = prompts.MAP_SYSTEM + prompts.MAP_TEMPLATE.replace(
            "{files}", ""
        ).replace("{diff}", "")
        budget = self._map_budget(context, scaffold)
        units, omitted_by_path = build_units_with_coverage(
            files, budget, estimate_tokens
        )
        chunks = pack_units(units, budget, estimate_tokens)
        self._last_chunk_count = len(chunks)
        self._phase(llm_log.MAP)

        def summarize(chunk: str) -> str:
            user = prompts.MAP_TEMPLATE.replace(
                "{files}", _files_in_chunk(chunk)
            ).replace("{diff}", chunk)
            return self.client.chat(
                model=self.settings.active_model(),
                system=prompts.MAP_SYSTEM,
                user=user,
                max_tokens=MAP_OUTPUT_TOKENS,
            )

        notes = self._run_parallel(
            chunks, summarize, progress, is_cancelled, "summarizing chunk"
        )
        # A chunk whose summary came back empty contributed nothing, and the
        # blank line it leaves behind hides that. Drop it and say so: a message
        # written from ten notes when twelve chunks were sent is a different
        # thing from one written from twelve.
        kept = [note for note in notes if note.strip()]
        self._blank_notes = len(notes) - len(kept)
        if self._blank_notes:
            progress(
                f"{self._blank_notes} of {len(notes)} chunk(s) returned nothing; "
                "their changes are not described below."
            )
        return kept, omitted_by_path

    def _reduce_if_needed(
        self, notes, context, branch, diffstat, progress, is_cancelled
    ) -> list[str]:
        usable = self._usable(context)
        final_scaffold = prompts.COMMIT_SYSTEM + render_template(
            self._template(), branch=branch, diffstat=diffstat, diff=""
        )
        scaffold = prompts.REDUCE_SYSTEM + prompts.REDUCE_TEMPLATE.replace(
            "{notes}", ""
        )
        reduce_budget = self._map_budget(context, scaffold)

        depth = 0
        while depth < MAX_REDUCE_DEPTH and len(notes) > 1:
            combined = "\n".join(notes)
            if estimate_tokens(final_scaffold) + estimate_tokens(combined) <= usable:
                break
            self._check_cancel(is_cancelled)
            progress(f"map-reduce: condensing {len(notes)} notes...")
            groups = pack_units(notes, reduce_budget, estimate_tokens)
            if len(groups) >= len(notes):
                break  # not converging; hard-truncate happens downstream
            self._phase(llm_log.REDUCE)
            def condense(group: str) -> str:
                return self.client.chat(
                    model=self.settings.active_model(),
                    system=prompts.REDUCE_SYSTEM,
                    user=prompts.REDUCE_TEMPLATE.replace("{notes}", group),
                    max_tokens=MAP_OUTPUT_TOKENS,
                )

            new_notes = self._run_parallel(
                groups, condense, progress, is_cancelled, "condensing notes"
            )
            notes = new_notes
            depth += 1
        return notes

    def _check_cancel(self, is_cancelled: CancelFn) -> None:
        if is_cancelled():
            raise CancelledError("Generation cancelled.")

    def _phase(self, phase: str) -> None:
        """Tell a recording client what the next calls are for.

        Ignored by a plain client, which is the usual case -- generation does
        not depend on being watched.
        """
        if hasattr(self.client, "phase"):
            self.client.phase = phase


def _files_in_chunk(chunk: str) -> str:
    """Extract a short, comma-separated list of file paths present in a chunk."""
    names: list[str] = []
    for line in chunk.splitlines():
        if line.startswith("+++ b/"):
            names.append(line[6:].strip())
        elif line.startswith("diff --git "):
            parts = line[len("diff --git ") :].split(" b/", 1)
            if len(parts) == 2:
                names.append(parts[1].strip())
    # De-duplicate, preserve order.
    seen: list[str] = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return ", ".join(seen) if seen else "(unknown)"
