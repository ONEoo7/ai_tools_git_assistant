"""Langfuse tracing: off by default, and unable to break a run when it is on.

Nothing here reaches the network. The SDK is replaced by a double that records
what it was asked to do, which is the only thing worth asserting: a real server
would tell us about Langfuse, not about this code.

The rule these tests exist to hold: **a generation must come back whether or not
the trace did.** Every failure mode of the tracer is therefore a test that the
chat still returned.
"""

import os
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from git_assistant import llm, usage
from git_assistant import tracing
from git_assistant.config import Settings
from git_assistant.llm_log import RecordingClient
from git_assistant.tracing import client as client_mod
from git_assistant.tracing import settings as trace_settings
from git_assistant.tracing import tracer
from git_assistant.tracing.settings import TraceSettings

SECRET = "sk-lf-secret-value"
ROOT = Path(__file__).resolve().parents[1]


# ---- doubles ------------------------------------------------------------------
class FakeSpan:
    def __init__(self, name, **kwargs):
        self.name = name
        self.kwargs = dict(kwargs)
        self.children: list["FakeSpan"] = []
        self.updates: list[dict] = []
        self.ended = False

    def start_observation(self, *, name, **kwargs):
        child = FakeSpan(name, **kwargs)
        self.children.append(child)
        return child

    def update(self, **kwargs):
        self.updates.append(dict(kwargs))

    def end(self):
        self.ended = True

    def field(self, key):
        """What the span was given, at creation or in a later update."""
        for patch in reversed(self.updates):
            if key in patch:
                return patch[key]
        return self.kwargs.get(key)


class FakeLangfuse:
    """Stands in for the SDK client. Records; never sends."""

    instances: list["FakeLangfuse"] = []

    def __init__(self, **kwargs):
        self.kwargs = dict(kwargs)
        self.roots: list[FakeSpan] = []
        self.shut_down = False
        self.auth = True
        FakeLangfuse.instances.append(self)

    def start_observation(self, *, name, **kwargs):
        root = FakeSpan(name, **kwargs)
        self.roots.append(root)
        return root

    def auth_check(self):
        return self.auth

    def flush(self):
        pass

    def shutdown(self):
        self.shut_down = True


class Chatter:
    """The provider client underneath, with the four methods and nothing else."""

    def __init__(self, reply="ok", fail=None):
        self.reply = reply
        self.fail = fail
        self.seen: list[dict] = []

    def chat(self, model, system, user, max_tokens, temperature=0.2):
        self.seen.append(
            {"model": model, "system": system, "user": user, "max_tokens": max_tokens}
        )
        if self.fail is not None:
            raise self.fail
        return self.reply

    def list_models(self):
        return ["a-model"]

    def context_length_for(self, model_id):
        return 4096

    def ping(self):
        return ["a-model"]


# ---- fixtures -----------------------------------------------------------------
@pytest.fixture(autouse=True)
def keys(monkeypatch):
    """A credential store in memory. Both keys live there; neither is a setting."""
    from git_assistant import credentials

    kept = {
        trace_settings.CREDENTIAL_KEY: SECRET,
        trace_settings.PUBLIC_CREDENTIAL_KEY: "pk-lf-public",
    }
    FakeLangfuse.instances = []
    # Patched at the store, not at the reader above it, so `_stored` -- which
    # is what has to swallow a store that refuses -- is the real one.
    monkeypatch.setattr(credentials, "get_secret", lambda key: kept.get(key))
    tracer.shutdown()
    yield kept
    tracer.shutdown()


@pytest.fixture(autouse=True)
def usage_store(tmp_path, monkeypatch):
    """The token counts this reads about are recorded; not into the real file.

    Patched where it is imported, as tests/test_usage.py does.
    """
    monkeypatch.setattr(usage, "user_config_dir", lambda *a, **k: str(tmp_path))
    usage.forget()
    return tmp_path


@pytest.fixture
def sdk(monkeypatch):
    """The SDK, replaced by the double."""

    class Module:
        Langfuse = FakeLangfuse

    monkeypatch.setattr(tracer, "_sdk", lambda: Module)
    return Module


@pytest.fixture
def configured():
    s = Settings()
    s.save = lambda: None
    s.langfuse_enabled = True
    s.langfuse_host = "https://langfuse.example"
    s.active_repo = "D:/work/acme-corp/widget"
    return s


