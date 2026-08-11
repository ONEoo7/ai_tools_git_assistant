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

#: Said when the file's language and version are known. The second sentence is
#: load-bearing: told "this is C++17" without it, a model reports "auto return
#: types need C++14" under an invented rule id, turning a rules review into a
#: free-form one.
LANGUAGE_LINE = "Language: {language}{version}. Judge only against the rules listed above.\n"

REVIEW_TEMPLATE = """\
Rules to check, one per line as <ruleID>: <ruleDetails>
{rules}

File: {path}
{language}{notes}
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

#: The judge is not reviewing the code. It is marking the reviewer's homework,
#: which is a different job and has to say so twice -- a model shown a diff will
#: review it however it was briefed.
JUDGE_SYSTEM = (
    "You grade the work of a code reviewer. You are shown the exact instructions "
    "another model was given and the exact answer it produced. You judge that "
    "answer, not the code: whether it followed the format it was asked for, "
    "whether the rule ids it quoted were on the list it was given, whether its "
    "findings are supported by the code shown, and whether it missed anything "
    "obvious. You output nothing except the one line you are asked for."
)

#: What a judge is shown, and the one line it may answer with. `{prompt}` is the
#: reviewer's user prompt verbatim -- rules, diff and file included -- and
#: `{reply}` is what came back.
JUDGE_TEMPLATE = """\
Below is the prompt another model was given, and the answer it returned.

=== THE PROMPT IT WAS GIVEN ===
{prompt}

=== THE ANSWER IT RETURNED ===
{reply}

Score that answer out of 10, where 10 is a review you would have signed off
yourself and 0 is unusable. Weigh, in this order:

1. Did it obey the output format exactly?
2. Are the rule ids real ones from the list it was given?
3. Is every finding supported by the code shown, with no invented ones?
4. Did it miss a violation that the rules and the code plainly show?

Answer with exactly one line and nothing else:
SCORE | <number from 0.0 to 10.0> | <one sentence saying why>
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
