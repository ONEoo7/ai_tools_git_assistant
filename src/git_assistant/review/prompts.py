"""What the reviewer asks, and the shape of the answer it can read.

The contract is one line per violation:

    FINDING | <ruleID> | <line number, or -> | <one sentence>

Not JSON. The provider may be a 4B model running locally, which emits fences,
trailing commas and objects that stop mid-key; with lines, a mangled one costs
one finding instead of all of them. The literal ``FINDING`` token is what lets
the parser skip a preamble ("Sure! Here is my review:") without guessing which
lines are data.

Placeholders are substituted with ``str.replace``, never ``str.format``: the
values are diffs and source code, which are full of braces.
"""

from __future__ import annotations

#: The whole answer for a file that breaks nothing.
NO_FINDINGS = "NO FINDINGS"

REVIEW_SYSTEM = (
    "You are a meticulous code reviewer. You are given a numbered list of rules "
    "and one file's change. You report only violations you can point to in the "
    "code shown. You never invent a rule id, and you output nothing except the "
    "lines you are asked for."
)

REVIEW_TEMPLATE = """\
Rules to check, one per line as <ruleID>: <ruleDetails>
{rules}

File: {path}
{notes}
What changed in this file (unified diff):
{diff}

The file after the change:
{content}

Report every rule above that this file breaks. Answer with one line per
violation and nothing else:
FINDING | <ruleID exactly as listed above> | <line number in the file, or -> | <one sentence>

If the file breaks none of the rules, answer with exactly: NO FINDINGS
Do not explain. Do not repeat the rules. Do not add a summary.
"""

#: Appended to the user prompt when the first answer could not be read at all.
#: One retry, in the shape ``agents.narrator`` uses: quote back what came so the
#: model can see what it did, then restate the contract.
REVIEW_RETRY_SUFFIX = """\

Your previous answer could not be read. It began:
{reply}

Answer again using only lines of this exact form:
FINDING | <ruleID> | <line number, or -> | <one sentence>
If the file breaks none of the rules, answer with exactly: NO FINDINGS
"""


def render(template: str, **values: str) -> str:
    """Fill a template's ``{name}`` placeholders literally."""
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out
