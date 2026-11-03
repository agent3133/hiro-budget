"""Classifier training in a worker thread (#55).

Training takes seconds (L-BFGS over ~0.5-0.9 M parameters); in the GUI thread it froze the
window after every import. The samples are read from the database in the GUI thread (a few
ms, plain columns), the maths and the saving run in a daemon thread, and ``finished``
arrives back in the GUI thread. A request while a training runs trains once more afterwards,
on the newer bookings. The model file is replaced atomically (``Classifier.save``), so a
Categorizer loading it meanwhile gets the old or the new model, never half of one.
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Callable

from PySide6.QtCore import QCoreApplication, QObject, Signal

from hiro_budget.app import App
from hiro_budget.services.categorization.pipeline import fit_and_save, training_samples

log = logging.getLogger(__name__)
_trainers: weakref.WeakSet[Trainer] = weakref.WeakSet()


class Trainer(QObject):
    """One per App: ``trainer(app)``."""

    started = Signal()
    # the Classifier or None (too few samples); error text ("" when fine)
    finished = Signal(object, str)
    _done = Signal(object, str)  # from the worker thread, queued to the GUI thread

    def __init__(self, app: App) -> None:
        super().__init__()
        self.app = app
        self._thread: threading.Thread | None = None
        self._again = False
        self._callbacks: list[Callable[[object, str], None]] = []
        self._done.connect(self._on_done)
        _trainers.add(self)

    @property
    def running(self) -> bool:
        return self._thread is not None

    def start(self, on_done: Callable[[object, str], None] | None = None) -> None:
        """Train on the current bookings; ``on_done(classifier or None, error text)`` afterwards."""
        if on_done is not None:
            self._callbacks.append(on_done)
        if self._thread is not None:
            self._again = True
            return
        with self.app.session() as s:
            samples = training_samples(s)
        settings = self.app.settings
        self._thread = threading.Thread(
            target=self._work, args=(samples, settings), name="train-classifier", daemon=True
        )
        self.started.emit()
        self._thread.start()

    def _work(self, samples, settings) -> None:
        try:
            clf, error = fit_and_save(samples, settings), ""
        except Exception as e:  # reported in the GUI thread
            log.exception("training the classifier failed")
            clf, error = None, str(e) or type(e).__name__
        self._done.emit(clf, error)

    def _on_done(self, clf, error: str) -> None:
        if self._thread is not None:
            self._thread.join()
        self._thread = None
        if self._again:
            self._again = False
            self.start()  # the callbacks wait for the newer model
            return
        callbacks, self._callbacks = self._callbacks, []
        self.finished.emit(clf, error)
        for callback in callbacks:
            callback(clf, error)

    def wait(self) -> None:
        """Until no training runs and ``finished`` was delivered (tests, closing)."""
        while self._thread is not None:
            self._thread.join()
            QCoreApplication.sendPostedEvents(self)


def wait_all() -> None:
    """Until every training has finished (tests)."""
    for t in list(_trainers):
        t.wait()


def trainer(app: App) -> Trainer:
    found = getattr(app, "_trainer", None)
    if found is None:
        found = Trainer(app)
        app._trainer = found  # type: ignore[attr-defined]
    return found
