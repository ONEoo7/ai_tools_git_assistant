"""What the model is asked, and what it is allowed to say.

Plain string constants rendered with ``.replace`` -- never ``str.format`` --
because the facts these carry are full of paths, braces and command lines.

The model writes prose and nothing else. Numbers, tables and commands are
measured and authored in Python, handed over in a FACTS block, and checked
against the model's answer afterwards (see ``narrator``).
"""

from __future__ import annotations

from dataclasses import dataclass

AGENT_SYSTEM = """\
You are a technical writer producing one section of a git repository audit for \
the senior engineer who owns that repository.

You are given a FACTS block. It holds every measurement that exists.

Rules, in order of importance:
1. Every number, size, count, date, path and file name you write must appear \
verbatim in the FACTS block. Copy them exactly, including units, and write them \
as digits ("3 checks", never "three checks") so they can be checked against the \
block.
2. Never compute, estimate, convert, round, total or compare by arithmetic. If \
a figure is not in the block it does not exist, and you say the measurement was \
not taken rather than working it out.
3. Never recommend a command that is not in the block.
4. Plain prose paragraphs only: no headings, no bullet lists, no code fences, \
no tables, and no preamble such as "Here is".
5. State findings plainly. No hedging, no salesmanship, no emoji.

Write the requested section and nothing else."""

SLOT_TEMPLATE = """\
SECTION: {section}
LENGTH: {length}
COVER, IN THIS ORDER:
{outline}

FACTS (the only figures you may use):
{facts}"""

RETRY_SUFFIX = """\

These do not appear in the FACTS block and must not be used: {bad}
Rewrite the section using only the figures given, writing every number as \
digits copied from the block."""


@dataclass(frozen=True)
class Outline:
    """What one section is for, and how long it should be."""

    section: str
    length: str
    points: tuple[str, ...]

    def render(self, facts: str) -> str:
        return (
            SLOT_TEMPLATE.replace("{section}", self.section)
            .replace("{length}", self.length)
            .replace("{outline}", "\n".join(f"- {p}" for p in self.points))
            .replace("{facts}", facts)
        )

    def max_tokens(self) -> int:
        """Roughly two tokens a word, kept inside sane bounds."""
        digits = [int(w) for w in self.length.replace("-", " ").split() if w.isdigit()]
        words = max(digits) if digits else 150
        return max(192, min(640, words * 2))


#: agent id -> slot -> outline. A section whose slot is missing here keeps its
#: deterministic prose, which is why adding a section cannot break narration.
OUTLINES: dict[str, dict[str, Outline]] = {
    "size-audit": {
        "exec_summary": Outline(
            "Executive summary of a repository size audit",
            "150-200 words, 2-3 paragraphs",
            (
                "how large the .git directory is",
                "how much of it can be reclaimed without rewriting history",
                "what dominates the real history, naming the largest path and "
                "how many committed versions of it there are",
                "if the facts mention content at LFS-tracked paths stored as "
                "full files, say that LFS rules do not apply to commits made "
                "before the rule was added",
            ),
        ),
        "where": Outline(
            "Where the .git directory's bytes are",
            "60-90 words, one paragraph",
            (
                "which location holds most of the bytes",
                "what that location is for",
            ),
        ),
        "garbage": Outline(
            "Orphaned data that can be deleted now",
            "70-110 words, one paragraph",
            (
                "how much is orphaned and what it is",
                "that leftover temporary packs come from an interrupted fetch, "
                "push or repack and are referenced by no branch, tag or index",
                "that removing them rewrites nothing",
            ),
        ),
        "history": Outline(
            "What the reachable history actually contains",
            "90-130 words, 1-2 paragraphs",
            (
                "how many commits and file versions there are",
                "the largest paths, named, with their version counts",
                "which file types dominate",
            ),
        ),
        "root_cause": Outline(
            "Why Git LFS did not make the repository smaller",
            "90-130 words, one paragraph",
            (
                "that LFS filters apply only to commits made after the rule was "
                "added, and never convert what is already committed",
                "how much content at LFS-tracked paths is still stored as full "
                "files, if the facts give a figure",
                "that the configuration is right and the backlog is what is left",
            ),
        ),
        "next_steps": Outline(
            "What to do about it",
            "80-120 words, one paragraph",
            (
                "that removing the leftover packs and repacking is safe and "
                "needs no coordination",
                "that reclaiming pre-LFS history means rewriting it: every later "
                "commit hash changes, the remote needs a force-push, and every "
                "clone has to be replaced",
                "that the second one is scheduled, not run on the spot",
            ),
        ),
    },
    "config-audit": {
        "exec_summary": Outline(
            "Executive summary of a repository configuration audit",
            "120-170 words, 2 paragraphs",
            (
                "how many checks ran and how many failed or warned",
                "what the failures are, in the words of the findings table",
                "what is fine, briefly",
            ),
        ),
        "next_steps": Outline(
            "What to fix first",
            "70-110 words, one paragraph",
            (
                "which finding to address first and why it matters",
                "that the fixes are shown as commands to run, not applied here",
            ),
        ),
    },
    "consistency-audit": {
        "consistency_summary": Outline(
            "Executive summary of a repository consistency audit",
            "130-180 words, 2 paragraphs",
            (
                "how many local branches there are and how many are stale",
                "that stale and merged branches are safe to delete because "
                "their commits are already on the default branch, and stale "
                "unmerged ones are not, because the branch is the only copy",
                "how many repositories were swept and whether any submodule is "
                "pinned at more than one version",
            ),
        ),
        "submodule_disagreements": Outline(
            "Submodules pinned at different versions across repositories",
            "70-110 words, one paragraph",
            (
                "how many submodules are used at more than one version",
                "that the version is the commit each parent repository pins, "
                "not whatever happens to be checked out locally",
                "if the facts give none, say the fleet agrees and say nothing "
                "further",
            ),
        ),
    },
}
