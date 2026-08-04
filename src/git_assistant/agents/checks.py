"""The configuration checks, one function each.

Every verdict is decided here, in Python, and so is every remediation string:
the rules are known exactly, and asking a model to restate them is fifteen
chances to get one wrong. The model's only job in this report is the prose that
sits above the findings.

Each check takes the shared :class:`RepoProbe` and returns one
:class:`CheckResult`. Adding a check means writing a function and listing it in
``CHECKS``.
"""

from __future__ import annotations

import re
import sys

from git_assistant.agents.base import CheckResult, Status
from git_assistant.agents.facts import human_bytes
from git_assistant.agents.probe import RepoProbe, working_tree_has

#: Anything above this belongs in LFS whatever it is; git slows down and every
#: clone pays for it forever.
BIG_FILE = 50 * 1024 * 1024
#: Formats that do not diff, do not merge and do not compress in a pack.
BINARY_EXTS = {
    ".7z", ".ai", ".avi", ".bin", ".blend", ".bmp", ".bz2", ".dll", ".dmg",
    ".doc", ".docx", ".dylib", ".eapx", ".exe", ".fbx", ".gz", ".ico", ".iso",
    ".jar", ".jpeg", ".jpg", ".lib", ".mov", ".mp3", ".mp4", ".msi", ".obj",
    ".odt", ".pdb", ".pdf", ".png", ".ppt", ".pptx", ".psd", ".qea", ".rar",
    ".so", ".tar", ".tif", ".tiff", ".ttf", ".unitypackage", ".wav", ".woff",
    ".woff2", ".xls", ".xlsx", ".zip",
}
LFS_KEYS = ("filter.lfs.process", "filter.lfs.clean", "filter.lfs.smudge")
#: Shell scripts break on CRLF; batch and PowerShell hosts mis-parse LF.
LF_SCRIPTS = (".sh", ".bash", ".zsh")
LF_SCRIPT_NAMES = {"configure", "gradlew"}
CRLF_SCRIPTS = (".bat", ".cmd", ".ps1")
EVIDENCE_MAX = 8
LOOSE_OBJECT_WARN = 5000


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    return name[dot:].lower() if dot > 0 else ""


def _evidence(items: list[str]) -> list[str]:
    shown = [str(i)[:160] for i in items[:EVIDENCE_MAX]]
    if len(items) > EVIDENCE_MAX:
        shown.append(f"... and {len(items) - EVIDENCE_MAX} more")
    return shown


def _scope_line(key: str, scope: str, value: str) -> str:
    return f"{key} = {value}  ({scope})"


# ---- Git LFS -----------------------------------------------------------------
def lfs_installed(probe: RepoProbe) -> CheckResult:
    rules = probe.has_lfs_rules()
    registered = [k for k in LFS_KEYS if probe.values(k)]
    if not rules and not probe.lfs_version:
        return CheckResult(
            "LFS-01",
            "Git LFS is available where it is needed",
            Status.SKIP,
            "This repository does not route anything through Git LFS.",
            weight=3,
        )
    if rules and not probe.lfs_version:
        return CheckResult(
            "LFS-01",
            "Git LFS is available where it is needed",
            Status.FAIL,
            "Files are routed through LFS but git-lfs is not installed — "
            "checkouts here get pointer text instead of file contents.",
            _evidence(sorted(probe.lfs_paths())),
            "Install Git LFS, then: git lfs install",
            weight=3,
        )
    if rules and not registered:
        return CheckResult(
            "LFS-01",
            "Git LFS is available where it is needed",
            Status.WARN,
            "git-lfs is installed but its filters are not registered in any "
            "git config, so it will not run on checkout.",
            [probe.lfs_version],
            "git lfs install",
            weight=3,
        )
    return CheckResult(
        "LFS-01",
        "Git LFS is available where it is needed",
        Status.PASS,
        f"{probe.lfs_version or 'git-lfs'} is installed and its filters are "
        "registered.",
        _evidence([_scope_line(k, s, v) for k, s, v in probe.matching(r"^filter\.lfs\.")]),
        weight=3,
    )


def lfs_attributes_tracked(probe: RepoProbe) -> CheckResult:
    on_disk = working_tree_has(probe.repo, ".gitattributes")
    tracked = ".gitattributes" in probe.tracked
    title = ".gitattributes is committed, not just present"
    if tracked:
        return CheckResult(
            "LFS-02", title, Status.PASS,
            "The repository's attributes travel with it.", weight=3,
        )
    if on_disk:
        return CheckResult(
            "LFS-02", title, Status.FAIL,
            "A .gitattributes exists but is not tracked, so its rules apply on "
            "this machine only and nowhere else.",
            [".gitattributes (untracked)"],
            "git add .gitattributes && git commit -m "
            '"chore: track attributes"',
            weight=3,
        )
    return CheckResult(
        "LFS-02", title, Status.SKIP,
        "There is no .gitattributes in this repository.", weight=3,
    )


