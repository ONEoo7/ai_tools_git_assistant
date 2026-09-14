"""Refresh the starter files the Clone & Create tab writes into a repository.

    uv run python tools/update_starter_templates.py [--gitignore-ref SHA]
                                                    [--licenses-ref SHA]

Downloads, at pinned commits, into src/git_assistant/resources/starter_templates.json:

- the .gitignore templates the tab offers, from github/gitignore (CC0-1.0);
- the MIT and Apache 2.0 license texts from github/choosealicense.com -- the ones
  GitHub itself puts into a new repository.

The application never downloads them. It ships this file, so the tab works
offline and every build writes the same bytes. Moving a pin is a reviewed change
to that file, like any other.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from git_assistant import starter_files  # noqa: E402
from git_assistant.net import http_client  # noqa: E402

GITIGNORE_REPO = "github/gitignore"
GITIGNORE_REF = "356fd7baab4c05e092194a41f64dbd5afc8817e4"
LICENSES_REPO = "github/choosealicense.com"
LICENSES_REF = "58267f8f2c5c0099810849cfd7677f52ae0c0eb3"
#: The license id this project uses -> the file choosealicense.com keeps it in.
LICENSE_FILES = {"MIT": "mit.txt", "Apache-2.0": "apache-2.0.txt"}

OUTPUT = ROOT / "src" / "git_assistant" / "resources" / "starter_templates.json"


def _raw(client, repo: str, ref: str, path: str) -> str:
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"
    response = client.get(url)
    response.raise_for_status()
    return _lf(response.text)


def _lf(text: str) -> str:
    """LF only, one final newline: the bytes are the same on every checkout."""
    return text.replace("\r\n", "\n").replace("\r", "\n").strip("\n") + "\n"


def _without_front_matter(text: str) -> str:
    """choosealicense.com files open with YAML describing the license."""
    if text.startswith("---\n"):
        end = text.index("\n---\n", 4)
        text = text[end + len("\n---\n") :]
    return _lf(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gitignore-ref", default=GITIGNORE_REF)
    parser.add_argument("--licenses-ref", default=LICENSES_REF)
    args = parser.parse_args()

    names = sorted({name for name in starter_files.GITIGNORE_TEMPLATE.values() if name})
    with http_client(timeout=30, follow_redirects=True) as client:
        templates = {
            name: _raw(client, GITIGNORE_REPO, args.gitignore_ref, f"{name}.gitignore")
            for name in names
        }
        licenses = {
            kind: _without_front_matter(
                _raw(client, LICENSES_REPO, args.licenses_ref, f"_licenses/{file}")
            )
            for kind, file in LICENSE_FILES.items()
        }

    data = {
        "gitignore": {
            "source": f"https://github.com/{GITIGNORE_REPO}",
            "commit": args.gitignore_ref,
            "license": "CC0-1.0",
            "templates": templates,
        },
        "licenses": {
            "source": f"https://github.com/{LICENSES_REPO}",
            "commit": args.licenses_ref,
            "texts": licenses,
        },
    }
    with OUTPUT.open("w", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {OUTPUT.relative_to(ROOT)}: {len(templates)} templates, "
          f"{len(licenses)} licenses")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
