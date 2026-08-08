"""Where a repository's ``.git`` bytes went, and why LFS did not shrink them.

The expensive part is one pipeline:

    git rev-list --objects --all | git cat-file --batch-check=...

``rev-list --objects`` emits every reachable object once, each with the path it
was found at; separate versions of a file are separate objects, so summing by
path gives "every version ever committed" and counting rows gives the version
count. ``%(rest)`` carries the path through the pipe, which is what keeps this
streaming rather than holding a SHA-to-size dictionary for millions of objects.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from git_assistant import git_ops, metrics
from git_assistant.agents import gitstream
from git_assistant.agents.base import (
    INDETERMINATE,
    AgentContext,
    AgentInfo,
    Report,
    Section,
    Table,
)
from git_assistant.agents.facts import (
    count_fact,
    fact,
    human_bytes,
    human_count,
    percent,
    size_fact,
)

#: An LFS pointer file is about 130 bytes. Anything larger at an LFS-tracked
#: path is the file itself, stored in the object store the pointer was meant to
#: keep it out of.
POINTER_MAX = 200

TOP_PATHS = 10
TOP_EXTENSIONS = 15
TMP_PACK_ROWS = 20
#: Distinct paths kept before the tail is folded into per-directory rows. Bounds
#: memory on a repository with millions of files without losing the big ones.
MAX_PATHS = 300_000
PROGRESS_EVERY = 25_000

_BATCH_CHECK = "%(objectname) %(objecttype) %(objectsize) %(objectsize:disk) %(rest)"

DESCRIPTION = (
    "Measures where the .git directory's bytes are, separates leftover garbage "
    "from real history, ranks the paths and file types that dominate it, and "
    "explains what can be reclaimed now versus what needs a history rewrite."
)


@dataclass
class PathStat:
    versions: int = 0
    size: int = 0
    disk: int = 0
    largest: int = 0


@dataclass
class HistoryScan:
    """Totals over every object reachable from a ref."""

    by_path: dict[str, PathStat] = field(default_factory=dict)
    by_ext: dict[str, PathStat] = field(default_factory=dict)
    objects: int = 0
    blobs: int = 0
    blob_size: int = 0
    blob_disk: int = 0
    commits: int = 0
    trees: int = 0
    #: Blobs at a path an LFS rule claims, stored as content rather than as a
    #: pointer -- the backlog that `git lfs migrate import` exists to fix.
    lfs_raw_blobs: int = 0
    lfs_raw_size: int = 0
    truncated: bool = False


class SizeAuditAgent:
    info = AgentInfo(
        id="size-audit",
        label="Size",
        description=DESCRIPTION,
        cost_hint="Seconds on a small repository; minutes on a large one.",
    )

    def collect(self, ctx: AgentContext) -> Report:
        warnings: list[str] = []
        ident = _identify(ctx)
        git_dir = ident["git_dir"]

        ctx.say("Measuring the .git directory...")
        tmp_packs: list[tuple[str, int, float]] = []
        tree = _measure_git_dir(ctx, git_dir, tmp_packs)
        ctx.check_cancel()

        ctx.say("Reading the object store...")
        counts = _count_objects(ctx)
        reach = _reachability(ctx)
        ctx.check_cancel()

        lfs = _lfs_facts(ctx, tree)
        scan = _history(ctx, counts, lfs["patterns"], warnings)

        return _build(ctx, ident, tree, counts, tmp_packs, reach, lfs, scan, warnings)


# ---- phase 0: identify -------------------------------------------------------
def _identify(ctx: AgentContext) -> dict:
    repo = ctx.repo
    git_dir = git_ops._run(repo, ["rev-parse", "--absolute-git-dir"]).stdout.strip()
    if not git_dir:
        raise RuntimeError(
            f"{repo}\nis not a readable git repository (git could not find its "
            "git directory)."
        )
    shallow = git_ops._run(repo, ["rev-parse", "--is-shallow-repository"])
    lfs_version = git_ops._run_global(["lfs", "version"])
    return {
        "git_dir": git_dir,
        "name": Path(repo).name or repo,
        "shallow": shallow.stdout.strip() == "true",
        "branch": git_ops.current_branch(repo),
        "remote": git_ops.get_remote_url(repo) or "",
        "git_version": git_ops._run_global(["--version"]).stdout.strip(),
        "lfs_version": lfs_version.stdout.strip() if lfs_version.ok else "",
        "submodules": len(git_ops.find_submodules(repo)),
    }


# ---- phase 1: what is on disk ------------------------------------------------
def _bucket_of(parts: tuple[str, ...]) -> str:
    head = parts[0]
    if head == "objects":
        return "objects/pack" if len(parts) > 1 and parts[1] == "pack" else "objects"
    if head in ("modules", "lfs", "worktrees", "logs"):
        return head
    return "other"


BUCKET_LABELS = {
    "objects/pack": "objects/pack (packed history)",
    "objects": "objects (loose)",
    "modules": "modules (submodule git dirs)",
    "lfs": "lfs (local LFS cache)",
    "worktrees": "worktrees",
    "logs": "logs (reflogs)",
    "other": "everything else (refs, index, config)",
}


def _measure_git_dir(ctx: AgentContext, git_dir: str, tmp_packs: list) -> gitstream.TreeSize:
    def note_file(parts: tuple[str, ...], size: int, mtime: float) -> None:
        if parts[-1].startswith("tmp_pack_"):
            tmp_packs.append(("/".join(parts), size, mtime))

    return gitstream.measure_tree(
        git_dir,
        _bucket_of,
        on_file=note_file,
        on_progress=lambda t: ctx.say(
            f"Measuring the .git directory: {human_bytes(t.total)} so far"
        ),
        should_stop=ctx.is_cancelled,
    )


# ---- phase 2: the object store's own accounting -------------------------------
def _count_objects(ctx: AgentContext) -> dict[str, int]:
    """`git count-objects -v`, with its KiB fields converted to bytes."""
    res = git_ops._run(ctx.repo, ["count-objects", "-v"])
    out: dict[str, int] = {}
    for line in res.stdout.splitlines():
        key, _, value = line.partition(":")
        try:
            out[key.strip()] = int(value.strip())
        except ValueError:
            continue
    for key in ("size", "size-pack", "size-garbage"):
        if key in out:
            out[key] *= 1024
    return out


# ---- phase 3: what is reachable ----------------------------------------------
def _ref_count(repo: str, namespace: str) -> int:
    res = git_ops._run(repo, ["for-each-ref", "--format=%(refname)", namespace])
    return len([line for line in res.stdout.splitlines() if line.strip()])


def _reachability(ctx: AgentContext) -> dict:
    repo = ctx.repo
    commits = git_ops._run(repo, ["rev-list", "--all", "--count"]).stdout.strip()
    first = last = ""
    with gitstream.streamed(
        repo, ["log", "--all", "--format=%ad", "--date=short"]
    ) as lines:
        for line in lines:
            if not line:
                continue
            if not last:
                last = line  # newest first
            first = line
    return {
        "commits": int(commits) if commits.isdigit() else 0,
        "branches": _ref_count(repo, "refs/heads"),
        "remote_branches": _ref_count(repo, "refs/remotes"),
        "tags": _ref_count(repo, "refs/tags"),
        "first_commit": first,
        "last_commit": last,
    }


# ---- phase 4: every version of every file ------------------------------------
def _parse_line(line: str) -> tuple[str, int, int, str] | None:
    """``<sha> <type> <size> <disk> <path>`` -> ``(type, size, disk, path)``."""
    parts = line.split(" ", 4)
    if len(parts) < 4:
        return None
    _sha, kind, size, disk = parts[0], parts[1], parts[2], parts[3]
    try:
        return kind, int(size), int(disk), parts[4] if len(parts) > 4 else ""
    except ValueError:
        return None  # "<sha> missing" and other non-answers


def _history(
    ctx: AgentContext, counts: dict, lfs_patterns: list[str], warnings: list[str]
) -> HistoryScan:
    total = counts.get("count", 0) + counts.get("in-pack", 0)
    scan = HistoryScan()
    if ctx.fast:
        args = [
            "cat-file",
            "--batch-all-objects",
            "--unordered",
            "--batch-check=%(objectname) %(objecttype) %(objectsize) %(objectsize:disk)",
        ]
        with gitstream.streamed(ctx.repo, args) as lines:
            _consume(ctx, lines, scan, total, lfs_patterns, per_path=False)
        warnings.append(
            "Fast mode: totals only. The per-file and per-extension breakdowns "
            "need the full history scan."
        )
        return scan

    with gitstream.piped(
        ctx.repo,
        ["-c", "core.quotePath=false", "rev-list", "--objects", "--all"],
        ["cat-file", f"--batch-check={_BATCH_CHECK}", "--buffer"],
    ) as lines:
        _consume(ctx, lines, scan, total, lfs_patterns, per_path=True)
    if scan.truncated:
        warnings.append(
            f"More than {human_count(MAX_PATHS)} distinct paths: the smallest "
            "are grouped by directory."
        )
    return scan


def _consume(
    ctx: AgentContext,
    lines,
    scan: HistoryScan,
    total: int,
    lfs_patterns: list[str],
    *,
    per_path: bool,
) -> None:
    for line in lines:
        scan.objects += 1
        if scan.objects % gitstream.CANCEL_EVERY == 0:
            ctx.check_cancel()
        if scan.objects % PROGRESS_EVERY == 0:
            pct = int(100 * scan.objects / total) if total else INDETERMINATE
            ctx.say(
                f"Scanning history: {human_count(scan.objects)} objects"
                + (f" of ~{human_count(total)}" if total else "")
                + f" — {human_bytes(scan.blob_size)} of content",
                min(pct, 99) if total else INDETERMINATE,
            )
        parsed = _parse_line(line)
        if parsed is None:
            continue
        kind, size, disk, path = parsed
        if kind == "commit":
            scan.commits += 1
            continue
        if kind == "tree":
            scan.trees += 1
            continue
        if kind != "blob":
            continue
        scan.blobs += 1
        scan.blob_size += size
        scan.blob_disk += disk
        if not per_path or not path:
            continue
        _record(scan.by_path, path, size, disk, cap=MAX_PATHS)
        _record(scan.by_ext, metrics.ext_of(path), size, disk)
        if size > POINTER_MAX and _matches_any(lfs_patterns, path):
            scan.lfs_raw_blobs += 1
            scan.lfs_raw_size += size


def _record(
    table: dict[str, PathStat], key: str, size: int, disk: int, cap: int = 0
) -> None:
    stat = table.get(key)
    if stat is None:
        if cap and len(table) >= cap:
            key = _fold(key)
            stat = table.get(key)
        if stat is None:
            stat = PathStat()
            table[key] = stat
    stat.versions += 1
    stat.size += size
    stat.disk += disk
    stat.largest = max(stat.largest, size)


def _fold(path: str) -> str:
    head, _, _ = path.partition("/")
    return f"{head}/..." if head != path else "(top level)"


# ---- phase 5: LFS -------------------------------------------------------------
_LFS_RULE_RE = re.compile(r"^\s*(\S+)\s+(.*filter=lfs.*)$")


def _lfs_patterns(ctx: AgentContext) -> tuple[list[str], int]:
    """Patterns routed through LFS, read from the index's .gitattributes files."""
    listed = git_ops._run(ctx.repo, ["ls-files", "-z"])
    names = [
        p
        for p in listed.stdout.split("\0")
        if p.rsplit("/", 1)[-1] == ".gitattributes"
    ]
    patterns: list[str] = []
    rules = 0
    for name in dict.fromkeys(names):
        shown = git_ops._run(ctx.repo, ["show", f":{name}"])
        if not shown.ok:
            continue
        prefix = os.path.dirname(name)
        for line in shown.stdout.splitlines():
            if line.lstrip().startswith("#"):
                continue
            match = _LFS_RULE_RE.match(line)
            if not match:
                continue
            rules += 1
            pattern = match.group(1)
            patterns.append(f"{prefix}/{pattern}" if prefix else pattern)
    return patterns, rules


