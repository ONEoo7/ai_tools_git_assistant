# Code review

The **Code Review** tab checks staged files against a table of rules — the
standard a team already keeps, whether that lives in a spreadsheet or in the
rules that ship here.

## Languages

Detected per file: C, C++, C#, CSS, HTML, Java, JavaScript, Python, Rust, shell,
PowerShell and TypeScript. One review can therefore span a polyglot repository
with each file judged by the rules that apply to it. A file no language claims
is listed as **unreviewable** rather than checked against somebody else's rules.

The **Languages** tab states all of it: every language, the extensions and
shebangs that reach it, and — expanding a row — how many built-in rules apply at
each of its versions. That last number is the one worth looking at before
pinning a version, because it is not the same for every version.

`.h` is C or C++ and only your repository knows which. The pre-run window has an
editable language column, and the answer is remembered so it is asked once.

## Versions

Rules carry the language versions they are true for: `nullptr` is not a C++98
rule and f-strings are not a Python 2 one.

The version is read from what the repository already declares —
`pyproject.toml`, `Cargo.toml`, `tsconfig.json`, a `.csproj`, `pom.xml`,
`CMakeLists.txt`, a doctype, a shebang — and can be set by hand where nothing
declares one.

## Profiles

A **profile** ties it together: which rules apply to which language, at which
version. Open one and it has a row per language, a version dropdown, and under
each language the **rule sets** it is checked against.

A language can draw on more than one set — its own shipped rules, another
language's, and any table of yours — through **Add rule set…**. Each set has its
own tick: turn the whole set off, or open it and untick individual rules. A set
with some of its rules off shows part-ticked rather than off, and a set that is
entirely off is still a set the language points at, so one rule can be turned
back on without adding it again.

Every install starts with **Default Rules**: the shipped rules for whatever
language each file turns out to be.

Which profile a review runs against is the **Rules profile** dropdown beside the
repository, remembered per repository, and marked in bold in the list — so
reading a profile is never mistaken for selecting it.

**Default Rules is read-only** — genuinely, not copy-on-edit: nothing in it can
be ticked, no version changed, and it cannot be deleted. It is *generated* from
the rules this build ships with rather than stored anywhere, so an edit would
have nowhere to live, and a stored profile of the same name would shadow it and
stop it tracking the build.

**New…** makes a profile of your own, copied from whichever one is open. That is
how you change the shipped rules: copy them, then edit the copy. A new profile
is opened but not put under review — which profile a review runs against stays
the **Rules profile** dropdown's decision.

**Delete** removes one of your own. Any repository that was reviewed against it
falls back to Default Rules, so its next review never runs against a profile
that is not there. A profile a repository ships is read-only here too, for the
same reason it always was: it belongs to the project.

Profiles live in the settings a repository can carry, so a project can ship the
standard it holds itself to. **Share with the repository** writes it to
`.git-assistant/code-review-profile.json` — your own tables in full, the shipped
ones by name. That is the only file this application ever writes into a working
tree, and it takes an explicit press.

## Rule sets

The **Rule Sets** tab lists every set a profile can draw on, of both kinds:

- **Built in** — one per language, and what nearly every review actually runs
  against. Each rule is shown with the span of language versions it is true for,
  which is the one thing the file cannot tell you at a glance.
- **Mine** — the tables imported from a spreadsheet or from another machine.

The built-in sets are one JSON file per language under the config directory's
`code_review/`, which **Open rules folder** and **Open this file** open. They are
yours to edit; each rule carries a `since`/`until` span so one rule can be true
across several language versions without being written out several times.
**Reset to shipped** puts the rules this build came with back over one of them.

**Import spreadsheet…** reads an `.xlsx` with a rule-ID column and a
rule-details column. The header is looked for rather than assumed, so a title row
above it and columns nobody here cares about are both fine; `Rule ID`, `rule_id`
and `RULEID` all read the same. Tables export back to `.xlsx`, or to JSON to
move between machines — and an import never overwrites a table you already have.

## LLM-as-a-Judge, and the leaderboard

Code review is the hardest thing this application asks a model to do, and a
small local model cannot tell you whether it is any good at it. So a second,
stronger model can be asked: it is shown **the exact prompt the reviewer was
given and the exact answer it returned**, and scores that answer out of 10.

Tick **Use LLM-as-a-Judge** beside the repository to turn it on — it is off by
default, because it roughly doubles the calls a review makes, and the pre-run
window says so in the call count before you spend anything. Configure who judges
under **Connection & Model → Code Review Judge**: a provider, and its own model
and temperature. Its own, deliberately: judge and reviewer are often the same
provider with different models, and sharing the fields would mean choosing a
judge silently changed what does the reviewing. Set the temperature to 0 if you
want two runs to be comparable.

The judge is asked for one line:

```
SCORE | 7.5 | quoted a rule id that was not on the list
```

**Nothing about the review changes.** Findings are not filtered, re-ranked or
hidden — a judge that edited them would make a bad judge indistinguishable from
a good reviewer, which is the one comparison this exists to make. The only new
output is the score.

An answer the judge cannot produce a score for is recorded as **unscored, never
as zero**. Zero is a judgement; "the judge timed out" is not one, and averaging
it in would file the judge's failures as the reviewer's. Files whose review
failed outright are not scored either — there is no answer to grade.

### The leaderboard

Scores accumulate in the **Leaderboard** tab, and in
`<config dir>/code_review/leaderboard.json` beside the rule files. One row per
**reviewed model and judge model together** — a 7 from Opus and a 7 from a 4B
local model are not the same measurement, so changing judge starts a fresh row
rather than quietly moving every average. Each row keeps its runs, the files
scored across them, the running total and the mean.

**Time / file** is beside the score, because the two together are the decision:
a 4B model that scores 7.2 in under a second is a different proposition from a
hosted one that scores 8.3 and takes a minute and a half. It is the time each
*call* took, added up and divided by the files — not how long the run took,
which mostly measures how many calls ran at once. It covers exactly the files
that were scored, so the two columns are about the same set.

Judge tokens are billed separately, as **Code review judge** in the Usage pane.
That is the point: the whole reason to run a small local reviewer with a strong
judge is that the two cost wildly different amounts, and one figure covering
both cannot show it.

The prompt is yours to edit — `default_judge_prompt` in
`static_user_settings.json`, with `{prompt}` and `{reply}` filled in.

## Running one

**Mark the files.** Everything staged starts marked; unmark what you do not want
checked. Files dropped by the noise filter are listed as unreviewable rather
than silently left out.

**A window before it runs** lists every marked file with its language, its
version and the rules that will be checked, beside the token estimate.

**One call per file**, run `model.parallel_calls` at a time, each carrying the
rules, that file's diff, and the file as it will be after the change. When they
do not all fit, the content is dropped before the diff and the diff before the
rules — and whatever was cut is said on the file's row, above the findings, and
in the prompt itself, so a partial review cannot be mistaken for a clean one.

**View LLM calls** shows the exact prompt sent for each file and exactly what
came back. A reply that cannot be read as findings is asked again once, then
kept verbatim as a visible finding — never as a clean file.

## Previous reviews

Every run is recorded per repository, newest first, pinnable, reopenable. The
findings carry their rule text with them, so a stored review still reads after
its table has been edited or deleted.

The limit is `review.history_limit`; `0` keeps every one.
