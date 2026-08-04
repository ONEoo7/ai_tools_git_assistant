"""Rendering a report: Markdown to keep, HTML to read, plain text to paste.

Markdown is the export format because it survives being pasted anywhere and
stays readable unrendered. The HTML is for the panel's viewer and for Word,
which opens it with the headings and tables intact -- which is why it uses
table borders and no colours: it has to look right in a dark viewer and on a
white page.
"""

from __future__ import annotations

import html

from git_assistant.agents.base import Report, Section

#: Said once, under a section whose prose came from the measurements after the
#: model wrote a figure that was never measured.
UNVERIFIED_NOTE = (
    "Written from the measurements: the model's draft quoted a figure that is "
    "not in this report."
)


# ---- markdown ---------------------------------------------------------------
def to_markdown(report: Report) -> str:
    out: list[str] = [f"# {report.title}", "", f"**{report.subtitle}**", ""]
    where = f"{report.commit_line()} — " if report.commit_line() else ""
    out.append(f"Generated {report.generated_at} — {where}`{report.repo_path}`")
    out.append("")
    if report.warnings:
        out.append("> " + "  \n> ".join(report.warnings))
        out.append("")
    for section in report.sections:
        out.extend(_section_md(section, depth=2))
    return "\n".join(out).rstrip() + "\n"


def _section_md(section: Section, depth: int) -> list[str]:
    out = ["#" * depth + f" {section.number} {section.title}", ""]
    if section.prose:
        out += [section.prose, ""]
        if not section.prose_verified:
            out += [f"*{UNVERIFIED_NOTE}*", ""]
    if section.facts:
        out += ["| Item | Value |", "| --- | --- |"]
        out += [f"| {f.label} | {f.value} |" for f in section.facts]
        out.append("")
    for table in section.tables:
        out += _table_md(table)
    for caption, block in section.commands:
        out += [caption, "", "```bash", block.strip(), "```", ""]
    for child in section.sections:
        out += _section_md(child, depth + 1)
    return out


def _table_md(table) -> list[str]:
    out = [f"**{table.title}**", ""] if table.title else []
    out.append("| " + " | ".join(table.columns) + " |")
    out.append("| " + " | ".join("---" for _ in table.columns) + " |")
    for row in table.rows:
        out.append("| " + " | ".join(str(cell) for cell in row) + " |")
    out.append("")
    if table.note:
        out += [f"*{table.note}*", ""]
    return out


# ---- html -------------------------------------------------------------------
def to_html(report: Report) -> str:
    e = html.escape
    out = [
        "<html><body>",
        f"<h1>{e(report.title)}</h1>",
        f"<p><b>{e(report.subtitle)}</b><br>",
        f"<span>Generated {e(report.generated_at)} — "
        + (f"{e(report.commit_line())} — " if report.commit_line() else "")
        + f"<code>{e(report.repo_path)}</code></span></p>",
    ]
    for warning in report.warnings:
        out.append(f"<p><i>{e(warning)}</i></p>")
    for section in report.sections:
        out += _section_html(section, depth=2)
    out.append("</body></html>")
    return "\n".join(out)


def _section_html(section: Section, depth: int) -> list[str]:
    e = html.escape
    tag = f"h{min(depth, 6)}"
    out = [f"<{tag}>{e(section.number)} {e(section.title)}</{tag}>"]
    if section.prose:
        for para in section.prose.split("\n\n"):
            if para.strip():
                out.append(f"<p>{e(para.strip())}</p>")
        if not section.prose_verified:
            out.append(f"<p><i>{e(UNVERIFIED_NOTE)}</i></p>")
    if section.facts:
        rows = "".join(
            f"<tr><td>{e(f.label)}</td><td>{e(f.value)}</td></tr>"
            for f in section.facts
        )
        out.append(_table_tag(["Item", "Value"], rows))
    for table in section.tables:
        if table.title:
            out.append(f"<p><b>{e(table.title)}</b></p>")
        rows = "".join(
            "<tr>" + "".join(f"<td>{e(str(cell))}</td>" for cell in row) + "</tr>"
            for row in table.rows
        )
        out.append(_table_tag(table.columns, rows))
        if table.note:
            out.append(f"<p><i>{e(table.note)}</i></p>")
    for caption, block in section.commands:
        out.append(f"<p>{e(caption)}</p>")
        out.append(f"<pre>{e(block.strip())}</pre>")
    for child in section.sections:
        out += _section_html(child, depth + 1)
    return out


def _table_tag(columns: list[str], rows_html: str) -> str:
    e = html.escape
    head = "".join(f"<th align='left'>{e(c)}</th>" for c in columns)
    return (
        "<table border='1' cellspacing='0' cellpadding='4' width='100%'>"
        f"<tr>{head}</tr>{rows_html}</table>"
    )


# ---- plain text -------------------------------------------------------------
def to_text(report: Report) -> str:
    """Markdown with the table pipes kept -- they align well enough to read."""
    text = to_markdown(report)
    return text.replace("```bash\n", "").replace("```", "").replace("**", "")
