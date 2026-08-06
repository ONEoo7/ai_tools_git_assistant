"""Local-LLM git commit message assistant (system-tray app)."""

#: The one place the version is written.
#:
#: `pyproject.toml` derives from this line rather than declaring its own
#: number, and the release workflow checks the tag against it. That ordering
#: matters: a packaged build has no distribution metadata, so this literal is
#: what the running application reports — and the updater compares exactly
#: that against the published release. When the two numbers were written
#: separately they drifted, and a 0.2.0 build reported 0.1.0, which would have
#: made it offer itself as an update forever.
__version__ = "0.3.16"

#: Who wrote it and where it lives, shown at the foot of the settings window.
#:
#: Literals here for the same reason the version is: a packaged build carries no
#: distribution metadata, so nothing at runtime can read them out of
#: `pyproject.toml`. They are repeated in the Windows version resource
#: (`tools/win_version_info.py`) and the installer (`installer/*.nsi`), which
#: cannot import this module -- tests/test_about.py holds the three in step.
__author__ = "Stefan Ghitescu"
PROJECT_URL = "https://github.com/ONEoo7/ai_tools_git_assistant"

#: Everyone else who shaped this, and what they contributed. Shown under About.
#: ``(name, what they did)``, in the order they should be read -- not sorted, so
#: the list can be arranged by hand as it grows.
CONTRIBUTORS: tuple[tuple[str, str], ...] = (
    ("Stefan Dragomir", "Product feedback"),
)
