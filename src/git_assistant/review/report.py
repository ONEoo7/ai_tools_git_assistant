"""Rendering a review: Markdown to keep, HTML to read.

The same split as ``agents.report``. Markdown is the export format because it
survives being pasted into a pull request; the HTML is for the detail pane.

Everything a reader needs to trust the review is in the header: which table it
ran against, which model wrote it, and whether the rules or the files were cut
to fit. A findings list without that is a list of opinions of unknown coverage.
"""

from __future__ import annotations

import html

from git_assistant.review.parse import Finding
from git_assistant.review.reviewer import FileReview, ReviewRun

UNREADABLE_NOTE = (
    "The model's reply could not be read as findings. It is kept verbatim so "
    "the file is not mistaken for a clean one."
)


def _where(finding: Finding) -> str:
    return f"line {finding.line}" if finding.line else "no line given"


def _checked_against(review: FileReview) -> str:
    """What this file in particular was judged by.

    Per file, because a run spans languages: the header can only name the
    profile, and "40 rules" would be true of no single file in it.
    """
    from git_assistant.review import languages

    if not review.rules_total:
        return ""
    what = languages.label_of(review.language) if review.language else ""
    lang = languages.get(review.language)
    version = lang.label_for(review.version) if lang else ""
    if version:
        what = f"{what} {version}"
    rules = f"{review.rules_sent} of {review.rules_total} rule(s)" if review.rules_cut else (
        f"{review.rules_total} rule(s)"
    )
    return f"{what}, {rules}".strip(", ") if what else rules


def _header_lines(run: ReviewRun) -> list[str]:
    where = f"{run.branch} @ {run.head[:7]}" if run.head else ""
    if run.dirty and where:
        where += "*"
    out = [
        f"Reviewed {run.started_at} — `{run.repo_path}`" + (f" — {where}" if where else ""),
        "",
        f"Profile: **{run.table_name or 'none'}** — "
        f"model: {run.model or 'unknown'} ({run.provider})",
        "",
        run.summary(),
        "",
    ]
    cut = run.rules_truncated()
    if cut:
        out += [
            f"> {len(cut)} file(s) were judged against only part of their rules, "
            "which did not fit the model's context window.",
            "",
        ]
    return out


# ---- markdown ---------------------------------------------------------------------
def to_markdown(run: ReviewRun) -> str:
    out: list[str] = ["# Code review", ""]
    out += _header_lines(run)
    for review in run.files:
        out += _file_md(review)
    return "\n".join(out).rstrip() + "\n"


def _file_md(review: FileReview) -> list[str]:
    note = ", ".join(p for p in (_checked_against(review), review.note()) if p)
    heading = f"## {review.path}" + (f" — *{note}*" if note else "")
    out = [heading, ""]
    if review.error:
        return out + [f"Not reviewed: {review.error}", ""]
    if not review.findings:
        return out + ["No findings.", ""]
    for finding in review.findings:
        if not finding.parsed:
            out += [f"- **Unreadable reply** — {UNREADABLE_NOTE}", "", "  > " + finding.raw_line, ""]
            continue
        unknown = " *(not in the rule table)*" if not finding.rule_known else ""
        out.append(f"- **{finding.rule_id}**{unknown} ({_where(finding)}) — {finding.message}")
        if finding.rule_details:
            out.append(f"  - Rule: {finding.rule_details}")
    out.append("")
    return out


# ---- html -------------------------------------------------------------------------
def to_html(run: ReviewRun) -> str:
    e = html.escape
    out = ["<html><body>", "<h2>Code review</h2>"]
    out.append(f"<p><b>{e(run.summary())}</b><br>")
    out.append(
        f"Profile: {e(run.table_name or 'none')} — "
        f"model: {e(run.model or 'unknown')} ({e(run.provider)})<br>"
        f"{e(run.started_at)} — {e(run.repo_path)}</p>"
    )
    cut = run.rules_truncated()
    if cut:
        out.append(
            f"<p><b>{len(cut)} file(s) were judged against only part of their "
            "rules, which did not fit the model's context window.</b></p>"
        )
    for review in run.files:
        note = ", ".join(p for p in (_checked_against(review), review.note()) if p)
        out.append(f"<h3>{e(review.path)}{' — ' + e(note) if note else ''}</h3>")
        if review.error:
            out.append(f"<p>Not reviewed: {e(review.error)}</p>")
            continue
        if not review.findings:
            out.append("<p>No findings.</p>")
            continue
        out.append("<ul>")
        for finding in review.findings:
            if not finding.parsed:
                out.append(
                    f"<li><b>Unreadable reply</b> — {e(UNREADABLE_NOTE)}"
                    f"<pre>{e(finding.raw_line)}</pre></li>"
                )
                continue
            unknown = " (not in the rule table)" if not finding.rule_known else ""
            out.append(
                f"<li><b>{e(finding.rule_id)}</b>{unknown} ({_where(finding)}) — "
                f"{e(finding.message)}"
                + (f"<br><i>{e(finding.rule_details)}</i>" if finding.rule_details else "")
                + "</li>"
            )
        out.append("</ul>")
    out.append("</body></html>")
    return "\n".join(out)


def finding_html(finding: Finding, review: FileReview | None = None) -> str:
    """One finding, for the detail view beside the tree."""
    e = html.escape
    if not finding.parsed:
        return (
            f"<h3>Unreadable reply</h3><p>{e(UNREADABLE_NOTE)}</p>"
            f"<pre>{e(finding.raw_line)}</pre>"
        )
    unknown = (
        "<p><b>This rule id is not in the table.</b> The model either invented "
        "it or spelled it beyond recognition.</p>"
        if not finding.rule_known
        else ""
    )
    body = [
        f"<h3>{e(finding.rule_id)} — {e(finding.path)} ({_where(finding)})</h3>",
        unknown,
        f"<p>{e(finding.message) or '<i>(no explanation given)</i>'}</p>",
    ]
    if finding.rule_details:
        body.append(f"<p><b>Rule:</b> {e(finding.rule_details)}</p>")
    if finding.raw_line:
        body.append(f"<p><b>As the model wrote it:</b></p><pre>{e(finding.raw_line)}</pre>")
    if review is not None and review.note():
        body.append(f"<p><i>{e(review.note())}</i></p>")
    return "".join(body)
