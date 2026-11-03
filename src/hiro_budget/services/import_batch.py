"""Import a batch of statement files: the one workflow behind the import dialog and the
``import`` command (#76). No Qt; the callers only ask (overlap, failed backup) and format.

Per file: preview → skip it if it was already imported or brings nothing new
(``ImportPreview.can_commit``) → on an overlap ask (an "apply to all" answer is kept for
the rest of the batch) → preview **again** in a new session (an earlier file may have
changed the picture) → commit without retraining → tally. A file that fails does not stop
the others. Before the batch a backup (``backup.before_import``), after it the caller
retrains once if ``imported_any``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session

from hiro_budget.config import Settings
from hiro_budget.services import import_service
from hiro_budget.services.import_service import ImportPreview, ImportResult, OverlapMode
from hiro_budget.services.linked_payments import LinkResult

log = logging.getLogger(__name__)

SessionScope = Callable[[], AbstractContextManager[Session]]  # App.session
# how to go on with an overlapping file: (mode or None = cancel this file, apply to all)
OverlapDecision = Callable[[ImportPreview], tuple[OverlapMode | None, bool]]

Status = Literal["imported", "already_imported", "nothing_new", "cancelled", "failed"]


@dataclass
class FileOutcome:
    path: Path
    status: Status
    preview: ImportPreview | None = None  # the last one (None if it could not be read)
    result: ImportResult | None = None  # status "imported"
    error: str = ""  # status "failed"
    mode: OverlapMode | None = None  # how an overlap was handled


@dataclass
class BatchResult:
    files: list[FileOutcome] = field(default_factory=list)
    backup: Path | None = None  # made before the import
    backup_error: str = ""  # the backup failed (the import went on or was stopped)
    stopped: bool = False  # nothing imported: the caller declined after a failed backup
    added: int = 0
    duplicates: int = 0
    replaced: int = 0  # stored bookings removed by an overwrite
    matched: int = 0  # manual entries replaced by imported bookings
    valuations: int = 0  # securities / balance statements
    linked: int = 0  # payments linked to their bank booking
    linking: LinkResult | None = None  # waiting payments after the last import

    def count(self, status: Status) -> int:
        return sum(1 for f in self.files if f.status == status)

    @property
    def imported_any(self) -> bool:
        return self.count("imported") > 0

    @property
    def errors(self) -> list[str]:
        return [f"{f.path.name}: {f.error}" for f in self.files if f.status == "failed"]


def backup_first(
    settings: Settings, result: BatchResult, go_on: Callable[[Exception], bool]
) -> bool:
    """The backup before an import; on failure ``go_on(error)`` decides. False: stop."""
    if not settings.app.backup.before_import:
        return True
    from hiro_budget.services.backup import create_backup

    try:
        result.backup = create_backup(settings, "import")
    except Exception as e:
        log.warning("backup before import failed: %s", e, exc_info=e)
        result.backup_error = str(e)
        return go_on(e)
    return True


def preview(session: Session, settings: Settings, path: Path) -> ImportPreview:
    """A file's preview with the configured matching window and the connectors' cut-overs."""
    from hiro_budget.services.connections import cutovers

    return import_service.preview(
        session,
        path,
        match_days=settings.app.matching.days,
        cutovers=cutovers(session, settings),
        settings=settings,
    )


def import_files(
    session: SessionScope,
    settings: Settings,
    paths: list[Path],
    decide_overlap: OverlapDecision,
    *,
    backup_failed: Callable[[Exception], bool] = lambda e: True,
    on_preview: Callable[[ImportPreview], None] | None = None,
    on_done: Callable[[FileOutcome], None] | None = None,
) -> BatchResult:
    """Import ``paths`` in this order; see the module docstring. ``on_preview`` sees each
    file's first preview (the command line prints it), ``on_done`` each outcome."""
    result = BatchResult()
    if not backup_first(settings, result, backup_failed):
        result.stopped = True
        return result
    remembered: tuple[OverlapMode | None] | None = None  # an "apply to all" answer
    for path in paths:
        outcome = FileOutcome(path, "failed")
        try:
            with session() as s:
                p = outcome.preview = preview(s, settings, path)
            if on_preview is not None:
                on_preview(p)
            if p.file_already_imported:
                outcome.status = "already_imported"
                result.duplicates += p.duplicate_count
            elif p.errors:
                outcome.error = "; ".join(p.errors)
            elif not p.can_commit:
                outcome.status = "nothing_new"
                result.duplicates += p.duplicate_count
            else:
                if p.overlap is not None:
                    if remembered is None:
                        mode, apply_all = decide_overlap(p)
                        if apply_all:
                            remembered = (mode,)
                    else:
                        mode = remembered[0]
                    if mode is None:
                        outcome.status = "cancelled"
                    outcome.mode = mode
                if outcome.status != "cancelled":
                    _commit(session, settings, path, outcome, result)
        except Exception as e:  # keep importing the other files
            log.warning("import of %s failed", path.name, exc_info=e)
            outcome.status, outcome.error = "failed", str(e) or type(e).__name__
        result.files.append(outcome)
        if on_done is not None:
            on_done(outcome)
    return result


def _commit(
    session: SessionScope, settings: Settings, path: Path, outcome: FileOutcome, total: BatchResult
) -> None:
    with session() as s:  # again: an earlier file of the batch may overlap with this one
        p = outcome.preview = preview(s, settings, path)
        r = outcome.result = import_service.commit(s, settings, p, mode=outcome.mode, retrain=False)
    outcome.status = "imported"
    if p.statement.valuation:
        total.valuations += 1
        return
    total.added += r.added
    total.duplicates += r.duplicates
    total.replaced += r.replaced
    total.matched += r.matched
    total.linked += r.linking.linked
    total.linking = r.linking
