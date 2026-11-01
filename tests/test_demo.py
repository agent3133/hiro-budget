"""The demo database generator (demo/generate.py), run for a short span."""

from collections import Counter
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

pytest.importorskip("faker")

from demo.generate import CONFIG_DIR, generate  # noqa: E402 - needs faker

from hiro_budget.app import App  # noqa: E402
from hiro_budget.config import load_settings  # noqa: E402
from hiro_budget.db.models import (  # noqa: E402
    Account,
    BudgetAssignment,
    ImportBatch,
    Transaction,
)
from hiro_budget.services import budget  # noqa: E402
from hiro_budget.services.accounts import statement_gaps, statement_links  # noqa: E402


@pytest.fixture(scope="module")
def demo(tmp_path_factory) -> App:
    # Four months including a quarter's Depotauszug and the yearly loan statement
    db = generate(
        tmp_path_factory.mktemp("demo") / "demo.db", start="2025-11", months=4, quiet=True
    )
    app = App(load_settings(CONFIG_DIR), db)
    yield app
    app.session_factory.kw["bind"].dispose()


def test_every_booking_is_categorized(demo: App):
    with demo.session() as s:
        txs = s.scalars(select(Transaction).options(selectinload(Transaction.splits))).all()
        assert len(txs) > 200
        for tx in txs:
            assert tx.splits, tx.display_name
            assert sum(sp.amount_cents for sp in tx.splits) == tx.amount_cents
            assert all(not sp.is_uncategorized for sp in tx.splits), tx.display_name
            assert tx.is_transfer or tx.is_counterpart or all(sp.category for sp in tx.splits)


def test_accounts(demo: App):
    with demo.session() as s:
        kinds = Counter(str(a.kind) for a in s.scalars(select(Account)))
    assert kinds == {"giro": 2, "savings": 1, "loan": 1, "depot": 1, "cash": 2}


def test_statements_add_up(demo: App):
    with demo.session() as s:
        batches = s.scalars(select(ImportBatch).options(selectinload(ImportBatch.holdings))).all()
        by_account: dict[int, list[ImportBatch]] = {}
        for b in batches:
            by_account.setdefault(b.account_id, []).append(b)
            if b.valuation:
                if b.holdings:  # Depotauszug: positions add up to the total
                    assert sum(h.value_cents for h in b.holdings) == b.closing_balance_cents
                continue
            booked = s.scalar(
                select(func.coalesce(func.sum(Transaction.amount_cents), 0)).where(
                    Transaction.batch_id == b.id
                )
            )
            assert b.opening_balance_cents + booked == b.closing_balance_cents, b.source_file
        for account_batches in by_account.values():
            assert not statement_gaps(statement_links(account_batches))
        importers = Counter(b.importer for b in batches)
    # 4 months of Girokonto, Extra-Konto and Revolut, 1 Depotauszug, 1 loan statement
    assert importers == {"ing_pdf": 8, "revolut_pdf": 4, "ing_depot_pdf": 1, "ing_loan_pdf": 1}


def test_budget(demo: App):
    with demo.session() as s:
        months = sorted(s.scalars(select(BudgetAssignment.month).distinct()))
        assert months == ["2025-11", "2025-12", "2026-01", "2026-02"]
        b = budget.compute_month(s, demo.settings, "2026-02")
        assert b.uncategorized_count == 0
        assert not [e.name for e in b.envelopes if e.overspent]
        assert b.ready_to_assign >= 0


def test_reproducible(tmp_path: Path, demo: App):
    """The same seed gives the same bookings."""
    db = generate(tmp_path / "again.db", start="2026-01", months=1, quiet=True)
    again = App(load_settings(CONFIG_DIR), db)
    try:
        with demo.session() as a, again.session() as b:
            q = (
                select(Transaction.dedupe_key, Transaction.amount_cents, Transaction.purpose)
                .where(Transaction.batch_id.is_not(None), Transaction.booking_date >= "2026-01-01")
                .where(Transaction.booking_date < "2026-02-01")
                .order_by(Transaction.dedupe_key)
            )
            assert a.execute(q).all() == b.execute(q).all()
    finally:
        again.session_factory.kw["bind"].dispose()


def test_demo_command_opens_a_fresh_copy(tmp_path, monkeypatch):
    import shutil

    import hiro_budget.gui.main
    from hiro_budget import __main__, demo_mode

    source = tmp_path / "demo"
    shutil.copytree(CONFIG_DIR, source / "config")
    generate(
        source / demo_mode.DB, start="2026-09", months=1, config_dir=source / "config", quiet=True
    )
    target = tmp_path / "run"
    config = demo_mode.prepare(target, source=source)
    app = App(load_settings(config))
    with app.session() as s:
        count = s.scalar(select(func.count()).select_from(Transaction))
    assert count > 0
    with app.session() as s:  # the user plays with the demo …
        s.execute(Transaction.__table__.delete())
    app.session_factory.kw["bind"].dispose()  # the window is closed
    again = demo_mode.prepare(target, source=source)  # … and the next start is fresh again
    assert again == config
    with App(load_settings(again)).session() as s:
        assert s.scalar(select(func.count()).select_from(Transaction)) == count

    opened = []
    monkeypatch.setattr(demo_mode, "prepare", lambda: config)
    monkeypatch.setattr(
        hiro_budget.gui.main, "run_gui", lambda settings: opened.append(settings) or 0
    )
    assert __main__.main(["demo"]) == 0
    assert opened[0].config_dir == config
