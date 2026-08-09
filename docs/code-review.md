# Code review

The **Code Review** tab checks staged files against a table of rules — the
standard a team already keeps, whether that lives in a spreadsheet or in the
rules that ship here.

## Languages

Detected per file: C, C++, C#, CSS, HTML, Java, JavaScript, Python, Rust, shell,
PowerShell and TypeScript. One review can therefore span a polyglot repository
with each file judged by the rules that apply to it. A file no language claims
is listed as **unreviewable** rather than checked against somebody else's rules.

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
version. Open one and it has a row per language, a version dropdown and a
checkbox per rule.

Which profile a review runs against is the **Rules profile** dropdown beside the
repository, remembered per repository, and marked in bold in the list — so
reading a profile is never mistaken for selecting it.

The shipped defaults are read-only. Editing one keeps the change in a copy of
your own rather than silently rewriting what ships or losing the edit.

Profiles live in the settings a repository can carry, so a project can ship the
standard it holds itself to. **Share with the repository** writes it to
`.git-assistant/code-review-profile.json` — your own tables in full, the shipped
ones by name. That is the only file this application ever writes into a working
tree, and it takes an explicit press.

## Rules

The rules themselves are one JSON file per language under the config directory's
`code_review/`, which **Open rules folder** opens. They are yours to edit; each
rule carries a `since`/`until` span so one rule can be true across several
language versions without being written out several times.

**Import spreadsheet…** reads an `.xlsx` with a rule-ID column and a
rule-details column. The header is looked for rather than assumed, so a title row
above it and columns nobody here cares about are both fine; `Rule ID`, `rule_id`
and `RULEID` all read the same. Tables export back to `.xlsx`, or to JSON to
move between machines — and an import never overwrites a table you already have.

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
