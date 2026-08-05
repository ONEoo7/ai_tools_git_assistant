"""Turning a model's reply into findings, tolerantly.

The reply comes from whatever provider is configured, which may be a small
local model. It will wrap the answer in a code fence, bullet the lines, bold
the rule id, forget the line number, or write ``FINDING: R-12 - ...`` instead of
``FINDING | R-12 | ...``. None of that is a reason to lose a finding.

What this module refuses to do is guess. A line it cannot read is not silently
dropped: ``parse_findings`` returns what it understood, ``said_clean`` says
whether the model claimed the file was fine, and the caller
(``review.reviewer``) is what decides that a reply which is neither is a
failure worth showing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from git_assistant.review.rules import RuleTable, normalize_id

#: The token that marks a line as data rather than chatter.
_FINDING = re.compile(r"^finding\b[\s|:\-–—]*", re.I)
_FENCE = re.compile(r"^\s*```")
#: Bullets, numbering and quoting a model adds without being asked.
_ORNAMENT = re.compile(r"^[\s>*\-–—•]*(?:\d+[.)]\s*)?")
_CLEAN = re.compile(r"\bno\s+(?:findings|violations|issues|problems)\b", re.I)
_NUMBER = re.compile(r"\d+")


@dataclass
class Finding:
    """One rule a file breaks, as the model reported it."""

    rule_id: str
    rule_details: str  # resolved from the table now, not when this is rendered
    path: str
    line: int  # 0 means the model did not say
    message: str
    raw_line: str = ""  # exactly what the model wrote, for the detail view
    parsed: bool = True  # False for the salvaged one, below
    rule_known: bool = True  # the id matched a rule in the table

    def label(self) -> str:
        where = f":{self.line}" if self.line else ""
        return f"{self.rule_id or '?'}{where}"


def _strip_ornament(line: str) -> str:
    text = _ORNAMENT.sub("", line.strip())
    text = text.replace("**", "").replace("__", "")
    return text.strip().strip("`").strip()


def _fields(rest: str) -> list[str]:
    """Split a finding's body into fields, whatever separator was used."""
    if "|" in rest:
        return [f.strip() for f in rest.split("|")]
    # No pipes: the model reformatted. A spaced dash or colon is what it uses
    # instead; an unspaced one is inside the rule id (R-12) and must survive.
    return [f.strip() for f in re.split(r"\s+[-–—:]\s+", rest)]


def _line_number(field: str) -> int:
    """``42``, ``line 42``, ``L42`` -> 42; ``-``, ``n/a``, ``?`` -> 0."""
    match = _NUMBER.search(field or "")
    return int(match.group()) if match else 0


def _looks_like_the_template(rule_id: str) -> bool:
    """The model echoing the instruction back is not a finding."""
    return rule_id.startswith("<") or normalize_id(rule_id) in {
        "ruleid",
        "ruleidexactlyaslistedabove",
    }


def parse_findings(reply: str, table: RuleTable, path: str) -> list[Finding]:
    """Every finding that could be read out of ``reply``.

    Fields are filled left to right, so ``FINDING | R-12 | uses a bare except``
    still yields a finding -- a missing line number costs the line number, not
    the finding.
    """
    findings: list[Finding] = []
    for raw in (reply or "").splitlines():
        if _FENCE.match(raw):
            continue
        text = _strip_ornament(raw)
        match = _FINDING.match(text)
        if not match:
            continue
        fields = _fields(text[match.end() :])
        if not fields or not fields[0]:
            continue
        rule_id = fields[0]
        if _looks_like_the_template(rule_id):
            continue

        line, message = 0, ""
        if len(fields) >= 3:
            line = _line_number(fields[1])
            message = " | ".join(f for f in fields[2:] if f)
        elif len(fields) == 2:
            # Two fields: either a line number or the sentence, not both.
            if fields[1] and _NUMBER.fullmatch(fields[1].strip().lstrip("Ll:")):
                line = _line_number(fields[1])
            else:
                message = fields[1]

        rule = table.find(rule_id)
        findings.append(
            Finding(
                rule_id=rule.rule_id if rule is not None else rule_id,
                # Snapshotted here, so a stored review still reads after the
                # table it ran against is edited or deleted.
                rule_details=rule.details if rule is not None else "",
                path=path,
                line=line,
                message=message,
                raw_line=text,
                rule_known=rule is not None,
            )
        )
    return findings


def said_clean(reply: str) -> bool:
    """Whether the model claimed the file breaks nothing.

    Deliberately narrow: an answer that merely failed to mention a violation is
    not the same as one that said there is none, and only the second may be
    shown as a clean file.
    """
    return bool(_CLEAN.search(reply or ""))


def salvage(reply: str, path: str, limit: int = 400) -> Finding:
    """The one finding that stands in for a reply nobody could read.

    An unreadable answer must not render as a clean file. It becomes a visible
    finding carrying the raw text, the same way ``agents.narrator`` keeps a
    draft it could not verify and marks it rather than dropping it.
    """
    excerpt = " ".join((reply or "").split())[:limit]
    return Finding(
        rule_id="",
        rule_details="",
        path=path,
        line=0,
        message="The model's reply could not be read as findings.",
        raw_line=excerpt,
        parsed=False,
        rule_known=False,
    )