def _matches_any(patterns: list[str], path: str) -> bool:
    """Approximate gitattributes matching: no slash means match the basename.

    An approximation on purpose -- `git check-attr` answers exactly, but only
    for paths that still exist, and this has to judge paths that were deleted
    years ago. Stated as such in the report.
    """
    name = path.rsplit("/", 1)[-1]
    for pattern in patterns:
        bare = pattern.lstrip("/")
        if "/" in bare:
            if fnmatch.fnmatch(path, bare) or fnmatch.fnmatch(path, f"*/{bare}"):
                return True
        elif fnmatch.fnmatch(name, bare):
            return True
    return False


def _lfs_facts(ctx: AgentContext, tree: gitstream.TreeSize) -> dict:
    patterns, rules = _lfs_patterns(ctx)
    added = ""
    pointers = None
    if patterns:
        # Only worth asking when something is routed through LFS: `git lfs
        # ls-files` walks the index through a second binary, and on a large
        # repository that is a wait for an answer that would be "none".
        ctx.say("Reading Git LFS state...")
        with gitstream.streamed(
            ctx.repo,
            [
                "log",
                "--diff-filter=A",
                "--format=%ad",
                "--date=short",
                "--",
                ".gitattributes",
            ],
        ) as lines:
            for line in lines:
                if line.strip():
                    added = line.strip()  # the last one is when it first appeared
        listed = git_ops._run(ctx.repo, ["lfs", "ls-files", "-n"])
        pointers = len(listed.stdout.split("\n")) - 1 if listed.ok else None
    return {
        "patterns": patterns,
        "rules": rules,
        "attributes_added": added,
        "pointers": pointers,
        "cache_size": tree.buckets.get("lfs", 0),
    }


