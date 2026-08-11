"""Scoring a review, so one model's work can be compared with another's.

The reviewer is often something small and local. Whether it is any good at this
is not a question it can answer about itself, so a second and stronger model is
shown the same exchange -- the exact prompt the reviewer was given and the exact
reply it returned -- and asked to mark it out of ten.

Nothing here changes a review. Findings are not filtered, re-ranked or hidden;
the judge's opinion leaves the run as a number, and the numbers accumulate in
`review.leaderboard`. That separation is deliberate: a judge that silently
edited the findings would make a bad judge indistinguishable from a good
reviewer, which is the one comparison this exists to make.

The output contract is one line, in the shape `review.parse` already reads:

    SCORE | 7.5 | quoted a rule id that was not on the list

A reply that cannot be read is an **error**, never a zero. Zero is a judgement --
"this review was useless" -- and recording it for a judge that timed out or
answered in prose would quietly poison the average with the judge's failures
rather than the reviewer's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from git_assistant.review import prompts

#: The band a score lives in. Ten is a review you would have signed off, zero is
#: unusable; anything outside is the model not following the contract.
MIN_SCORE = 0.0
MAX_SCORE = 10.0

#: How much room the judge is given to answer. One line, so this is generous --
#: and generous on purpose, because a model cut off mid-sentence produces an
#: unparseable reply, which costs the whole file's score.
JUDGE_OUTPUT_TOKENS = 200

#: `SCORE | 7.5 | why`, with the separator the reviewer's own format uses. The
#: reason is optional: a score with no sentence is still a score.
_SCORE = re.compile(
    r"^\s*SCORE\s*[|:]\s*(?P<score>-?\d+(?:\.\d+)?)\s*(?:[|:]\s*(?P<reason>.*))?$",
    re.IGNORECASE,
)

#: A bare number on its own line, for a model that dropped the label. Accepted
#: only when nothing better was found -- see `parse_verdict`.
_BARE = re.compile(r"^\s*(?P<score>\d+(?:\.\d+)?)\s*(?:/\s*10)?\s*$")


@dataclass(frozen=True)
class JudgeConfig:
    """Who scores a run, and what they are asked.

    Resolved once, before the run, from the judge section of the settings and
    the prompt in the user's own. Frozen because a run must be scored by the
    model the estimate priced.
    """

    provider: str = ""
    model: str = ""
    temperature: float = 0.0
    prompt: str = ""

    def usable(self) -> bool:
        """Whether there is actually a judge here.

        A ticked box with no model chosen is not a judge, and a run that
        silently scored nothing would be indistinguishable from one that scored
        badly.
        """
        return bool(self.provider and self.model)

    def label(self) -> str:
        return f"{self.model} ({self.provider})" if self.usable() else "not configured"


def rules():
    """The judge section of the user's own settings.

    The user tier and not the tier in force for a repository: who judges is a
    property of the person doing the comparing, not of the project being
    compared, and a leaderboard whose judge changed with the repository would
    be measuring two things at once.
    """
    from git_assistant import repo_config

    return repo_config.defaults().review.judge


def config_from(settings) -> JudgeConfig | None:
    """The judge to use, or None if it is switched off.

    `settings` supplies only the prompt -- `default_judge_prompt`, the user's
    own, which a repository can never replace. Everything else comes from
    `rules()`. Accepts a `Bound` or a plain `Settings`; the attribute resolves
    through either.
    """
    judge = rules()
    if not judge.enabled:
        return None
    return JudgeConfig(
        provider=judge.provider,
        model=judge.model,
        temperature=judge.temperature,
        prompt=getattr(settings, "default_judge_prompt", "") or prompts.JUDGE_TEMPLATE,
    )


@dataclass
class Verdict:
    """What the judge said about one file's review."""

    score: float = 0.0
    reason: str = ""
    raw: str = ""
    #: Set when there is no score to be had. `scored` is what callers ask; a
    #: verdict with an error is never counted, however its score field reads.
    error: str = ""

    @property
    def scored(self) -> bool:
        return not self.error


def clamp(value: float) -> float:
    return max(MIN_SCORE, min(MAX_SCORE, value))


def parse_verdict(reply: str) -> Verdict:
    """Read a judge's answer. Never raises; an unreadable one is an error.

    The labelled line wins wherever it appears, because a model that reasons
    before answering puts it last. A bare number is accepted only when there is
    no labelled line at all -- taking the first number in prose would score a
    review on the digits in "rule PY-06".
    """
    text = reply or ""
    for line in text.splitlines():
        found = _SCORE.match(line)
        if found:
            return Verdict(
                score=clamp(float(found.group("score"))),
                reason=(found.group("reason") or "").strip(),
                raw=text,
            )

    lines = [one for one in text.splitlines() if one.strip()]
    if len(lines) == 1:
        found = _BARE.match(lines[0])
        if found:
            return Verdict(score=clamp(float(found.group("score"))), raw=text)

    if not text.strip():
        return Verdict(raw=text, error="the judge returned nothing")
    return Verdict(
        raw=text,
        error="the judge did not answer with a SCORE line",
    )


def build_prompt(template: str, *, prompt: str, reply: str) -> str:
    """Fill the judge's template with the exchange being scored.

    `prompts.render` and not `str.format`: the values are a diff and a model's
    prose, both full of braces, and formatting them would raise on somebody's
    C++ rather than score it.
    """
    return prompts.render(template or prompts.JUDGE_TEMPLATE, prompt=prompt, reply=reply)


def mean_of(verdicts: list[Verdict]) -> float:
    """The mean of the scores there are, or 0.0 when there are none.

    Errors are left out rather than counted as zero, for the reason the module
    docstring gives: they are the judge's failures, not the reviewer's.
    """
    scored = [one.score for one in verdicts if one.scored]
    return round(sum(scored) / len(scored), 4) if scored else 0.0
