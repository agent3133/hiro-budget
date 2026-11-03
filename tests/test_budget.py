"""Budget computation: Ready to Assign, Available, overspending, targets, operations."""

from datetime import date

import pytest
from sqlalchemy import select

from hiro_budget.config import TargetDef
from hiro_budget.db.models import Account, Category
from hiro_budget.services import budget, import_service
from hiro_budget.services import transactions as txs
from hiro_budget.services.budget import CategoryMonth, Envelope, apply_target, month_add
from hiro_budget.services.transactions import ManualEntry

MARCH, APRIL, MAY = "2026-03", "2026-04", "2026-05"


def acc_id(s, name: str) -> int:
    return s.scalar(select(Account.id).where(Account.name == name))


def cat(s, key: str) -> int:
    return s.scalar(select(Category.id).where(Category.key == key))


def cash_booking(app, s, day: date, cents: int, key: str | None = None, payee="Test") -> int:
    return txs.create_manual(
        s,
        app.settings,
        ManualEntry(
            acc_id(s, "Bargeld"), day, cents, payee, category_id=cat(s, key) if key else None
        ),
    )


def check_invariant(s, settings, month: str) -> budget.BudgetMonth:
    b = budget.compute_month(s, settings, month)
    assert b.ready_to_assign + b.available + b.assigned_in_future == budget.budget_balance(
        s, month
    ), month
    assert b.balance == budget.budget_balance(s, month)
    # the bottom line of the budget tab adds up to Ready to Assign
    assert (
        b.balance_last_month
        + b.inflow
        - b.carried_in
        - b.assigned_this_month
        - b.assigned_in_future
        == b.ready_to_assign
    ), month
    assert b.carried_in == b.carry  # nothing carried is left out of the table
    return b


# --- months ------------------------------------------------------------------------


def test_month_helpers() -> None:
    assert month_add("2026-11", 3) == "2027-02"
    assert month_add("2026-01", -1) == "2025-12"
    assert budget.months_between("2026-11", "2027-02") == 3
    assert budget.month_last_day("2028-02") == date(2028, 2, 29)


# --- core model --------------------------------------------------------------------


def test_cash_only_budget(app) -> None:
    """Bargeld opens with 100 on 2026-03-01; nothing else."""
    with app.session() as s:
        b = check_invariant(s, app.settings, MARCH)
        assert b.start_month == MARCH
        assert b.ready_to_assign == 100_00
        budget.set_assigned(s, MARCH, cat(s, "lebensmittel.supermarkt"), 30_00)
        cash_booking(app, s, date(2026, 3, 10), -20_00, "lebensmittel.supermarkt")
        b = check_invariant(s, app.settings, MARCH)
        groceries = b.category(cat(s, "lebensmittel.supermarkt"))
        assert (groceries.assigned, groceries.activity, groceries.available) == (
            30_00,
            -20_00,
            10_00,
        )
        assert b.ready_to_assign == 70_00
        # Available carries over.
        april = check_invariant(s, app.settings, APRIL)
        assert april.category(cat(s, "lebensmittel.supermarkt")).available == 10_00
        assert april.ready_to_assign == 70_00


def test_overspending_is_taken_from_next_months_ready_to_assign(app) -> None:
    with app.session() as s:
        groceries = cat(s, "lebensmittel.supermarkt")
        budget.set_assigned(s, MARCH, groceries, 30_00)
        cash_booking(app, s, date(2026, 3, 10), -50_00, "lebensmittel.supermarkt")
        march = check_invariant(s, app.settings, MARCH)
        assert march.category(groceries).available == -20_00
        assert march.category(groceries).overspent
        assert march.ready_to_assign == 70_00  # not yet affected
        april = check_invariant(s, app.settings, APRIL)
        assert april.overspent_last_month == -20_00
        assert april.category(groceries).available == 0  # reset
        assert april.ready_to_assign == 50_00


def test_future_assignments_reduce_ready_to_assign(app) -> None:
    with app.session() as s:
        budget.set_assigned(s, MAY, cat(s, "freizeit.abos"), 40_00)
        march = check_invariant(s, app.settings, MARCH)
        assert march.assigned_in_future == 40_00
        assert march.ready_to_assign == 60_00


