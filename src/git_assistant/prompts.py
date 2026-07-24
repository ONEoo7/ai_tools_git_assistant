"""Prompt templates: the user-editable commit template + map-reduce prompts.

The commit template supports these placeholders (all optional):
    {branch}   - current branch name
    {diffstat} - compact `git diff --stat` output
    {diff}     - the (possibly summarized) change content
"""

from __future__ import annotations

# Default final template (Conventional Commits). Editable in Settings.
DEFAULT_TEMPLATE = """\
You are an expert developer writing a git commit message that follows the
Conventional Commits specification.

Rules:
- First line: `<type>(<optional scope>): <short imperative summary>` (<= 72 chars).
- type is one of: feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert.
- Blank line, then a body explaining WHAT changed and WHY (wrap at ~72 chars).
- Use bullet points in the body when several distinct changes are present.
- Add `BREAKING CHANGE: <description>` in the footer if applicable.
- Output ONLY the commit message. No markdown fences, no commentary.

Current branch: {branch}

Summary of changed files (git diff --stat):
{diffstat}

Changes:
{diff}
"""

# System message used for the final commit-message generation call.
COMMIT_SYSTEM = (
    "You write precise, conventional git commit messages. "
    "You output only the commit message text, nothing else."
)

# ---- Map-reduce prompts (used only when the diff overflows the context) ----

# System message for the per-chunk "map" step.
MAP_SYSTEM = (
    "You are a code-review assistant. You summarize a fragment of a git diff "
    "into a terse, factual note. Output only the note."
)

# User prompt for a single map chunk. {files} lists the file(s) in the chunk.
MAP_TEMPLATE = """\
Summarize the following git diff fragment in 1-3 short bullet points.
Focus on WHAT changed and, if inferable, WHY. Name the affected file(s).
Do not speculate beyond the diff. Be terse.

File(s): {files}

Diff fragment:
{diff}
"""

# System message for the "reduce" step that condenses notes further if needed.
REDUCE_SYSTEM = (
    "You condense several change notes into a shorter combined set of notes, "
    "preserving every distinct change. Output only the notes."
)

REDUCE_TEMPLATE = """\
Combine and condense these change notes into a shorter list of bullet points.
Preserve every distinct change; merge duplicates. Be terse.

Notes:
{notes}
"""
