"""Everything the configuration checks need, read once.

Fifteen checks asking git fifteen questions would be fifteen process launches on
Windows, where launching a process is the expensive part. They ask together,
here, and each check then works on data in memory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from git_assistant import git_ops
from git_assistant.agents.base import AgentContext

#: Attributes worth knowing per path. `filter` identifies LFS, `text`/`eol`
#: decide line endings, `diff`/`merge` say whether git treats it as text.
ATTRS = ("filter", "text", "eol", "diff", "merge")

_EOL_RE = re.compile(r"i/(\S+)\s+w/(\S+)\s+attr/(.*)")


@dataclass
class RepoProbe:
    """One read of a repository's configuration, index and attributes."""

    repo: str
    git_dir: str = ""
    #: key -> [(scope, value)], scope being system | global | local | worktree |
    #: command. A key set at two scopes appears twice, which is the point.
    config: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    tracked: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    #: path -> (index eol, worktree eol, attributes) from `ls-files --eol`
    eol: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    #: path -> {attribute: value}, values as `check-attr` reports them
    attrs: dict[str, dict[str, str]] = field(default_factory=dict)
    sizes: dict[str, int] = field(default_factory=dict)  # blob size in the index
    counts: dict[str, int] = field(default_factory=dict)  # count-objects -v
    attributes_files: dict[str, str] = field(default_factory=dict)  # path -> text
    lfs_version: str = ""

    # ---- config helpers ----------------------------------------------------
    def values(self, key: str) -> list[tuple[str, str]]:
        return self.config.get(key.lower(), [])

    def value(self, key: str) -> str:
        """The value git would use: the narrowest scope wins."""
        order = {"command": 0, "worktree": 1, "local": 2, "global": 3, "system": 4}
        found = sorted(self.values(key), key=lambda sv: order.get(sv[0], 9))
        return found[0][1] if found else ""

    def scopes(self, key: str) -> list[str]:
        return [scope for scope, _v in self.values(key)]

    def matching(self, pattern: str) -> list[tuple[str, str, str]]:
        """``(key, scope, value)`` for every key matching a regex."""
        rx = re.compile(pattern)
        return [
            (key, scope, value)
            for key, entries in self.config.items()
            if rx.search(key)
            for scope, value in entries
        ]

    def attr(self, path: str, name: str) -> str:
        return self.attrs.get(path, {}).get(name, "unspecified")

    def has_lfs_rules(self) -> bool:
        return any(a.get("filter") == "lfs" for a in self.attrs.values())

    def lfs_paths(self) -> list[str]:
        return [p for p, a in self.attrs.items() if a.get("filter") == "lfs"]


def collect(ctx: AgentContext) -> RepoProbe:
    repo = ctx.repo
    probe = RepoProbe(repo=repo)
    probe.git_dir = git_ops._run(repo, ["rev-parse", "--absolute-git-dir"]).stdout.strip()
    if not probe.git_dir:
        raise RuntimeError(f"{repo}\nis not a readable git repository.")

    ctx.say("Reading git configuration...")
    probe.config = _config(repo)
    lfs = git_ops._run_global(["lfs", "version"])
    probe.lfs_version = lfs.stdout.strip() if lfs.ok else ""

    ctx.say("Reading the index...")
    probe.tracked = _zsplit(git_ops._run(repo, ["ls-files", "-z"]).stdout)
    probe.ignored = _zsplit(
        git_ops._run(repo, ["ls-files", "-i", "-c", "--exclude-standard", "-z"]).stdout
    )
    probe.eol = _eol(repo)
    probe.sizes = _sizes(repo)
    probe.counts = _counts(repo)

    ctx.check_cancel()
    ctx.say("Reading .gitattributes...")
    probe.attrs = _attrs(repo, probe.tracked)
    for path in probe.tracked:
        if path.rsplit("/", 1)[-1] == ".gitattributes":
            shown = git_ops._run(repo, ["show", f":{path}"])
            if shown.ok:
                probe.attributes_files[path] = shown.stdout
    return probe


def _zsplit(text: str) -> list[str]:
    return [part for part in text.split("\0") if part]


def _config(repo: str) -> dict[str, list[tuple[str, str]]]:
    """Parse ``config --list --show-scope --show-origin -z``.

    Records come in threes: scope, origin, then ``key\\nvalue`` (a key with no
    value has no newline at all).
    """
    res = git_ops._run(repo, ["config", "--list", "--show-scope", "--show-origin", "-z"])
    fields = res.stdout.split("\0")
    out: dict[str, list[tuple[str, str]]] = {}
    for i in range(0, len(fields) - 2, 3):
        scope, _origin, entry = fields[i], fields[i + 1], fields[i + 2]
        if not entry:
            continue
        key, _, value = entry.partition("\n")
        out.setdefault(key.strip().lower(), []).append((scope.strip(), value))
    return out


def _eol(repo: str) -> dict[str, tuple[str, str, str]]:
    res = git_ops._run(repo, ["ls-files", "--eol", "-z"])
    out: dict[str, tuple[str, str, str]] = {}
    for record in _zsplit(res.stdout):
        info, _, path = record.rpartition("\t")
        match = _EOL_RE.match(info.strip())
        if match and path:
            out[path] = (match.group(1), match.group(2), match.group(3).strip())
    return out


def _sizes(repo: str) -> dict[str, int]:
    """Blob size of every tracked path, from the index."""
    listed = git_ops._run(repo, ["ls-files", "-s", "-z"])
    by_sha: dict[str, list[str]] = {}
    for record in _zsplit(listed.stdout):
        meta, _, path = record.partition("\t")
        parts = meta.split()
        if len(parts) >= 2 and path:
            by_sha.setdefault(parts[1], []).append(path)
    if not by_sha:
        return {}
    checked = git_ops._run(
        repo, ["cat-file", "--batch-check"], stdin="\n".join(by_sha) + "\n"
    )
    out: dict[str, int] = {}
    for line in checked.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[1] != "blob":
            continue
        try:
            size = int(parts[2])
        except ValueError:
            continue
        for path in by_sha.get(parts[0], []):
            out[path] = size
    return out


def _attrs(repo: str, tracked: list[str]) -> dict[str, dict[str, str]]:
    if not tracked:
        return {}
    res = git_ops._run(
        repo,
        ["check-attr", "-z", "--stdin", *ATTRS],
        stdin="\0".join(tracked) + "\0",
    )
    fields = res.stdout.split("\0")
    out: dict[str, dict[str, str]] = {}
    for i in range(0, len(fields) - 2, 3):
        path, name, value = fields[i], fields[i + 1], fields[i + 2]
        if path:
            out.setdefault(path, {})[name] = value
    return out


def _counts(repo: str) -> dict[str, int]:
    res = git_ops._run(repo, ["count-objects", "-v"])
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


def working_tree_has(repo: str, name: str) -> bool:
    return (Path(repo) / name).exists()