# ---- the report ---------------------------------------------------------------
def _build(
    ctx: AgentContext,
    ident: dict,
    tree: gitstream.TreeSize,
    counts: dict,
    tmp_packs: list,
    reach: dict,
    lfs: dict,
    scan: HistoryScan,
    warnings: list[str],
) -> Report:
    garbage_size = counts.get("size-garbage", 0)
    tmp_total = sum(size for _n, size, _m in tmp_packs)
    # `count-objects` only knows the repository's own object store; leftovers
    # inside submodule git dirs are just as reclaimable, so both are reported.
    reclaimable = max(garbage_size, tmp_total)
    top_paths = sorted(scan.by_path.items(), key=lambda kv: -kv[1].size)[:TOP_PATHS]
    dominant = top_paths[0] if top_paths else None

    summary = Section(
        number="1",
        title="Executive summary",
        slot="exec_summary",
        facts=[
            size_fact("git_dir_total", "Total .git size", tree.total),
            size_fact("reclaimable_now", "Reclaimable without a rewrite", reclaimable),
            fact(
                "reclaimable_share",
                "Share of .git that is reclaimable now",
                percent(reclaimable, tree.total),
            ),
            size_fact("history_content", "Content across all versions", scan.blob_size),
            count_fact("reachable_commits", "Reachable commits", reach["commits"]),
        ],
    )
    if dominant:
        path, stat = dominant
        summary.facts += [
            fact("dominant_path", "Largest path in history", path),
            size_fact("dominant_path_total", "Its total across all versions", stat.size),
            count_fact("dominant_path_versions", "Its committed versions", stat.versions),
            fact(
                "dominant_path_share",
                "Its share of all content",
                percent(stat.size, scan.blob_size),
            ),
        ]
    if scan.lfs_raw_size:
        summary.facts.append(
            size_fact(
                "lfs_raw_size",
                "Content at LFS-tracked paths stored as full files",
                scan.lfs_raw_size,
            )
        )

    where = Section(
        number="2",
        title=f"Where the {human_bytes(tree.total)} goes",
        slot="where",
        tables=[
            Table(
                title="",
                columns=["Location", "Size", "Share"],
                rows=[
                    [
                        BUCKET_LABELS.get(name, name),
                        human_bytes(size),
                        percent(size, tree.total),
                    ]
                    for name, size in sorted(tree.buckets.items(), key=lambda kv: -kv[1])
                ],
            )
        ],
        facts=[count_fact("git_dir_files", "Files inside .git", tree.files)],
    )

    where.sections.append(_garbage_section(ctx, counts, tmp_packs, tmp_total, reclaimable))
    where.sections.append(_history_section(reach, scan, top_paths, counts, tree))

    root_cause = Section(
        number="3",
        title="Root cause: LFS rules are not retroactive",
        slot="root_cause",
        facts=[
            count_fact("lfs_rules", "LFS rules in .gitattributes", lfs["rules"]),
            fact(
                "lfs_attributes_added",
                "First .gitattributes commit",
                lfs["attributes_added"] or "not found",
            ),
            size_fact("lfs_cache", "Local LFS cache (.git/lfs)", lfs["cache_size"]),
            fact(
                "lfs_pointers",
                "Files stored in LFS at HEAD",
                human_count(lfs["pointers"]) if lfs["pointers"] is not None else "n/a",
            ),
            count_fact(
                "lfs_raw_blobs", "Historical versions stored raw", scan.lfs_raw_blobs
            ),
            size_fact("lfs_raw_total", "Their total content", scan.lfs_raw_size),
        ],
    )

    pack_dir = Path(ident["git_dir"]) / "objects" / "pack"
    remove = (
        f'del /q "{pack_dir}\\tmp_pack_*"'
        if os.name == "nt"
        else f'rm -f "{pack_dir}"/tmp_pack_*'
    )
    next_steps = Section(number="4", title="Recommended next steps", slot="next_steps")
    next_steps.sections.append(
        Section(
            number="4.1",
            title="Do now — low risk, no history rewrite",
            commands=[
                (
                    "Confirm nothing is running against the repository, remove the "
                    "leftover temporary packs, then let git repack:",
                    f'git -C "{ctx.repo}" count-objects -v\n'
                    f"{remove}\n"
                    f'git -C "{ctx.repo}" gc --prune=now',
                )
            ],
        )
    )
    next_steps.sections.append(
        Section(
            number="4.2",
            title="Plan separately — disruptive",
            commands=[
                (
                    "Rewrites history: every commit hash after the migration point "
                    "changes, the remote needs a force-push, and everyone re-clones. "
                    "Run on a fresh clone, in a low-activity window:",
                    'git lfs migrate import --include="*.eapx,*.qea" --everything',
                )
            ],
        )
    )

    repo_facts = Section(
        number="5",
        title="Repository facts",
        facts=[
            fact("repo_path", "Path", ctx.repo),
            fact("repo_branch", "Checked-out branch", ident["branch"]),
            fact("repo_remote", "Remote", ident["remote"] or "none"),
            size_fact("git_dir_bytes", "Total .git size", tree.total),
            fact("git_dir_exact", "Exact bytes", f"{tree.total:,} bytes"),
            count_fact("commits", "Reachable commits", reach["commits"]),
            count_fact("branches", "Local branches", reach["branches"]),
            count_fact("remote_branches", "Remote-tracking branches", reach["remote_branches"]),
            count_fact("tags", "Tags", reach["tags"]),
            fact("first_commit", "First commit", reach["first_commit"] or "n/a"),
            fact("last_commit", "Last commit", reach["last_commit"] or "n/a"),
            fact("shallow", "Shallow clone", "yes" if ident["shallow"] else "no"),
            count_fact("submodules", "Submodules", ident["submodules"]),
            fact("git_version", "git", ident["git_version"] or "unknown"),
            fact("lfs_version", "git-lfs", ident["lfs_version"] or "not installed"),
        ],
    )

    return Report(
        agent_id=SizeAuditAgent.info.id,
        title="Git repository size audit",
        subtitle=f"{ident['name']} — findings and recommendations",
        generated_at=datetime.now().strftime("%d %B %Y %H:%M"),
        repo_path=ctx.repo,
        sections=[summary, where, root_cause, next_steps, repo_facts],
        warnings=warnings,
    )


