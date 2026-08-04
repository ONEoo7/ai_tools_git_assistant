"""QThread workers so git + network calls never block the Qt event loop.

Three workers:
- ``GeneratorWorker``  : runs commit-message generation, emitting live progress.
- ``AgentWorker``      : runs a repository audit, which can take minutes.
- ``FunctionWorker``   : runs an arbitrary callable (e.g. listing models) off-thread.

Each is a QObject meant to be moved onto a QThread; see ``run_worker`` for the
standard wiring.
"""

from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from git_assistant.commit_generator import (
    CancelledError,
    CommitGenerator,
    GenerationResult,
)
from git_assistant.config import Settings
from git_assistant.llm import build_client
from git_assistant.llm_log import RecordingClient


# Threads still running, kept referenced so Python cannot collect them early.
# See run_worker for why this is not left to the caller.
_IN_FLIGHT: set = set()


class GeneratorWorker(QObject):
    progress = pyqtSignal(str)
    #: Emitted as each call to the model comes back, so a long map-reduce run
    #: can be watched rather than waited out. Carries an llm_log.LlmCall.
    call = pyqtSignal(object)
    finished = pyqtSignal(object)  # GenerationResult
    error = pyqtSignal(str)

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        try:
            # Whichever provider is selected. `build_client` refuses rather
            # than falling back: generating through LM Studio while the window
            # says "Claude" would put that model's output under the wrong name,
            # and the user would have no way to tell.
            client = build_client(self._settings)
            # Wrapped so every exchange can be inspected afterwards. The
            # recorder forwards each call unchanged, so what it shows is what
            # was sent -- see git_assistant.llm_log.
            recorder = RecordingClient(client, on_call=self.call.emit)
            self.calls = recorder.calls
            generator = CommitGenerator(self._settings, recorder)
            result: GenerationResult = generator.generate(
                progress=self.progress.emit,
                is_cancelled=lambda: self._cancelled,
            )
            result.calls = list(recorder.calls)
            self.finished.emit(result)
        except CancelledError:
            self.error.emit("Cancelled.")
        except Exception as exc:  # surface any failure to the UI
            self.error.emit(str(exc))


class AgentWorker(QObject):
    """Runs one repository audit off-thread.

    Separate from ``GeneratorWorker`` for one reason: an audit reports how far
    through it is, not just what it is doing, because reading every object in a
    large repository takes minutes and a spinner is not an answer.
    """

    progress = pyqtSignal(str)
    progressPct = pyqtSignal(int)  # noqa: N815 - Qt signal naming
    finished = pyqtSignal(object)  # agents.Report
    error = pyqtSignal(str)

    def __init__(
        self, settings: Settings, agent_id: str, repo: str, *, fast: bool, narrate: bool
    ) -> None:
        super().__init__()
        self._settings = settings
        self._agent_id = agent_id
        self._repo = repo
        self._fast = fast
        self._narrate = narrate
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        # Imported here rather than at module scope: this pulls in the whole
        # agents package, and the tray's quick action must not pay for it.
        from git_assistant import agents

        try:
            report = agents.run(
                self._agent_id,
                self._settings,
                repo=self._repo,
                progress=self.progress.emit,
                progress_pct=self.progressPct.emit,
                is_cancelled=lambda: self._cancelled,
                fast=self._fast,
                narrate=self._narrate,
            )
            self.finished.emit(report)
        except agents.CancelledError:
            self.error.emit("Cancelled.")
        except Exception as exc:  # surface any failure to the UI
            self.error.emit(str(exc))


class SetupWorker(QObject):
    """Runs the LM Studio setup: an install and a multi-gigabyte download."""

    progress = pyqtSignal(str)
    finished = pyqtSignal(object)  # lmstudio_setup.SetupOutcome
    error = pyqtSignal(str)

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self._settings = settings
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        from git_assistant import lmstudio_setup

        ctx = lmstudio_setup.SetupContext(
            progress=self.progress.emit,
            is_cancelled=lambda: self._cancelled,
        )
        try:
            self.finished.emit(lmstudio_setup.run(self._settings, ctx))
        except lmstudio_setup.Cancelled:
            self.error.emit("Cancelled.")
        except Exception as exc:  # surface any failure to the UI
            self.error.emit(str(exc))


class FunctionWorker(QObject):
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, fn: Callable[[], object]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.finished.emit(self._fn())
        except Exception as exc:
            self.error.emit(str(exc))


def run_worker(worker: QObject) -> QThread:
    """Move ``worker`` onto a fresh QThread, start it, and auto-clean up.

    The worker must expose a ``run`` slot and a ``finished``/``error`` signal.
    Returns the QThread, though the caller need not hold on to it.
    """
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)

    def _cleanup(*_):
        thread.quit()

    worker.finished.connect(_cleanup)
    worker.error.connect(_cleanup)
    thread.finished.connect(thread.deleteLater)
    thread.finished.connect(worker.deleteLater)
    # Hold both alive until the thread actually finishes. Relying on the caller
    # to keep a reference means a widget that is closed (or garbage collected)
    # mid-flight drops the last reference to a *running* QThread, and Qt aborts
    # the process with "QThread: Destroyed while thread is still running".
    _IN_FLIGHT.add((thread, worker))
    thread.finished.connect(lambda: _IN_FLIGHT.discard((thread, worker)))
    thread.start()
    return thread