def _traced(settings, feature="Code review", reply="ok", fail=None):
    inner = Chatter(reply=reply, fail=fail)
    return inner, tracing.wrap(inner, settings, feature)


@contextmanager
def _null_scope():
    yield


# ---- what counts as configured -----------------------------------------------
def test_nothing_is_configured_by_default():
    assert not trace_settings.from_settings(Settings()).configured()


def test_neither_key_is_a_setting(configured):
    """Both halves are in the credential store; the settings file has neither."""
    stored = str(configured.to_dict())
    assert SECRET not in stored and "pk-lf-public" not in stored
    assert trace_settings.from_settings(configured).public_key == "pk-lf-public"


def test_the_missing_field_is_named_one_at_a_time(configured, keys):
    keys[trace_settings.PUBLIC_CREDENTIAL_KEY] = ""
    assert "public key" in trace_settings.from_settings(configured).missing()


def test_a_configuration_that_is_ready_says_nothing_is_missing(configured):
    assert trace_settings.from_settings(configured).missing() == ""


def test_the_host_is_taken_as_typed_without_its_trailing_slash(configured):
    configured.langfuse_host = "https://langfuse.example/"
    assert trace_settings.from_settings(configured).host == "https://langfuse.example"


def test_a_credential_store_that_refuses_reads_as_no_key(monkeypatch, configured):
    """Not as a crash: an unreadable store means tracing does not start."""
    from git_assistant import credentials

    monkeypatch.setattr(
        credentials,
        "get_secret",
        lambda key: (_ for _ in ()).throw(credentials.CredentialError("locked")),
    )

    assert not trace_settings.from_settings(configured).configured()


# ---- off means off -------------------------------------------------------------
def test_an_unconfigured_build_gets_its_own_client_back():
    inner, wrapped = _traced(Settings())
    assert wrapped is inner


def test_nothing_is_built_when_tracing_is_off(sdk):
    _traced(Settings())[1].chat("m", "s", "u", 10)
    assert FakeLangfuse.instances == []


def test_a_build_without_the_sdk_still_generates(monkeypatch, configured):
    monkeypatch.setattr(tracer, "_sdk", lambda: None)
    inner, wrapped = _traced(configured)

    assert wrapped.chat("m", "s", "u", 10) == "ok"
    assert "does not include" in tracer.status()


# ---- one client is one trace ------------------------------------------------------
def test_a_run_is_one_trace_named_after_what_it_is_for(sdk, configured):
    _, wrapped = _traced(configured)

    wrapped.chat("m", "s", "u", 10)
    wrapped.chat("m", "s2", "u2", 10)

    root = FakeLangfuse.instances[0].roots[0]
    assert len(FakeLangfuse.instances[0].roots) == 1, "one run, one trace"
    assert root.name == "Code review"
    assert len(root.children) == 2


def test_a_client_that_never_chats_leaves_no_empty_trace(sdk, configured):
    """`build_client` is also how the Connection tab tests a connection."""
    _, wrapped = _traced(configured)
    wrapped.ping()
    assert FakeLangfuse.instances == [] or not FakeLangfuse.instances[0].roots


def test_the_repository_s_name_travels_and_its_path_does_not(sdk, configured):
    _, wrapped = _traced(configured)
    wrapped.chat("m", "s", "u", 10)

    metadata = FakeLangfuse.instances[0].roots[0].kwargs["metadata"]
    assert metadata["repository"] == "widget"
    assert "acme-corp" not in repr(metadata)


# ---- who and what produced the trace ----------------------------------------------
def test_the_account_running_the_application_is_the_user(sdk, configured, monkeypatch):
    import getpass

    monkeypatch.setattr(getpass, "getuser", lambda: "stefan.ghitescu")
    assert tracing.who() == "stefan.ghitescu"


def test_a_platform_that_will_not_say_leaves_the_user_empty(monkeypatch):
    """A trace attributed to "unknown" looks like a real account."""
    import getpass

    monkeypatch.setattr(
        getpass, "getuser", lambda: (_ for _ in ()).throw(OSError("no login"))
    )
    assert tracing.who() == ""


