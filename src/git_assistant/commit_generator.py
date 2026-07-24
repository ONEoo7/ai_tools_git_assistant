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

from git_assistant import git_ops, prompts
from git_assistant.config import Settings
from git_assistant.diff_strategy import (
    build_units,
    filter_files,
    pack_units,
    split_diff,
    truncate_to_budget,
)
from git_assistant.lmstudio_client import LMStudioClient
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


@dataclass
class GenerationResult:
    message: str
    strategy: str  # "single-shot" | "map-reduce"
    context_window: int
    input_budget: int
    input_tokens: int
    num_chunks: int = 1
    dropped_files: list[str] = field(default_factory=list)


class CancelledError(RuntimeError):
    """Raised when generation is cancelled cooperatively."""


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

    # ---- budget helpers ----------------------------------------------------
    def _detected_context(self) -> int | None:
        """Model's real loaded context, or None if it can't be determined."""
        try:
            return self.client.context_length_for(self.settings.selected_model)
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
        if not s.selected_model:
            raise ValueError("No model is selected. Open Settings and pick one.")

        progress("Reading git diff...")
        branch = git_ops.current_branch(repo)
        diffstat = git_ops.get_diffstat(repo, s.diff_mode)
        raw_diff = git_ops.get_diff(repo, s.diff_mode)
        if not raw_diff.strip():
            mode = "staged" if s.diff_mode == "cached" else "uncommitted"
            raise ValueError(f"No {mode} changes to describe in this repository.")

        files, dropped = filter_files(split_diff(raw_diff), s.ignore_globs)
        if not files:
            raise ValueError(
                "All changed files were filtered out as noise "
                "(lockfiles/binaries). Adjust ignore globs in Settings."
            )
        filtered_diff = "\n".join(f.text for f in files)

        context = self._context_window()
        usable = self._usable(context)
        out_tokens = self._reserved_output(context)

        # Does the full single-shot prompt fit?
        full_prompt = render_template(
            s.prompt_template, branch=branch, diffstat=diffstat, diff=filtered_diff
        )
        full_tokens = estimate_tokens(prompts.COMMIT_SYSTEM) + estimate_tokens(
            full_prompt
        )
        self._check_cancel(is_cancelled)

        if full_tokens <= usable:
            progress("Diff fits context - generating (single-shot)...")
            message = self.client.chat(
                model=s.selected_model,
                system=prompts.COMMIT_SYSTEM,
                user=full_prompt,
                max_tokens=out_tokens,
            )
            return GenerationResult(
                message=message,
                strategy="single-shot",
                context_window=context,
                input_budget=usable,
                input_tokens=full_tokens,
                dropped_files=dropped,
            )

        # --- Overflow: map-reduce ------------------------------------------
        progress(
            f"Diff too large ({full_tokens} > {usable} tokens) - "
            "switching to map-reduce..."
        )
        notes = self._map(files, context, branch, diffstat, progress, is_cancelled)
        notes = self._reduce_if_needed(
            notes, context, branch, diffstat, progress, is_cancelled
        )

        combined = "\n".join(notes)
        # Ensure the final prompt fits by hard-truncating the notes if needed.
        final_scaffold = prompts.COMMIT_SYSTEM + render_template(
            s.prompt_template, branch=branch, diffstat=diffstat, diff=""
        )
        content_budget = max(256, usable - estimate_tokens(final_scaffold))
        combined = truncate_to_budget(combined, content_budget, estimate_tokens)

        final_prompt = render_template(
            s.prompt_template, branch=branch, diffstat=diffstat, diff=combined
        )
        progress("Synthesizing final commit message...")
        self._check_cancel(is_cancelled)
        message = self.client.chat(
            model=s.selected_model,
            system=prompts.COMMIT_SYSTEM,
            user=final_prompt,
            max_tokens=out_tokens,
        )
        return GenerationResult(
            message=message,
            strategy="map-reduce",
            context_window=context,
            input_budget=usable,
            input_tokens=estimate_tokens(prompts.COMMIT_SYSTEM)
            + estimate_tokens(final_prompt),
            num_chunks=self._last_chunk_count,
            dropped_files=dropped,
        )

    # ---- map / reduce ------------------------------------------------------
    _last_chunk_count: int = 0

    def _map_budget(self, context: int, scaffold: str) -> int:
        overhead = estimate_tokens(scaffold)
        # Map summaries are short, so reserve only a small fixed output here to
        # keep each chunk as large as possible.
        return input_budget(context, MAP_OUTPUT_TOKENS, overhead)

    def _map(
        self, files, context, branch, diffstat, progress, is_cancelled
    ) -> list[str]:
        scaffold = prompts.MAP_SYSTEM + prompts.MAP_TEMPLATE.replace(
            "{files}", ""
        ).replace("{diff}", "")
        budget = self._map_budget(context, scaffold)
        units = build_units(files, budget, estimate_tokens)
        chunks = pack_units(units, budget, estimate_tokens)
        self._last_chunk_count = len(chunks)

        notes: list[str] = []
        for i, chunk in enumerate(chunks, start=1):
            self._check_cancel(is_cancelled)
            progress(f"map-reduce: summarizing chunk {i}/{len(chunks)}...")
            file_names = _files_in_chunk(chunk)
            user = prompts.MAP_TEMPLATE.replace("{files}", file_names).replace(
                "{diff}", chunk
            )
            note = self.client.chat(
                model=self.settings.selected_model,
                system=prompts.MAP_SYSTEM,
                user=user,
                max_tokens=MAP_OUTPUT_TOKENS,
            )
            notes.append(note)
        return notes

    def _reduce_if_needed(
        self, notes, context, branch, diffstat, progress, is_cancelled
    ) -> list[str]:
        usable = self._usable(context)
        final_scaffold = prompts.COMMIT_SYSTEM + render_template(
            self.settings.prompt_template, branch=branch, diffstat=diffstat, diff=""
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
            new_notes: list[str] = []
            for group in groups:
                user = prompts.REDUCE_TEMPLATE.replace("{notes}", group)
                new_notes.append(
                    self.client.chat(
                        model=self.settings.selected_model,
                        system=prompts.REDUCE_SYSTEM,
                        user=user,
                        max_tokens=MAP_OUTPUT_TOKENS,
                    )
                )
            notes = new_notes
            depth += 1
        return notes

    def _check_cancel(self, is_cancelled: CancelFn) -> None:
        if is_cancelled():
            raise CancelledError("Generation cancelled.")


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