def lfs_coverage(probe: RepoProbe, large_mb: int = 5) -> CheckResult:
    threshold = max(1, large_mb) * 1024 * 1024
    huge: list[str] = []
    large: list[str] = []
    for path, size in probe.sizes.items():
        if probe.attr(path, "filter") == "lfs":
            continue
        if size >= BIG_FILE:
            huge.append(f"{path} — {human_bytes(size)}")
        elif size >= threshold and _ext(path) in BINARY_EXTS:
            large.append(f"{path} — {human_bytes(size)}")
    title = "Large and binary files are routed through LFS"
    fix = (
        'git lfs track "*.<ext>"  # then commit .gitattributes\n'
        "# existing history is not converted by this; see git lfs migrate import"
    )
    if huge:
        return CheckResult(
            "LFS-03", title, Status.FAIL,
            f"{len(huge)} tracked file(s) of {human_bytes(BIG_FILE)} or more are "
            "stored directly in the repository.",
            _evidence(sorted(huge, reverse=True)), fix, weight=3,
        )
    if large:
        return CheckResult(
            "LFS-03", title, Status.WARN,
            f"{len(large)} binary file(s) over {large_mb} MB are stored directly "
            "in the repository.",
            _evidence(sorted(large, reverse=True)), fix, weight=3,
        )
    return CheckResult(
        "LFS-03", title, Status.PASS,
        "No oversized or binary file is stored outside LFS.", weight=3,
    )


def lfs_pointers_are_pointers(probe: RepoProbe) -> CheckResult:
    """The retroactivity trap: an LFS rule does not convert what is already in."""
    paths = probe.lfs_paths()
    title = "Files at LFS paths are stored as pointers"
    if not paths:
        return CheckResult(
            "LFS-04", title, Status.SKIP, "Nothing is routed through LFS.", weight=3
        )
    raw = [
        f"{p} — {human_bytes(probe.sizes[p])}"
        for p in paths
        if probe.sizes.get(p, 0) > 200
    ]
    if raw:
        return CheckResult(
            "LFS-04", title, Status.FAIL,
            f"{len(raw)} file(s) match an LFS rule but are stored as full "
            "content — LFS filters only apply to commits made after the rule "
            "was added.",
            _evidence(sorted(raw, reverse=True)),
            "git lfs migrate import --include=\"<pattern>\" --everything\n"
            "# rewrites history: force-push, and everyone re-clones",
            weight=3,
        )
    return CheckResult(
        "LFS-04", title, Status.PASS,
        f"All {len(paths)} LFS-tracked file(s) are pointers at HEAD.", weight=3,
    )


def lfs_settings(probe: RepoProbe) -> CheckResult:
    title = "LFS fetch settings do not hide missing files"
    if not probe.has_lfs_rules():
        return CheckResult("LFS-05", title, Status.SKIP, "Nothing is routed through LFS.", weight=1)
    filters = probe.matching(r"^lfs\.fetch(include|exclude)$")
    if filters:
        return CheckResult(
            "LFS-05", title, Status.WARN,
            "A fetch filter is configured, so clones here are missing LFS "
            "content without saying so.",
            [_scope_line(k, s, v) for k, s, v in filters],
            "git config --unset lfs.fetchexclude   # and/or lfs.fetchinclude",
            weight=1,
        )
    return CheckResult("LFS-05", title, Status.PASS, "No LFS fetch filter is set.", weight=1)


# ---- line endings ------------------------------------------------------------
#: A rule that decides text handling. `-text` is the opposite -- it marks a file
#: binary -- so it must not count as "line endings are declared".
_TEXT_RULE_RE = re.compile(r"^\s*(\S+)\s+(.*(?<!-)\btext\b.*)$")


def eol_declared(probe: RepoProbe) -> CheckResult:
    title = "Line endings are declared in .gitattributes"
    rules: list[str] = []
    catch_all = False
    for path, text in probe.attributes_files.items():
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = _TEXT_RULE_RE.match(line)
            if not match:
                continue
            rules.append(f"{path}: {line.strip()}")
            if match.group(1) in ("*", "**"):
                catch_all = True
    if catch_all:
        return CheckResult(
            "EOL-01", title, Status.PASS,
            "A catch-all text rule decides line endings for every file.",
            _evidence(rules), weight=3,
        )
    if rules:
        return CheckResult(
            "EOL-01", title, Status.WARN,
            "Some patterns declare text handling, but there is no catch-all "
            "rule, so anything unmatched follows each machine's config.",
            _evidence(rules),
            "Add to .gitattributes:  * text=auto eol=lf",
            weight=3,
        )
    return CheckResult(
        "EOL-01", title, Status.FAIL,
        "Nothing declares how line endings are stored, so they are decided by "
        "each contributor's git config — the same edit produces a whole-file "
        "diff depending on who made it.",
        [],
        "Add to .gitattributes:  * text=auto eol=lf\n"
        "then: git add --renormalize .",
        weight=3,
    )


