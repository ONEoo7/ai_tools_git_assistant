"""How long a commit message may be, and whether the one on screen is.

Two numbers everyone in git has agreed on without ever writing them down:

- **50 characters** for the subject line is the soft target. It is what
  ``git log --oneline`` and most web interfaces show before they start cutting.
- **72 characters** is the hard cap. Past it the subject is truncated by tools
  that do not say they truncated it, so the last words are simply gone.

The body has no such convention, but a commit message nobody reads is a commit
message that was not worth generating, and a model asked for "a body" will
happily write nine paragraphs about a two-line change. A cap in the region of
500 to 1000 characters is what teams that bother to have a rule tend to pick.

Both halves of this module exist because a limit told to a model is a request,
not a guarantee: `rules` asks, and `measure` checks. The check is reported and
never enforced -- truncating a commit message at 72 characters would produce
exactly the mangled subject the limit exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The traditional soft target for the subject line.
SUBJECT_TARGET = 50
#: The common hard cap. Past this, tools cut without saying so.
SUBJECT_LIMIT = 72
#: Total characters of body. The upper end of the usual 500-1000 convention:
#: a cap is there to stop an essay, not to stop an explanation.
BODY_LIMIT = 1000

#: Any of the three set to this means "no rule": nothing is asked of the model
#: and nothing is reported. Zero rather than None so it is one spinbox with a
#: minimum of zero rather than a spinbox and a checkbox.
OFF = 0


@dataclass(frozen=True)
class Limits:
    """What this repository considers too long."""

    subject_target: int = SUBJECT_TARGET
    subject_limit: int = SUBJECT_LIMIT
    body_limit: int = BODY_LIMIT

    @classmethod
    def of(cls, settings) -> "Limits":
        return cls(
            subject_target=_number(settings, "commit_subject_target", SUBJECT_TARGET),
            subject_limit=_number(settings, "commit_subject_limit", SUBJECT_LIMIT),
            body_limit=_number(settings, "commit_body_limit", BODY_LIMIT),
        )

    def asks_anything(self) -> bool:
        return bool(self.subject_limit or self.subject_target or self.body_limit)


def _number(settings, name: str, fallback: int) -> int:
    try:
        value = int(getattr(settings, name, fallback))
    except (TypeError, ValueError):
        return fallback
    return max(OFF, value)


# ---- what the model is told -------------------------------------------------------
def rules(limits: Limits) -> str:
    """The length rules, as a block appended to whichever template is in use.

    Appended rather than written into the default template, for the reason a
    stored setting always wins: every template a user has already saved -- and
    every per-repository one -- would otherwise keep the numbers it was written
    with, and changing the limit in Settings would change nothing at all.
    """
    if not limits.asks_anything():
        return ""
    lines = ["Length matters as much as the content:"]
    if limits.subject_limit:
        aim = (
            f" Aim for {limits.subject_target}."
            if limits.subject_target and limits.subject_target < limits.subject_limit
            else ""
        )
        lines.append(
            f"- The first line must be at most {limits.subject_limit} characters."
            + aim
            + " Tools that show a subject line cut it without saying so, so a"
            " subject that runs over loses its last words rather than wrapping."
        )
    elif limits.subject_target:
        lines.append(f"- Keep the first line to about {limits.subject_target} characters.")
    if limits.body_limit:
        lines.append(
            f"- The body must be at most {limits.body_limit} characters in total,"
            " counting every line of it."
        )
    lines.append(
        "- If it does not fit, say less. Cut the restatement of what the diff"
        " already shows, not the reason for the change."
    )
    return "\n".join(lines)


def with_rules(template: str, limits: Limits) -> str:
    """``template`` with the length rules after it, if there are any."""
    block = rules(limits)
    if not block:
        return template
    return f"{template.rstrip()}\n\n{block}\n"


# ---- what came back ----------------------------------------------------------------
def split(message: str) -> tuple[str, str]:
    """``(subject, body)``. The first line, and everything after it.

    Blank lines between the two belong to neither: the convention is one blank
    line, and counting it against the body would make a well-formed message a
    character longer than an ill-formed one.
    """
    text = (message or "").strip()
    if not text:
        return "", ""
    subject, _, rest = text.partition("\n")
    return subject.strip(), rest.strip()


@dataclass(frozen=True)
class Measurement:
    """How long the message is, against what was asked for."""

    subject: int
    body: int
    limits: Limits

    @property
    def over_limit(self) -> bool:
        """Past the hard cap on the subject -- the one that loses words."""
        return bool(self.limits.subject_limit and self.subject > self.limits.subject_limit)

    @property
    def over_target(self) -> bool:
        return bool(self.limits.subject_target and self.subject > self.limits.subject_target)

    @property
    def over_body(self) -> bool:
        return bool(self.limits.body_limit and self.body > self.limits.body_limit)

    @property
    def too_long(self) -> bool:
        return self.over_limit or self.over_body

    def label(self) -> str:
        """The counts, for a line under the editor: ``Subject 47/72 - body 312/1000``."""
        parts = [f"Subject {self.subject}" + _of(self.limits.subject_limit)]
        parts.append(f"body {self.body}" + _of(self.limits.body_limit))
        return " - ".join(parts)

    def note(self) -> str:
        """What is wrong with it, or "" when nothing is."""
        if self.over_limit:
            return (
                f"The subject line is {self.subject} characters; past "
                f"{self.limits.subject_limit} it is cut by tools that show it."
            )
        if self.over_body:
            return (
                f"The body is {self.body} characters, over the "
                f"{self.limits.body_limit} this repository asks for."
            )
        if self.over_target:
            return (
                f"The subject line is {self.subject} characters. "
                f"{self.limits.subject_target} is the usual target; it still fits."
            )
        return ""


    def retry_note(self) -> str:
        """What to tell the model it got wrong, appended to the same prompt.

        The reason a second attempt is worth paying for. A retry that re-sent
        the prompt unchanged would be a coin toss at a normal temperature and a
        near-certainty of the same answer at a low one -- which is where this
        application's default sits, because a commit message is a description
        and not a creative act.
        """
        wrong = []
        if self.over_limit:
            wrong.append(
                f"its first line was {self.subject} characters, over the "
                f"{self.limits.subject_limit} allowed"
            )
        if self.over_body:
            wrong.append(
                f"its body was {self.body} characters, over the "
                f"{self.limits.body_limit} allowed"
            )
        if not wrong:
            return ""
        return (
            "Your previous answer was rejected because "
            + " and ".join(wrong)
            + ". Write it again, shorter. Keep the same meaning: drop the "
            "restatement of what the diff already shows, not the reason for "
            "the change. Output only the commit message."
        )


def _of(limit: int) -> str:
    return f"/{limit}" if limit else ""


def measure(message: str, limits: Limits | None = None) -> Measurement:
    subject, body = split(message)
    return Measurement(
        subject=len(subject), body=len(body), limits=limits or Limits()
    )