def test_the_user_and_the_tag_go_on_every_span(sdk, configured, monkeypatch):
    """Not only the root: OTEL's context does not cross the pool a review uses,
    and Langfuse filters on these only where they were actually put."""
    import getpass

    import langfuse

    monkeypatch.setattr(getpass, "getuser", lambda: "stefan")
    asked = []
    monkeypatch.setattr(
        langfuse,
        "propagate_attributes",
        lambda **kw: (asked.append(kw), _null_scope())[1],
    )
    _, wrapped = _traced(configured)

    wrapped.chat("m", "s", "u", 10)

    assert len(asked) == 2, "once for the run's span, once for the generation"
    for scope in asked:
        assert scope["user_id"] == "stefan"
        assert scope["tags"] == ["git-assistant"]
        assert scope["metadata"]["feature"] == "Code review"
        assert scope["metadata"]["repository"] == "widget"


def test_no_user_is_no_user_rather_than_a_blank_one(sdk, configured, monkeypatch):
    import getpass

    import langfuse

    monkeypatch.setattr(getpass, "getuser", lambda: "")
    asked = []
    monkeypatch.setattr(
        langfuse,
        "propagate_attributes",
        lambda **kw: (asked.append(kw), _null_scope())[1],
    )
    _traced(configured)[1].chat("m", "s", "u", 10)

    assert asked[0]["user_id"] is None


def test_a_scope_that_cannot_be_made_does_not_stop_the_chat(
    sdk, configured, monkeypatch
):
    import langfuse

    monkeypatch.setattr(langfuse, "propagate_attributes", lambda **kw: 1 / 0)
    assert _traced(configured)[1].chat("m", "s", "u", 10) == "ok"


def test_the_service_names_itself(sdk, configured, monkeypatch):
    monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
    tracer.client(trace_settings.from_settings(configured))
    assert os.environ["OTEL_SERVICE_NAME"] == tracer.SERVICE_NAME


def test_a_service_name_someone_else_set_is_left_alone(sdk, configured, monkeypatch):
    """Under a collector that already names the service, they meant it."""
    monkeypatch.setenv("OTEL_SERVICE_NAME", "their-collector-name")
    tracer.client(trace_settings.from_settings(configured))
    assert os.environ["OTEL_SERVICE_NAME"] == "their-collector-name"


def test_closing_the_run_ends_its_span(sdk, configured):
    _, wrapped = _traced(configured)
    wrapped.chat("m", "s", "u", 10)

    tracing.close(wrapped)

    assert FakeLangfuse.instances[0].roots[0].ended


def test_closing_something_with_nothing_to_close_is_not_an_error():
    tracing.close(Chatter())
    tracing.close(None)


# ---- what a generation carries -------------------------------------------------
def test_a_generation_carries_the_model_the_prompt_and_the_reply(sdk, configured):
    _, wrapped = _traced(configured, reply="the answer")
    wrapped.chat("qwen3.5-4b", "be brief", "review this", 512)

    span = FakeLangfuse.instances[0].roots[0].children[0]
    assert span.kwargs["as_type"] == "generation"
    assert span.kwargs["model"] == "qwen3.5-4b"
    assert span.kwargs["input"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "review this"},
    ]
    assert span.field("output") == "the answer"
    assert span.ended


def test_withholding_prompts_omits_them_rather_than_blanking_them(sdk, configured):
    """An empty prompt in Langfuse reads as a call made with an empty prompt."""
    configured.langfuse_send_prompts = False
    _, wrapped = _traced(configured)

    wrapped.chat("m", "be brief", "review this", 512)

    span = FakeLangfuse.instances[0].roots[0].children[0]
    assert span.kwargs["input"] is None
    assert span.field("output") is None
    assert span.kwargs["model"] == "m", "the rest of the trace survives"


def test_a_failed_call_is_marked_and_still_raises(sdk, configured):
    _, wrapped = _traced(configured, fail=RuntimeError("connection reset"))

    with pytest.raises(RuntimeError):
        wrapped.chat("m", "s", "u", 10)

    span = FakeLangfuse.instances[0].roots[0].children[0]
    assert span.field("level") == "ERROR"
    assert "connection reset" in span.field("status_message")
    assert span.ended


def test_the_phase_names_the_span(sdk, configured):
    """A trace of fifteen spans called "completion" is a list, not a story."""
    _, wrapped = _traced(configured, feature="Commit message")
    recorder = RecordingClient(wrapped)

    recorder.phase = "summarizing a chunk"
    recorder.chat("m", "s", "u", 10)

    assert FakeLangfuse.instances[0].roots[0].children[0].name == "summarizing a chunk"