def test_statement_flows(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        b = check_invariant(s, app.settings, MARCH)
        # Giro opening 1000 + Bargeld 100 + salary 4000 + uncategorized outflows
        # (Hausverwaltung -250, Handwerker -120, mortgage principal -400 to the loan).
        assert b.ready_to_assign == 1000_00 + 100_00 + 4000_00 - 250_00 - 120_00 - 400_00
        assert b.uncategorized_count == 3
        assert b.uncategorized_amount == -(250_00 + 120_00 + 400_00)
        assert b.category(cat(s, "wohnen.baufinanzierung")).activity == -600_00
        assert b.category(cat(s, "sonstiges.online")).activity == -(2 * 9_09 + 32_50)
        # Income categories are not budget categories.
        assert all(not c.key.startswith("einkommen.") for c in b.categories)


def test_categorized_transfer_to_tracking_account_is_activity(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        mortgage = txs.list_transactions(s, txs.TxFilter(text="ZINSEN"))[0]
        loan = acc_id(s, "ING Baufinanzierung")
        txs.set_splits(
            s,
            mortgage.id,
            [
                txs.SplitInput(-600_00, cat(s, "wohnen.baufinanzierung")),
                txs.SplitInput(-400_00, cat(s, "wohnen.nebenkosten"), loan),
            ],
        )
        b = check_invariant(s, app.settings, MARCH)
        assert b.category(cat(s, "wohnen.nebenkosten")).activity == -400_00
        assert b.uncategorized_count == 2


def test_transfers_between_budget_accounts_do_not_change_budget(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        before = budget.compute_month(s, app.settings, MARCH).ready_to_assign
        hw = txs.list_transactions(s, txs.TxFilter(text="Handwerker"))[0]
        txs.mark_transfer(s, hw.id, acc_id(s, "Bargeld"))  # Giro -120 -> cash +120
        b = check_invariant(s, app.settings, MARCH)
        assert b.ready_to_assign == before + 120_00  # no longer an uncategorized outflow


def test_transfer_without_counter_booking_leaves_the_budget(app, statement_pdf) -> None:
    """Cash withdrawn before the cash account's history starts is gone from the budget."""
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        hw = txs.list_transactions(s, txs.TxFilter(text="Handwerker"))[0]  # 2026-03-10
        txs.mark_transfer(s, hw.id, acc_id(s, "Bargeld"))
        before = budget.compute_month(s, app.settings, MARCH)
        s.get(Account, acc_id(s, "Bargeld")).opening_date = date(2026, 3, 20)
        s.flush()
        b = check_invariant(s, app.settings, MARCH)
        assert b.ready_to_assign == before.ready_to_assign - 120_00
        assert b.uncategorized_count == before.uncategorized_count


def test_configured_start_month(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        cash_booking(app, s, date(2026, 4, 2), -10_00, "lebensmittel.supermarkt")
    app.settings.app.budget.start_month = APRIL
    with app.session() as s:
        b = check_invariant(s, app.settings, APRIL)
        assert b.start_month == APRIL
        # Everything up to March is start funds; April only has the groceries.
        assert b.ready_to_assign == budget.budget_balance(s, MARCH)
        assert b.category(cat(s, "lebensmittel.supermarkt")).activity == -10_00
        # Months before the start are clamped.
        assert budget.compute_month(s, app.settings, MARCH).month == APRIL


def test_invariant_over_several_months(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        groceries, fun = cat(s, "lebensmittel.supermarkt"), cat(s, "freizeit.restaurants")
        budget.set_assigned(s, MARCH, groceries, 100_00)
        budget.set_assigned(s, APRIL, fun, 50_00)
        cash_booking(app, s, date(2026, 4, 3), -70_00, "freizeit.restaurants")  # overspend
        cash_booking(app, s, date(2026, 5, 3), 25_00, "einkommen.sonstige_einnahmen")
        cash_booking(app, s, date(2026, 5, 4), -5_00)  # uncategorized (rules: none)
        for m in (MARCH, APRIL, MAY, month_add(MAY, 1)):
            check_invariant(s, app.settings, m)


def test_months_in_one_pass_equal_each_month_on_its_own(app, statement_pdf) -> None:
    """#59: ``compute_months`` (one pass, carrying what is available) gives every month
    exactly as ``compute_month`` (which replays all months before it)."""
    from dataclasses import asdict

    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        groceries, fun = cat(s, "lebensmittel.supermarkt"), cat(s, "freizeit.restaurants")
        budget.set_assigned(s, MARCH, groceries, 100_00)
        budget.set_assigned(s, APRIL, fun, 50_00)
        budget.set_assigned(s, month_add(MAY, 2), fun, 10_00)
        cash_booking(app, s, date(2026, 4, 3), -70_00, "freizeit.restaurants")  # overspend
        cash_booking(app, s, date(2026, 5, 3), 25_00, "einkommen.sonstige_einnahmen")
        start = budget.start_month(s, app.settings)
        last = month_add(MAY, 3)
        for balances in (True, False):
            together = list(budget.compute_months(s, app.settings, start, last, None, balances))
            months = [b.month for b in together]
            assert months[0] == start and months[-1] == last
            alone = [budget.compute_month(s, app.settings, m, None, balances) for m in months]
            assert [asdict(b) for b in together] == [asdict(b) for b in alone]
        later = list(budget.compute_months(s, app.settings, APRIL, MAY))
        assert [b.month for b in later] == [APRIL, MAY]
        assert asdict(later[1]) == asdict(budget.compute_month(s, app.settings, MAY))


# --- targets -----------------------------------------------------------------------


def _cm(**kw) -> CategoryMonth:
    return CategoryMonth(0, "g.c", "C", "g", "G", False, **kw)


def test_target_monthly() -> None:
    c = _cm(assigned=200_00, target=TargetDef(type="monthly", amount=600))
    apply_target(c, MARCH)
    assert (c.target_month, c.needed) == (600_00, 400_00)
    assert c.underfunded and c.progress == pytest.approx(1 / 3)


def test_target_by_date_spreads_over_remaining_months() -> None:
    t = TargetDef(type="by_date", amount=600, date=date(2026, 6, 15))
    c = _cm(target=t, carry=100_00)
    apply_target(c, MARCH)  # March..June = 4 months, 500 remaining
    assert (c.due, c.target_month, c.needed) == ("2026-06", 125_00, 125_00)
    late = _cm(target=t, carry=100_00)
    apply_target(late, "2026-08")  # past due: everything remaining now
    assert late.needed == 500_00


def test_target_repeating_restarts_after_due_month() -> None:
    t = TargetDef(type="repeating", amount=480, date=date(2026, 3, 1), every_months=12)
    c = _cm(target=t)
    apply_target(c, MARCH)
    assert (c.due, c.needed) == ("2026-03", 480_00)
    c = _cm(target=t)
    apply_target(c, APRIL)  # next cycle: due 2027-03, 12 months
    assert (c.due, c.needed) == ("2027-03", 40_00)


def test_target_balance_without_date_is_progress_only() -> None:
    c = _cm(carry=2500_00, target=TargetDef(type="balance", amount=5000))
    apply_target(c, MARCH)
    assert c.needed == 0 and c.progress == pytest.approx(0.5)
    dated = _cm(target=TargetDef(type="balance", amount=1200, date=date(2026, 12, 1)))
    apply_target(dated, MARCH)  # 10 months
    assert dated.needed == 120_00


# --- operations --------------------------------------------------------------------


def test_move_money_and_copy_last_month(app) -> None:
    with app.session() as s:
        groceries, fun = cat(s, "lebensmittel.supermarkt"), cat(s, "freizeit.restaurants")
        budget.set_assigned(s, MARCH, groceries, 60_00)
        budget.move_money(s, MARCH, groceries, fun, 20_00)
        assert budget.get_assigned(s, MARCH, groceries) == 40_00
        assert budget.get_assigned(s, MARCH, fun) == 20_00
        budget.move_money(s, MARCH, None, fun, 5_00)  # from Ready to Assign
        assert budget.get_assigned(s, MARCH, fun) == 25_00
        with pytest.raises(ValueError):
            budget.move_money(s, MARCH, fun, fun, 1)

        budget.set_assigned(s, APRIL, fun, 99_00)
        assert budget.copy_last_month(s, app.settings, APRIL) == 1  # only groceries
        assert budget.get_assigned(s, APRIL, groceries) == 40_00
        assert budget.get_assigned(s, APRIL, fun) == 99_00
        budget.set_assigned(s, APRIL, fun, 0)  # zero removes the row
        assert budget.get_assigned(s, APRIL, fun) == 0


def test_assign_to_targets_respects_ready_to_assign(app) -> None:
    groups = {g.key: g for g in app.settings.categories.groups}
    groups["lebensmittel"].categories[0].target = TargetDef(type="monthly", amount=80)
    groups["freizeit"].categories[0].target = TargetDef(type="monthly", amount=50)
    with app.session() as s:
        r = budget.assign_to_targets(s, app.settings, MARCH)  # only 100 available
        assert (r.categories, r.amount, r.still_needed) == (2, 100_00, 30_00)
        b = budget.compute_month(s, app.settings, MARCH)
        assert b.ready_to_assign == 0
        assert b.category(cat(s, "lebensmittel.supermarkt")).assigned == 80_00
        assert b.category(cat(s, "freizeit.restaurants")).needed == 30_00


def test_cover_overspending_respects_ready_to_assign(app) -> None:
    with app.session() as s:  # only the 100 opening cash, nothing assigned
        cash_booking(app, s, date(2026, 3, 5), -30_00, "lebensmittel.supermarkt")
        cash_booking(app, s, date(2026, 3, 6), -90_00, "freizeit.restaurants")
        r = budget.cover_overspending(s, app.settings, MARCH)
        assert (r.categories, r.amount, r.still_needed) == (2, 100_00, 20_00)
        b = check_invariant(s, app.settings, MARCH)
        assert b.ready_to_assign == 0
        assert b.category(cat(s, "lebensmittel.supermarkt")).available == 0  # budget order
        assert b.category(cat(s, "freizeit.restaurants")).available == -20_00
        r = budget.cover_overspending(s, app.settings, MARCH)
        assert (r.categories, r.amount, r.still_needed) == (0, 0, 20_00)


def test_save_target_roundtrip_and_kept_by_category_save(app, config_dir) -> None:
    from hiro_budget.config import load_settings
    from hiro_budget.services import config_edit

    t = TargetDef(type="repeating", amount=480, date=date(2027, 3, 1), every_months=12)
    config_edit.save_target(app.settings, "versicherungen.haftpflicht_hausrat", t)
    config_edit.save_target(
        app.settings, "lebensmittel.supermarkt", TargetDef(type="monthly", amount=600)
    )
    reloaded = load_settings(config_dir)
    cats = {f"{g.key}.{c.key}": c for g in reloaded.categories.groups for c in g.categories}
    assert cats["versicherungen.haftpflicht_hausrat"].target == t
    assert cats["lebensmittel.supermarkt"].target.amount == 600

    # Saving categories (e.g. a rename in the category editor) keeps targets.
    cfg = reloaded.categories.model_copy(deep=True)
    cfg.groups[0].categories[0].name = "Lohn"
    config_edit.save_categories(app.settings, cfg)
    again = load_settings(config_dir)
    cats = {f"{g.key}.{c.key}": c for g in again.categories.groups for c in g.categories}
    assert cats["lebensmittel.supermarkt"].target.amount == 600

    config_edit.save_target(app.settings, "lebensmittel.supermarkt", None)
    cats = {
        f"{g.key}.{c.key}": c
        for g in load_settings(config_dir).categories.groups
        for c in g.categories
    }
    assert cats["lebensmittel.supermarkt"].target is None


def test_clear_and_restore_assignments(app) -> None:
    with app.session() as s:
        groceries, fun = cat(s, "lebensmittel.supermarkt"), cat(s, "freizeit.restaurants")
        budget.set_assigned(s, MARCH, groceries, 40_00)
        budget.set_assigned(s, MARCH, fun, 20_00)
        budget.set_assigned(s, APRIL, fun, 5_00)
        saved = budget.assignments_of(s, MARCH)
        assert saved == {Envelope.of(groceries): 40_00, Envelope.of(fun): 20_00}
        assert budget.clear_month(s, MARCH) == 2
        assert budget.assignments_of(s, MARCH) == {}
        assert budget.get_assigned(s, APRIL, fun) == 5_00  # other months untouched
        budget.set_assigned(s, MARCH, fun, 99_00)
        budget.restore_assignments(s, MARCH, saved)
        assert budget.assignments_of(s, MARCH) == saved
        check_invariant(s, app.settings, MARCH)