def _garbage_section(
    ctx: AgentContext, counts: dict, tmp_packs: list, tmp_total: int, reclaimable: int
) -> Section:
    rows = [
        [name, human_bytes(size), datetime.fromtimestamp(mtime).strftime("%d %b %Y")]
        for name, size, mtime in sorted(tmp_packs, key=lambda t: -t[1])[:TMP_PACK_ROWS]
    ]
    extra = len(tmp_packs) - len(rows)
    if extra > 0:
        rows.append([f"... and {extra} more", "", ""])
    section = Section(
        number="2.1",
        title="Orphaned data — reclaimable now",
        slot="garbage",
        facts=[
            count_fact("garbage_objects", "Objects git calls garbage", counts.get("garbage", 0)),
            size_fact("garbage_size", "Their size", counts.get("size-garbage", 0)),
            count_fact("tmp_packs", "Leftover temporary pack files", len(tmp_packs)),
            size_fact("tmp_pack_size", "Their size", tmp_total),
            size_fact("reclaimable_total", "Reclaimable without a rewrite", reclaimable),
        ],
    )
    if rows:
        section.tables.append(
            Table(
                title="Leftover temporary packs",
                columns=["File", "Size", "Left behind"],
                rows=rows,
                note=(
                    "Incomplete packs from an interrupted fetch, push or repack. "
                    "No branch, tag or index refers to them."
                ),
            )
        )
    return section