# ---- token counts --------------------------------------------------------------
def test_the_provider_s_own_numbers_reach_the_span(sdk, configured, monkeypatch):
    inner = Chatter()
    inner.chat = lambda **kw: (
        usage.record("lmstudio", "m", 120, 34, feature="Code review") and "ok"
    )
    wrapped = tracing.wrap(inner, configured, "Code review")

    wrapped.chat(model="m", system="s", user="u", max_tokens=10)

    span = FakeLangfuse.instances[0].roots[0].children[0]
    assert span.field("usage_details") == {"input": 120, "output": 34}


def test_an_estimate_is_left_out_rather_than_sent_as_measured(
    sdk, configured, monkeypatch
):
    inner = Chatter()
    inner.chat = lambda **kw: (
        usage.record("proxy", "m", 120, 34, estimated=True) and "ok"
    )
    wrapped = tracing.wrap(inner, configured, "Code review")

    wrapped.chat(model="m", system="s", user="u", max_tokens=10)

    assert FakeLangfuse.instances[0].roots[0].children[0].field("usage_details") is None


def test_an_earlier_call_s_numbers_are_not_filed_against_this_one(
    sdk, configured, monkeypatch
):
    """The same thread makes many calls; a stale count is worse than none."""
    usage.record("lmstudio", "m", 999, 999, feature="Code review")
    _, wrapped = _traced(configured)  # a client that records nothing

    wrapped.chat("m", "s", "u", 10)

    assert FakeLangfuse.instances[0].roots[0].children[0].field("usage_details") is None


# ---- nothing here may cost a generation ------------------------------------------
def test_a_tracer_that_cannot_start_does_not_stop_the_chat(sdk, configured, monkeypatch):
    monkeypatch.setattr(
        FakeLangfuse, "start_observation", lambda *a, **k: 1 / 0, raising=False
    )
    _, wrapped = _traced(configured)
    assert wrapped.chat("m", "s", "u", 10) == "ok"


def test_a_client_that_cannot_be_built_does_not_stop_the_chat(
    sdk, configured, monkeypatch
):
    class Exploding:
        Langfuse = staticmethod(lambda **kw: 1 / 0)

    monkeypatch.setattr(tracer, "_sdk", lambda: Exploding)
    _, wrapped = _traced(configured)

    assert wrapped.chat("m", "s", "u", 10) == "ok"
    assert "Could not start tracing" in tracer.status()


def test_a_span_that_cannot_be_ended_does_not_stop_the_chat(sdk, configured, monkeypatch):
    _, wrapped = _traced(configured)
    monkeypatch.setattr(FakeSpan, "end", lambda self: 1 / 0)
    assert wrapped.chat("m", "s", "u", 10) == "ok"


def test_everything_else_is_the_client_underneath(sdk, configured):
    inner, wrapped = _traced(configured)
    assert wrapped.list_models() == inner.list_models()
    assert wrapped.context_length_for("a-model") == 4096
    assert wrapped.ping() == ["a-model"]


def test_the_calls_reach_the_provider_unchanged(sdk, configured):
    inner, wrapped = _traced(configured)
    wrapped.chat("m", "system text", "user text", 512, temperature=0.7)
    assert inner.seen == [
        {"model": "m", "system": "system text", "user": "user text", "max_tokens": 512}
    ]