def eol_config_scope(probe: RepoProbe) -> CheckResult:
    """The question the user asked: is this decided per repo, or per machine?"""
    title = "Line endings are not left to per-machine config"
    found = probe.matching(r"^core\.(autocrlf|eol|safecrlf)$")
    declared = any(
        _TEXT_RULE_RE.match(line)
        for text in probe.attributes_files.values()
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
    )
    local = [(k, s, v) for k, s, v in found if s in ("local", "worktree")]
    if local and not declared:
        return CheckResult(
            "EOL-02", title, Status.FAIL,
            "Line endings are set in this clone's own config instead of in "
            ".gitattributes, so the answer lives on this machine and no-one "
            "else gets it.",
            [_scope_line(k, s, v) for k, s, v in local],
            "git config --unset core.autocrlf\n"
            "and put the rule in .gitattributes instead:  * text=auto eol=lf",
            weight=3,
        )
    if found and not declared:
        return CheckResult(
            "EOL-02", title, Status.WARN,
            "Nothing in the repository declares line endings, so this machine's "
            "config decides them.",
            [_scope_line(k, s, v) for k, s, v in found],
            "Add to .gitattributes:  * text=auto eol=lf",
            weight=3,
        )
    if found:
        return CheckResult(
            "EOL-02", title, Status.PASS,
            ".gitattributes decides, and it outranks the config below.",
            [_scope_line(k, s, v) for k, s, v in found],
            weight=3,
        )
    return CheckResult(
        "EOL-02", title, Status.PASS,
        "No line-ending config is set at any scope.", weight=3,
    )


def eol_index_clean(probe: RepoProbe) -> CheckResult:
    title = "The index stores one line ending"
    crlf = [p for p, (index, _w, _a) in probe.eol.items() if index == "crlf"]
    mixed = [p for p, (index, _w, _a) in probe.eol.items() if index == "mixed"]
    if crlf:
        return CheckResult(
            "EOL-03", title, Status.FAIL,
            f"{len(crlf)} file(s) are stored with CRLF in the index. The next "
            "commit that touches one normalizes it, turning a small edit into a "
            "whole-file diff.",
            _evidence(sorted(crlf)),
            "git add --renormalize .   # one deliberate commit, once",
            weight=3,
        )
    if mixed:
        return CheckResult(
            "EOL-03", title, Status.WARN,
            f"{len(mixed)} file(s) have mixed line endings in the index.",
            _evidence(sorted(mixed)),
            "git add --renormalize .",
            weight=3,
        )
    return CheckResult(
        "EOL-03", title, Status.PASS,
        "Every tracked text file is stored with LF.", weight=3,
    )


def eol_scripts(probe: RepoProbe) -> CheckResult:
    title = "Scripts get the line endings their interpreter needs"
    wrong: list[str] = []
    for path in probe.tracked:
        name = path.rsplit("/", 1)[-1]
        ext = _ext(path)
        eol = probe.attr(path, "eol")
        if (ext in LF_SCRIPTS or name in LF_SCRIPT_NAMES) and eol != "lf":
            wrong.append(f"{path} — needs eol=lf, has {eol}")
        elif ext in CRLF_SCRIPTS and eol != "crlf":
            wrong.append(f"{path} — needs eol=crlf, has {eol}")
    if wrong:
        return CheckResult(
            "EOL-04", title, Status.WARN,
            f"{len(wrong)} script(s) may be checked out with endings their "
            "interpreter cannot parse.",
            _evidence(sorted(wrong)),
            "Add to .gitattributes:\n"
            "*.sh text eol=lf\n*.bat text eol=crlf\n*.cmd text eol=crlf\n"
            "*.ps1 text eol=crlf",
            weight=2,
        )
    return CheckResult("EOL-04", title, Status.PASS, "No script is at risk.", weight=2)


# ---- hygiene -----------------------------------------------------------------
def ignored_but_tracked(probe: RepoProbe) -> CheckResult:
    title = "Nothing is tracked despite being ignored"
    if probe.ignored:
        return CheckResult(
            "HYG-01", title, Status.WARN,
            f"{len(probe.ignored)} tracked file(s) match .gitignore. The ignore "
            "rule does nothing for a file already in the index, so these keep "
            "being committed.",
            _evidence(sorted(probe.ignored)),
            "git rm --cached <path>   # keeps the file on disk",
            weight=2,
        )
    return CheckResult("HYG-01", title, Status.PASS, "No tracked file is ignored.", weight=2)


