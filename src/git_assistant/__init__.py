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
__version__ = "0.3.2"
