"""Command line entry point: ``hiro-budget [--config-dir DIR] [gui|import|recategorize|train]``."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from hiro_budget.config import ConfigError, Settings, load_settings

log = logging.getLogger("hiro_budget")


def _setup_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.app.logging.level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hiro-budget", description=__doc__)
    p.add_argument("--config-dir", type=Path, help="directory with config.yaml (default ./config)")
    sub = p.add_subparsers(dest="command")
    sub.add_parser("gui", help="start the graphical interface (default)")
    imp = sub.add_parser("import", help="import statement files or folders")
    imp.add_argument("paths", nargs="+", type=Path)
    imp.add_argument(
        "--on-overlap",
        choices=["add", "overwrite", "cancel"],
        help="how to handle statements overlapping stored bookings (default: ask)",
    )
    sub.add_parser("recategorize", help="re-run categorization on all non-manual bookings")
    sub.add_parser("train", help="retrain the ML classifier")
    sub.add_parser(
        "demo", help="open the app on a fictional family's data (nothing of yours is used)"
    )
    dump = sub.add_parser(
        "dump-pdf", help="print the text of a PDF as importers see it (for writing a plugin)"
    )
    dump.add_argument("path", type=Path)
    dump.add_argument(
        "--x-tolerance",
        type=float,
        default=1.5,
        help="how far apart characters may be and still form a word (default 1.5, as ING)",
    )
    dump.add_argument(
        "--mask",
        action="store_true",
        help="replace IBANs, e-mail addresses and long numbers (names: replace by hand)",
    )
    return p


def cmd_dump_pdf(path: Path, x_tolerance: float, masked: bool) -> int:
    from hiro_budget.importers.textdump import dump

    try:
        text = dump(path, x_tolerance, masked)
    except Exception as e:  # not a readable PDF
        print(f"cannot read {path}: {e}", file=sys.stderr)
        return 1
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # umlauts and € on the Windows console
    sys.stdout.write(text)
    return 0


def _collect_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_dir():
            from hiro_budget.services.import_service import is_statement_file

            files.extend(sorted(f for f in p.iterdir() if f.is_file() and is_statement_file(f)))
        else:
            files.append(p)
    return files


def _ask_overlap(name: str, p) -> str:
    """Interactive prompt for an overlapping statement; returns add/overwrite/cancel."""
    o = p.overlap
    print(
        f"  overlap: account already has {o.existing_count} bookings between "
        f"{o.date_from} and {o.date_to} (earlier imports: {', '.join(o.files) or '-'})"
    )
    print(f"    [a]dd missing  - import only the {p.new_count} bookings not stored yet")
    print(
        f"    [o]verwrite    - replace the {o.existing_count} stored bookings in this period "
        f"with all {len(p.statement.transactions)} bookings of the file"
    )
    if o.removed_on_overwrite:
        print(
            f"                     ({o.removed_on_overwrite} stored bookings are not in the file)"
        )
    if o.manual_kept or o.manual_lost:
        print(f"                     (manual work: {o.manual_kept} kept, {o.manual_lost} lost)")
    print("    [c]ancel       - skip this file")
    choices = {"a": "add", "o": "overwrite", "c": "cancel"}
    while True:
        answer = input(f"  {name}: [a/o/c]? ").strip().lower()[:1]
        if answer in choices:
            return choices[answer]


def cmd_import(settings: Settings, paths: list[Path], on_overlap: str | None) -> int:
    from hiro_budget.app import App
    from hiro_budget.importers.base import ImportError_
    from hiro_budget.services import import_service
    from hiro_budget.services.bank.fetch import cutovers
    from hiro_budget.services.import_service import OverlapMode

    app = App(settings)
    if settings.app.backup.before_import:
        from hiro_budget.services.backup import create_backup

        try:
            print(f"backup: {create_backup(settings, 'import')}")
        except Exception as e:
            print(f"warning: backup failed: {e}", file=sys.stderr)
    failed = 0
    for path in _collect_files(paths):
        try:
            with app.session() as s:
                p = import_service.preview(
                    s,
                    path,
                    match_days=settings.app.matching.days,
                    cutovers=cutovers(s, settings),
                    settings=settings,
                )
            stmt = p.statement
            if stmt.valuation:
                print(
                    f"{path.name}: balance statement of {p.account_name} on "
                    f"{stmt.period_end}, {len(stmt.holdings)} positions, "
                    f"total {'OK' if stmt.balance_ok else 'NOT OK'}"
                    + (f", replaces {p.replaces_valuation}" if p.replaces_valuation else "")
                )
            else:
                print(
                    f"{path.name}: {len(stmt.transactions)} bookings, account {p.account_name}, "
                    f"balance {'OK' if stmt.balance_ok else 'NOT OK'}, "
                    f"{p.new_count} new, {p.duplicate_count} duplicates"
                )
            if p.file_already_imported:
                print("  skipped: already imported")
                continue
            if p.errors:
                print("  NOT imported: " + "; ".join(p.errors))
                failed += 1
                continue
            mode = None
            if p.overlap is not None:
                choice = on_overlap
                if choice is None:
                    if not sys.stdin.isatty():
                        print("  skipped: overlaps stored bookings; use --on-overlap to decide")
                        continue
                    choice = _ask_overlap(path.name, p)
                if choice == "cancel":
                    print("  cancelled")
                    continue
                mode = OverlapMode(choice)
            elif p.new_count == 0 and not stmt.valuation:
                print("  nothing new")
                continue
            with app.session() as s:
                p = import_service.preview(
                    s,
                    path,
                    match_days=settings.app.matching.days,
                    cutovers=cutovers(s, settings),
                    settings=settings,
                )
                result = import_service.commit(s, settings, p, mode=mode)
            if stmt.valuation:
                print(f"  imported, {result.holdings} positions")
                continue
            replaced = f", replaced {result.replaced}" if result.replaced else ""
            if result.matched:
                replaced += f", {result.matched} manual bookings matched"
            print(f"  imported {result.added}{replaced}; categorization: {result.categorization}")
            for w in result.categorization.warnings:
                print(f"  warning: {w}")
        except (ImportError_, OSError) as e:
            print(f"{path.name}: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def cmd_recategorize(settings: Settings) -> int:
    from hiro_budget.app import App
    from hiro_budget.services.categorization.pipeline import recategorize_all

    app = App(settings)
    with app.session() as s:
        stats = recategorize_all(s, settings)
    print(f"categorization: {stats}")
    return 0


def cmd_train(settings: Settings) -> int:
    from hiro_budget.app import App
    from hiro_budget.services.categorization.pipeline import train_classifier

    app = App(settings)
    with app.session() as s:
        clf = train_classifier(s, settings)
    if clf is None:
        print(f"not enough training data (need {settings.app.ml.min_samples} categorized bookings)")
        return 1
    print(f"classifier trained on {clf.n_samples} bookings -> {settings.model_path}")
    return 0


def cmd_gui(settings: Settings) -> int:
    from hiro_budget.gui.main import run_gui

    return run_gui(settings)


def _show_config_error(message: str) -> None:
    from PySide6.QtWidgets import QApplication, QMessageBox

    qapp = QApplication.instance() or QApplication(sys.argv[:1])
    QMessageBox.critical(
        None,
        "Hiro Budget",
        f"The configuration cannot be read / Die Konfiguration kann nicht gelesen werden:"
        f"\n\n{message}",
    )
    del qapp


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "dump-pdf":  # needs no configuration
        return cmd_dump_pdf(args.path, args.x_tolerance, args.mask)
    if args.command == "demo":  # its own configuration, a fresh copy every time
        from hiro_budget.demo_mode import prepare

        try:
            args.config_dir = prepare()
        except RuntimeError as e:
            print(f"Demo: {e}", file=sys.stderr)
            _show_config_error(str(e))
            return 2
        args.command = "gui"
    try:
        settings = load_settings(args.config_dir)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        if args.command in (None, "gui"):  # started from Explorer: no console to read it
            _show_config_error(str(e))
        return 2
    _setup_logging(settings)
    match args.command:
        case "import":
            return cmd_import(settings, args.paths, args.on_overlap)
        case "recategorize":
            return cmd_recategorize(settings)
        case "train":
            return cmd_train(settings)
        case _:
            return cmd_gui(settings)


if __name__ == "__main__":
    sys.exit(main())
