"""QThread workers so git + network calls never block the Qt event loop.

Two workers:
- ``GeneratorWorker``  : runs commit-message generation, emitting live progress.
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
from git_assistant.lmstudio_client import LMStudioClient


class GeneratorWorker(QObject):
    progress = pyqtSignal(str)
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
            client = LMStudioClient(self._settings.base_url)
            generator = CommitGenerator(self._settings, client)
            result: GenerationResult = generator.generate(
                progress=self.progress.emit,
                is_cancelled=lambda: self._cancelled,
            )
            self.finished.emit(result)
        except CancelledError:
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
    Returns the QThread (keep a reference alive until it finishes).
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
    thread.start()
    return thread