def _history_section(
    reach: dict, scan: HistoryScan, top_paths: list, counts: dict, tree
) -> Section:
    unreachable = max(0, counts.get("count", 0) + counts.get("in-pack", 0) - scan.objects)
    section = Section(
        number="2.2",
        title="Real history",
        slot="history",
        facts=[
            count_fact("history_objects", "Objects reachable from a ref", scan.objects),
            count_fact("history_blobs", "File versions in history", scan.blobs),
            size_fact("history_content", "Their content, uncompressed", scan.blob_size),
            size_fact("history_packed", "What that occupies packed", scan.blob_disk),
            count_fact("unreachable_objects", "Objects not reachable from any ref", unreachable),
        ],
    )
    if top_paths:
        section.tables.append(
            Table(
                title="Largest paths across all versions",
                columns=["Path", "Versions", "Total", "Largest version"],
                rows=[
                    [
                        path,
                        human_count(stat.versions),
                        human_bytes(stat.size),
                        human_bytes(stat.largest),
                    ]
                    for path, stat in top_paths
                ],
                note=(
                    "Content identical at two paths is counted once, against one "
                    "of them."
                ),
            )
        )
    top_ext = sorted(scan.by_ext.items(), key=lambda kv: -kv[1].size)[:TOP_EXTENSIONS]
    if top_ext:
        section.tables.append(
            Table(
                title="Largest file types across all versions",
                columns=["Extension", "Versions", "Total"],
                rows=[
                    [ext or "(none)", human_count(stat.versions), human_bytes(stat.size)]
                    for ext, stat in top_ext
                ],
            )
        )
    return section
