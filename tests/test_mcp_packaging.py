"""The server has to be in what ships, and the installer has to know about it.

Text assertions over the build files: the specs, the workflow and the NSIS
script all have to agree about what ships, and have not before.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ONEDIR_SPECS = ("git-assistant-onedir.spec",)
MCP_EXE = "GitAssistantMcp"


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("spec", ONEDIR_SPECS)
def test_every_installed_build_carries_the_mcp_executable(spec):
    text = _read(spec)
    assert f'name="{MCP_EXE}"' in text
    assert "console=True" in text, "stdio needs a console subsystem build"


@pytest.mark.parametrize("spec", ONEDIR_SPECS)
def test_the_mcp_executable_is_collected_beside_the_app(spec):
    """Built but not collected would leave it out of the payload entirely."""
    text = _read(spec)
    collect = text[text.index("COLLECT(") :]
    assert "mcp_exe" in collect


def test_the_portable_build_carries_it_too():
    text = _read("git-assistant.spec")
    assert f'name="{MCP_EXE}"' in text
    assert "console=True" in text
    # Its own analysis, without Qt: the server never imports it, so a portable
    # zip should not pay for it twice.
    assert '"PyQt6"' in text


def test_the_app_itself_is_still_windowed():
    for spec in (*ONEDIR_SPECS, "git-assistant.spec"):
        assert "console=False" in _read(spec), spec


def test_both_executables_are_signed():
    text = _read("tools/build.py")
    assert 'payload / "GitAssistantMcp.exe"' in text
    assert 'DIST / "GitAssistantMcp.exe"' in text


def test_the_installer_stops_a_running_server_before_replacing_it():
    """A client keeps it alive for its whole session, holding _internal open."""
    text = _read("installer/git-assistant.nsi")
    macro = text[text.index("!macro StopRunningApp") : text.index("!macroend")]
    assert "GitAssistantMcp.exe" in macro


def test_the_uninstaller_removes_it():
    text = _read("installer/git-assistant.nsi")
    assert 'Delete "$INSTDIR\\GitAssistantMcp.exe"' in text


def test_released_builds_come_from_the_spec():
    """A command line can only make one executable; this build needs two."""
    text = _read(".github/workflows/release.yml")
    assert "pyinstaller --noconfirm git-assistant-onedir.spec" in text
    assert "--windowed" not in text


def test_the_workflow_points_at_what_the_spec_produces():
    text = _read(".github/workflows/release.yml")
    assert "dist\\GitAssistant\\" not in text.replace("dist\\GitAssistant-noupdate", "")
    assert 'dir = "dist\\git-assistant"' not in text


def test_the_source_install_exposes_a_console_script():
    assert 'git-assistant-mcp = "git_assistant.mcp.server:main"' in _read("pyproject.toml")


# ---- reading a rule table ------------------------------------------------------
@pytest.mark.parametrize("spec", (*ONEDIR_SPECS, "git-assistant.spec"))
def test_every_build_of_the_app_can_read_a_spreadsheet(spec):
    """openpyxl is imported inside a function, which is what PyInstaller misses."""
    text = _read(spec)
    app = text[: text.index("mcp_a = Analysis")] if "mcp_a = Analysis" in text else text
    assert '"openpyxl"' in app


def test_the_mcp_server_does_not_pay_for_the_spreadsheet_reader():
    text = _read("git-assistant.spec")
    mcp = text[text.index("mcp_a = Analysis") :]
    excludes = mcp[mcp.index("excludes=") : mcp.index("noarchive")]
    assert '"openpyxl"' in excludes


def test_the_dependency_is_declared_where_it_is_installed_from():
    assert "openpyxl" in _read("pyproject.toml")
    assert "openpyxl" in _read("uv.lock")


# ---- the entry point ----------------------------------------------------------
def test_the_module_runs_the_server_without_a_display():
    """The whole point of the argv dispatch: no Qt, no tray, no single instance."""
    done = subprocess.run(
        [sys.executable, "-m", "git_assistant", "--mcp"],
        input=b"",  # EOF straight away
        capture_output=True,
        timeout=60,
        cwd=ROOT,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert done.returncode == 0
    assert done.stdout == b"", "nothing but MCP messages may reach stdout"


def test_the_console_companion_refuses_to_open_a_tray_icon(tmp_path):
    """Both executables run the same script; only argv tells them apart."""
    from git_assistant.__main__ import _entry

    assert _entry(["GitAssistantMcp.exe"], "GitAssistantMcp.exe") == 2


@pytest.mark.parametrize("spec", (*ONEDIR_SPECS, "git-assistant.spec"))
def test_every_build_ships_the_review_rules(spec):
    """Read at runtime, so a build without them reviews against nothing."""
    text = _read(spec)
    app = text[: text.index("mcp_a = Analysis")] if "mcp_a = Analysis" in text else text
    assert "review_rules.json" in app
    assert '"git_assistant/resources"' in app


# ---- Langfuse tracing ------------------------------------------------------------
@pytest.mark.parametrize("spec", (*ONEDIR_SPECS, "git-assistant.spec"))
def test_every_build_can_send_a_trace(spec):
    """A hidden import is not enough: OTEL finds its exporter by entry point."""
    text = _read(spec)
    app = text[: text.index("mcp_a = Analysis")] if "mcp_a = Analysis" in text else text
    assert 'collect_all(_pkg)' in app
    assert '"langfuse"' in app and '"opentelemetry"' in app
    assert "_lf_hidden" in app and "_lf_datas" in app


def test_the_mcp_server_traces_too():
    """It generates commit messages; unlike openpyxl this cannot be left out."""
    text = _read("git-assistant.spec")
    mcp = text[text.index("mcp_a = Analysis") :]
    assert "_lf_hidden" in mcp and "_lf_datas" in mcp
    excludes = mcp[mcp.index("excludes=") : mcp.index("noarchive")]
    assert "langfuse" not in excludes and "opentelemetry" not in excludes


@pytest.mark.parametrize("spec", (*ONEDIR_SPECS, "git-assistant.spec"))
def test_a_checkout_without_langfuse_still_builds(spec):
    """Optional in the same sense dist_client is; the app degrades on its own."""
    assert 'find_spec("langfuse")' in _read(spec)


def test_the_dependency_is_declared_where_it_is_installed_from_too():
    assert "langfuse" in _read("pyproject.toml")
    assert "langfuse" in _read("uv.lock")