def object_store_hygiene(probe: RepoProbe) -> CheckResult:
    title = "The object store is maintained"
    counts = probe.counts
    problems: list[str] = []
    if counts.get("garbage", 0):
        problems.append(
            f"garbage: {counts['garbage']} object(s), "
            f"{human_bytes(counts.get('size-garbage', 0))}"
        )
    loose = counts.get("count", 0)
    if loose > LOOSE_OBJECT_WARN:
        problems.append(f"loose objects: {loose:,}")
    if probe.value("gc.auto") == "0":
        problems.append("gc.auto = 0 (automatic maintenance is off)")
    if problems:
        return CheckResult(
            "HYG-02", title, Status.WARN,
            "The object store has work waiting that git would normally do "
            "for itself.",
            problems,
            "git gc\n# leftover temporary packs are reported by the size audit",
            weight=2,
        )
    return CheckResult(
        "HYG-02", title, Status.PASS,
        f"{loose:,} loose object(s), no garbage.", weight=2,
    )


# ---- secrets and portability --------------------------------------------------
_CREDENTIAL_URL_RE = re.compile(r"^(?P<scheme>\w+://)(?P<user>[^/@:]+):(?P<secret>[^/@]+)@")


def redact_url(url: str) -> str:
    return _CREDENTIAL_URL_RE.sub(r"\g<scheme>\g<user>:***@", url)


def credentials_in_remote(probe: RepoProbe) -> CheckResult:
    title = "No credentials are stored in a remote URL"
    bad = [
        (key, scope, value)
        for key, scope, value in probe.matching(r"^remote\..*\.url$")
        if _CREDENTIAL_URL_RE.match(value)
    ]
    if bad:
        return CheckResult(
            "SEC-01", title, Status.FAIL,
            "A remote URL carries a password in clear text in a config file, "
            "where any tool that prints the remote will show it.",
            # Redacted here, before it can reach the report, an export or a model.
            [_scope_line(k, s, redact_url(v)) for k, s, v in bad],
            "git remote set-url origin https://host/org/repo.git\n"
            "and let the credential manager hold the secret",
            weight=3,
        )
    return CheckResult("SEC-01", title, Status.PASS, "No remote URL contains a secret.", weight=3)


def filemode_on_windows(probe: RepoProbe) -> CheckResult:
    title = "core.filemode suits the platform"
    if sys.platform != "win32":
        return CheckResult("WIN-01", title, Status.SKIP, "Not a Windows checkout.", weight=1)
    if probe.value("core.filemode") == "true":
        return CheckResult(
            "WIN-01", title, Status.WARN,
            "core.filemode is on under Windows, which reports permission "
            "changes git cannot actually see.",
            [_scope_line("core.filemode", s, v) for _k, s, v in probe.matching(r"^core\.filemode$")],
            "git config core.filemode false",
            weight=1,
        )
    return CheckResult("WIN-01", title, Status.PASS, "core.filemode is off.", weight=1)


def case_collisions(probe: RepoProbe) -> CheckResult:
    title = "No two paths differ only by case"
    seen: dict[str, str] = {}
    clashes: list[str] = []
    for path in probe.tracked:
        key = path.lower()
        if key in seen and seen[key] != path:
            clashes.append(f"{seen[key]}  vs  {path}")
        else:
            seen[key] = path
    if clashes:
        return CheckResult(
            "WIN-02", title, Status.FAIL,
            "Paths that differ only by case cannot both be checked out on "
            "Windows or macOS — one silently overwrites the other.",
            _evidence(clashes),
            "git mv one of the paths to a distinct name",
            weight=3,
        )
    return CheckResult("WIN-02", title, Status.PASS, "Every tracked path is distinct.", weight=3)


#: Every check, in the order they are defined. The report sorts by outcome.
CHECKS = (
    lfs_installed,
    lfs_attributes_tracked,
    lfs_coverage,
    lfs_pointers_are_pointers,
    lfs_settings,
    eol_declared,
    eol_config_scope,
    eol_index_clean,
    eol_scripts,
    ignored_but_tracked,
    object_store_hygiene,
    credentials_in_remote,
    filemode_on_windows,
    case_collisions,
)


def run_all(probe: RepoProbe, *, large_mb: int = 5) -> list[CheckResult]:
    results: list[CheckResult] = []
    for check in CHECKS:
        if check is lfs_coverage:
            results.append(check(probe, large_mb))
        else:
            results.append(check(probe))
    return results