# ---- several threads, one trace ---------------------------------------------------
def test_a_fan_out_files_every_call_under_the_same_trace(sdk, configured):
    """A review runs `parallel_calls` at once; OTEL's context does not cross a pool."""
    _, wrapped = _traced(configured)
    threads = [
        threading.Thread(target=wrapped.chat, args=("m", "s", f"file {i}", 10))
        for i in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(FakeLangfuse.instances[0].roots) == 1
    assert len(FakeLangfuse.instances[0].roots[0].children) == 6


# ---- the secret ----------------------------------------------------------------
def test_the_secret_never_appears_in_a_status_line(sdk, configured, monkeypatch):
    class Exploding:
        Langfuse = staticmethod(
            lambda **kw: (_ for _ in ()).throw(RuntimeError(f"bad auth {SECRET}"))
        )

    monkeypatch.setattr(tracer, "_sdk", lambda: Exploding)
    _traced(configured)[1].chat("m", "s", "u", 10)

    assert SECRET not in tracer.status()
    assert tracer.REDACTED in tracer.status()


def test_the_secret_is_not_a_setting(configured):
    assert SECRET not in str(configured.to_dict())
    assert "langfuse_secret" not in str(set(configured.to_dict()))


# ---- the configuration can change while the application runs ----------------------
def test_pointing_it_somewhere_else_replaces_the_client(sdk, configured):
    tracer.client(trace_settings.from_settings(configured))
    first = FakeLangfuse.instances[0]

    configured.langfuse_host = "https://elsewhere.example"
    tracer.client(trace_settings.from_settings(configured))

    assert first.shut_down
    assert len(FakeLangfuse.instances) == 2


def test_the_same_configuration_keeps_the_same_client(sdk, configured):
    config = trace_settings.from_settings(configured)
    assert tracer.client(config) is tracer.client(config)


def test_turning_it_off_shuts_the_client_down(sdk, configured):
    tracer.client(trace_settings.from_settings(configured))
    configured.langfuse_enabled = False

    assert tracer.client(trace_settings.from_settings(configured)) is None
    assert FakeLangfuse.instances[0].shut_down


# ---- the test button --------------------------------------------------------------
def test_a_round_trip_that_works_says_where_it_reached(sdk, configured):
    ok, message = tracer.check(trace_settings.from_settings(configured))
    assert ok and "langfuse.example" in message


def test_refused_credentials_do_not_guess_which_one_is_wrong(sdk, configured):
    tracer.client(trace_settings.from_settings(configured))
    FakeLangfuse.instances[0].auth = False

    ok, message = tracer.check(trace_settings.from_settings(configured))

    assert not ok and "refused these credentials" in message


def test_an_unreachable_host_reports_the_reason_without_the_key(sdk, configured):
    tracer.client(trace_settings.from_settings(configured))
    FakeLangfuse.instances[0].auth_check = lambda: (_ for _ in ()).throw(
        OSError(f"connect failed for {SECRET}")
    )

    ok, message = tracer.check(trace_settings.from_settings(configured))

    assert not ok and "Could not reach" in message and SECRET not in message


def test_testing_before_it_is_configured_says_what_is_missing():
    ok, message = tracer.check(TraceSettings())
    assert not ok and "Not sending traces" in message


# ---- the hook -----------------------------------------------------------------------
def test_build_client_returns_the_bare_client_when_tracing_is_off(monkeypatch):
    s = Settings()
    s.save = lambda: None
    made = Chatter()
    monkeypatch.setattr(llm, "_provider_client", lambda *a, **k: made)

    assert llm.build_client(s, feature="Commit message") is made


def test_the_sdk_is_not_imported_until_something_is_traced():
    """It is the heaviest import in the tree, for a subsystem most never enable."""
    source = (ROOT / "src" / "git_assistant" / "tracing" / "__init__.py").read_text(
        encoding="utf-8"
    )
    assert "import langfuse" not in source


def test_the_application_runs_without_the_package_installed():
    """Packaged builds are assembled by three specs; one of them may miss it.

    A subprocess, because the import has already happened in this one, and
    "works in the checkout, ImportError in the installer" is the exact failure
    this test exists to catch.
    """
    program = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'langfuse' or name.startswith('langfuse.'):\n"
        "            raise ImportError('langfuse is not in this build')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "from git_assistant import tracing\n"
        "from git_assistant.config import Settings\n"
        "s = Settings()\n"
        "s.langfuse_enabled = True\n"
        "s.langfuse_host = 'https://x'\n"
        "assert tracing.available() is False\n"
        "print('ok')\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        timeout=120,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert done.returncode == 0, done.stderr.decode()
    assert done.stdout.strip().endswith(b"ok")


def test_build_client_traces_when_it_is_configured(sdk, configured, monkeypatch):
    monkeypatch.setattr(llm, "_provider_client", lambda *a, **k: Chatter())

    built = llm.build_client(configured, feature="Commit message")

    assert isinstance(built, client_mod.TracingClient)
    built.chat("m", "s", "u", 10)
    assert FakeLangfuse.instances[0].roots[0].name == "Commit message"
