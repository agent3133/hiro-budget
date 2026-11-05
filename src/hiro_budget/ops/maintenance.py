"""Operations: import statement files, categorize everything again, retrain the classifier.

Their command line is the existing one (``import``, ``recategorize``, ``train`` in
``__main__``, with the interactive overlap question); ``cli`` is empty here."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from hiro_budget.ops import Args, Changed, OpContext, Operation, OpError, render_changed


class ImportArgs(Args):
    paths: list[str] = Field(
        description="statement files or folders (a folder: all statement files in it)",
        min_length=1,
    )
    on_overlap: Literal["add", "overwrite", "cancel"] = Field(
        "cancel",
        description=(
            "a statement overlapping stored bookings: add (keep both), overwrite (replace the "
            "stored ones) or cancel (skip the file)"
        ),
    )


def run_import(ctx: OpContext, a: ImportArgs) -> Changed:
    from pathlib import Path

    from hiro_budget.services.categorization.pipeline import train_classifier
    from hiro_budget.services.import_batch import import_files
    from hiro_budget.services.import_service import OverlapMode, is_statement_file

    files: list[Path] = []
    for text in a.paths:
        p = Path(text)
        if not p.exists():
            raise OpError(f"'{text}' does not exist")
        if p.is_dir():
            files.extend(sorted(f for f in p.iterdir() if f.is_file() and is_statement_file(f)))
        else:
            files.append(p)
    if not files:
        raise OpError("no statement files found")
    mode = None if a.on_overlap == "cancel" else OverlapMode(a.on_overlap)

    def decide(preview):
        return mode, True

    with ctx.write("import_files", a):
        batch = import_files(ctx.app.session, ctx.settings, files, decide)
        if batch.stopped:
            raise OpError("the backup failed; nothing was imported")
        details = []
        for f in batch.files:
            if f.status == "imported" and f.result is not None:
                details.append(f"{f.path.name}: imported {f.result.added} bookings")
            else:
                details.append(f"{f.path.name}: {f.status}" + (f" ({f.error})" if f.error else ""))
        if batch.imported_any and ctx.settings.app.ml.enabled:
            with ctx.session() as s:
                clf = train_classifier(s, ctx.settings)
            if clf is not None:
                details.append(f"classifier trained on {clf.n_samples} bookings")
    return Changed(
        message=(
            f"{batch.count('imported')} of {len(files)} files imported, {batch.added} new bookings"
        ),
        details=details,
    )


class NoArgs(Args):
    pass


def run_recategorize(ctx: OpContext, a: NoArgs) -> Changed:
    from hiro_budget.services.categorization.pipeline import recategorize_all

    with ctx.write("recategorize", a), ctx.session() as s:
        stats = recategorize_all(s, ctx.settings)
    return Changed(message=f"categorization: {stats}")


def run_train(ctx: OpContext, a: NoArgs) -> Changed:
    from hiro_budget.services.categorization.pipeline import train_classifier

    with ctx.write("train", a), ctx.session() as s:
        clf = train_classifier(s, ctx.settings)
    if clf is None:
        raise OpError(
            f"not enough training data (need {ctx.settings.app.ml.min_samples} "
            "categorized bookings)"
        )
    return Changed(
        message=f"classifier trained on {clf.n_samples} bookings -> {ctx.settings.model_path}"
    )


OPERATIONS = [
    Operation(
        name="import_files",
        cli=(),
        summary="Import statement files",
        description=(
            "Import ING / Revolut statements (PDF, CSV by plugin) from files or folders on "
            "this computer. A backup is made first. Not undoable: restore the backup. "
            "on_overlap decides what happens to a statement that overlaps stored bookings."
        ),
        args=ImportArgs,
        result=Changed,
        run=run_import,
        render=render_changed,
        read_only=False,
    ),
    Operation(
        name="recategorize",
        cli=(),
        summary="Categorize all bookings again",
        description=(
            "Run transfers, rules and the classifier again on all bookings. Manually "
            "categorized bookings are never changed."
        ),
        args=NoArgs,
        result=Changed,
        run=run_recategorize,
        render=render_changed,
        read_only=False,
    ),
    Operation(
        name="train",
        cli=(),
        summary="Retrain the classifier",
        description="Train the classifier on the categorized bookings. Not undoable.",
        args=NoArgs,
        result=Changed,
        run=run_train,
        render=render_changed,
        read_only=False,
    ),
]
