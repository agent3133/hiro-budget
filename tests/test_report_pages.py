"""Report pages on the reports tab: net worth, budget vs. actual, ... (services and GUI)."""

from datetime import date

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox
from sqlalchemy import select

from hiro_budget.db.models import Account, Transaction
from hiro_budget.services import accounts as acc_service
from hiro_budget.services import import_service, networth, savings
from hiro_budget.services import transactions as txs
from hiro_budget.services.counterparts import new_manual_key
from hiro_budget.services.transactions import ManualEntry


@pytest.fixture(autouse=True)
def no_message_boxes(monkeypatch):
    for name in ("information", "warning", "critical"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)


def account_id(s, name: str) -> int:
    return s.scalar(select(Account.id).where(Account.name == name))


@pytest.fixture
def imported(app, statement_pdf):
    """Girokonto statement of March 2026 (1.000,00 -> 3.555,87), cash from 01.03.2026."""
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        txs.create_manual(
            s,
            app.settings,
            ManualEntry(account_id(s, "Bargeld"), date(2026, 4, 3), -20_00, "Kiosk"),
        )
    return app


# --- net worth -------------------------------------------------------------------------


def test_net_worth_per_month(imported) -> None:
    with imported.session() as s:
        data = networth.net_worth(s, "2026-04")
        balances = {b.name: b.balance_cents for b in acc_service.account_balances(s)}
    assert data.months == ["2026-03", "2026-04"]
    by_name = {a.name: a for a in data.accounts}
    assert by_name["ING Girokonto"].balances == {"2026-03": 3555_87, "2026-04": 3555_87}
    assert by_name["Bargeld"].balances == {"2026-03": 100_00, "2026-04": 80_00}
    # the last month matches the balances of the accounts tab
    assert data.net("2026-04") == sum(balances.values())
    assert data.assets("2026-04") + data.debts("2026-04") == data.net("2026-04")


def test_cash_counts_as_zero_before_it_was_tracked(imported) -> None:
    with imported.session() as s:
        s.get(Account, account_id(s, "Bargeld")).opening_date = date(2026, 4, 2)
        s.flush()
        data = networth.net_worth(s, "2026-04")
    cash = next(a for a in data.accounts if a.name == "Bargeld")
    assert data.months[0] == "2026-03"  # cash does not delay the start
    assert cash.balances["2026-03"] == 0


def test_loan_is_worked_back_from_its_statement(config_dir, tmp_path) -> None:
    from hiro_budget.app import App
    from tests.conftest import write_statement_pdf
    from tests.fixtures import ing_loan as fx
    from tests.test_loan import LOAN_CONFIG

    (config_dir / "config.yaml").write_text(LOAN_CONFIG, encoding="utf-8")
    app = App.from_config_dir(config_dir)
    pdf = write_statement_pdf(tmp_path / "Direkt_Baufinanzierung.pdf", fx.PAGES)
    with app.session() as s:
        loan = s.scalar(select(Account).where(Account.name == "ING Baufinanzierung"))
        for day, cents in ((date(2025, 11, 28), 1_000_00), (date(2026, 1, 30), 1_003_00)):
            s.add(
                Transaction(
                    account=loan,
                    booking_date=day,
                    amount_cents=cents,
                    booking_type="Umbuchung",
                    dedupe_key=new_manual_key(),
                )
            )
        s.flush()
        import_service.commit(s, app.settings, import_service.preview(s, pdf))
    with app.session() as s:
        data = networth.net_worth(s, "2026-01", account_id(s, "ING Baufinanzierung"))
    loan = data.accounts[0]
    assert loan.balances["2025-12"] == fx.CLOSING_CENTS  # the statement of 31.12.2025
    assert loan.balances["2025-11"] == fx.CLOSING_CENTS  # payment of 28.11. already made
    assert loan.balances["2026-01"] == fx.CLOSING_CENTS + 1_003_00
    assert data.months[0] == "2025-11"  # back to its first booking, not further
    assert data.debts("2026-01") == fx.CLOSING_CENTS + 1_003_00


@pytest.fixture
def opening_only(imported):
    """Plus an account with only an opening balance (no bookings) late in April."""
    from hiro_budget.db.models import AccountKind

    with imported.session() as s:
        s.add(
            Account(
                name="DKB Girokonto",
                kind=AccountKind.GIRO,
                opening_balance_cents=500_00,
                opening_date=date(2026, 4, 24),
            )
        )
    return imported


def test_account_without_history_counts_back(opening_only) -> None:
    with opening_only.session() as s:
        data = networth.net_worth(s, "2026-04")
        rates = savings.savings_rates(s, date(2026, 3, 1), date(2026, 4, 30))
    assert data.months == ["2026-03", "2026-04"]  # not cut to April
    dkb = next(a for a in data.accounts if a.name == "DKB Girokonto")
    assert dkb.balances == {"2026-03": None, "2026-04": 500_00}  # raw: unknown in March
    assert dkb.estimated("2026-03") and not dkb.estimated("2026-04")
    assert dkb.value("2026-03") == 500_00
    others = sum(a.balances["2026-03"] or 0 for a in data.accounts if a is not dkb)
    assert data.net("2026-03") == others + 500_00  # counted in the total
    assert [a.name for a, _ in data.estimated(data.months)] == ["DKB Girokonto"]
    # no bookings, so no income or expenses missing: complete from the start
    assert data.complete_from == "2026-03" and rates.complete_from == "2026-03"


def test_net_worth_page_marks_estimates(qtbot, opening_only) -> None:
    from hiro_budget.gui.report_pages import NetWorthReport

    page = NetWorthReport(opening_only)
    qtbot.addWidget(page)
    page.refresh(date(2026, 3, 1), date(2026, 4, 30), None)
    row = next(
        r for r in range(page.table.rowCount()) if page.table.item(r, 0).text() == "DKB Girokonto"
    )
    assert page.table.item(row, 1).text().startswith("≈")
    assert not page.table.item(row, 2).text().startswith("≈")
    assert page.table.item(row, 3).text().startswith("0")  # carried back: no change
    assert "DKB Girokonto" in page.note.text()


def test_net_worth_page(qtbot, imported) -> None:
    from hiro_budget.gui.report_pages import NetWorthReport

    page = NetWorthReport(imported)
    qtbot.addWidget(page)
    page.refresh(date(2026, 3, 1), date(2026, 4, 30), None)
    names = [page.table.item(r, 0).text() for r in range(page.table.rowCount())]
    assert "ING Girokonto" in names and page.table.rowCount() == len(names)
    assert page.view.chart().series() and page.parts_view.chart().series()
    page.refresh(date(2030, 1, 1), date(2030, 2, 1), None)  # nothing in this period
    assert page.table.rowCount() == 0 and page.note.text()


def test_reports_view_has_a_tab_per_report(qtbot, imported) -> None:
    from hiro_budget.gui.report_pages import ReportPage
    from hiro_budget.gui.reports_view import ReportsView

    view = ReportsView(imported)
    qtbot.addWidget(view)
    refreshed = []
    for page in view.pages:
        page.refresh = lambda *a, p=page, **k: refreshed.append(p)  # noqa: B023
    view.refresh()
    assert refreshed == []  # only the visible tab is drawn
    for i in range(1, view.tabs.count()):
        view.tabs.setCurrentIndex(i)
        assert isinstance(view.tabs.currentWidget(), ReportPage)
    assert refreshed == view.pages
    view.tabs.setCurrentIndex(1)
    assert refreshed == view.pages  # not drawn again until the filter changes


# --- budget vs. actual -----------------------------------------------------------------


def test_budget_vs_actual(imported) -> None:
    from hiro_budget.db.models import Category
    from hiro_budget.services import budget, budget_report

    with imported.session() as s:
        groceries = s.scalar(select(Category.id).where(Category.key == "lebensmittel.supermarkt"))
        budget.set_assigned(s, "2026-03", groceries, 100_00)
        data = budget_report.budget_vs_actual(s, imported.settings, "2026-01", "2026-04")
        only = budget_report.budget_vs_actual(
            s, imported.settings, "2026-01", "2026-04", only_budgeted=True
        )
        march = budget.compute_month(s, imported.settings, "2026-03")
    # the budget starts in March (first bookings): earlier months are left out
    assert [m.month for m in data.months] == ["2026-03", "2026-04"]
    assert [m.month for m in only.months] == ["2026-03"]
    line = next(line for line in data.lines if line.key == "lebensmittel.supermarkt")
    spent = -march.category(groceries).activity
    assert (line.assigned, line.spent) == (100_00, spent)
    assert line.difference == 100_00 - spent
    assert line.used == spent / 100_00
    assert data.assigned == 100_00
    assert data.spent == sum(-e.activity for e in march.envelopes) + data.months[1].spent


def test_used_share_without_meaning(imported) -> None:
    from hiro_budget.services.budget_report import LineActual

    assert LineActual("k", "n", "g", False, 0, 50_00).used is None  # nothing assigned
    assert LineActual("k", "n", "g", False, 50_00, -10_00).used is None  # refunds only
    assert LineActual("k", "n", "g", False, 50_00, 25_00).used == 0.5


def test_budget_vs_actual_page(qtbot, imported) -> None:
    from hiro_budget.db.models import Category
    from hiro_budget.gui.report_pages import BudgetActualReport
    from hiro_budget.services import budget

    with imported.session() as s:
        groceries = s.scalar(select(Category.id).where(Category.key == "lebensmittel.supermarkt"))
        budget.set_assigned(s, "2026-03", groceries, 100_00)
    page = BudgetActualReport(imported)
    qtbot.addWidget(page)
    page.refresh(date(2026, 1, 1), date(2026, 4, 30), None)
    rows = [page.table.item(r, 0).text() for r in range(page.table.rowCount())]
    assert any("Supermarkt" in r for r in rows)
    assert page.table.item(page.table.rowCount() - 1, 1).text().startswith("100")  # total
    assert page.view.chart().series()[0].barSets()[0].count() == 1  # only March is budgeted
    page.only_budgeted.setChecked(False)
    assert page.view.chart().series()[0].barSets()[0].count() == 2


# --- savings rate ----------------------------------------------------------------------


def test_savings_rates(imported) -> None:
    from hiro_budget.services import reports, savings

    with imported.session() as s:
        data = savings.savings_rates(s, date(2026, 3, 1), date(2026, 4, 30))
        months = reports.monthly_totals(s, date(2026, 3, 1), date(2026, 4, 30))
        totals = {t.month: t for t in months}
    assert [m.month for m in data.months] == ["2026-03", "2026-04"]
    march = data.months[0]
    assert (march.income_cents, march.expense_cents) == (
        totals["2026-03"].income_cents,
        totals["2026-03"].expense_cents,
    )
    assert march.rate == pytest.approx(march.saved_cents / march.income_cents)
    assert data.months[1].rate is None  # April: no income, only the cash purchase
    assert march.rolling_rate is None  # fewer than 3 months of complete data
    assert data.complete_from == "2026-03"
    assert data.rate == pytest.approx(data.saved_cents / data.income_cents)


def test_rolling_rate_is_saved_over_income() -> None:
    from hiro_budget.services.savings import SavingsMonth

    m = SavingsMonth(
        "2026-06",
        1_000_00,
        400_00,
        rolling_income=6_000_00,
        rolling_saved=1_500_00,
        rolling_months=3,
    )
    assert m.saved_cents == 600_00 and m.rate == 0.6
    assert m.rolling_rate == 0.25
    m.rolling_months = 2
    assert m.rolling_rate is None


def test_savings_rate_page(qtbot, imported) -> None:
    from hiro_budget.gui.report_pages import SavingsRateReport

    page = SavingsRateReport(imported)
    qtbot.addWidget(page)
    page.refresh(date(2026, 3, 1), date(2026, 4, 30), None)
    assert page.table.rowCount() == 3  # two months and the period
    assert page.table.item(2, 4).text().endswith("%")
    assert len(page.view.chart().series()) == 2  # bars and the 12-month line


def test_costs_by_month(payments) -> None:
    from hiro_budget.services import recurring

    with payments.session() as s:
        costs = recurring.costs_by_month(s, date(2026, 1, 1), date(2026, 10, 31))
    # January-April the gym (ended), from June the streaming subscription; the DIY store
    # is no fixed cost, the top-ups no expense
    assert {m: (c.fixed_cents, c.variable_cents) for m, c in costs.items()} == {
        "2026-01": (15_00, 0),
        "2026-02": (15_00, 0),
        "2026-03": (15_00, 0),
        "2026-04": (15_00, 45_10),
        "2026-06": (9_99, 0),
        "2026-07": (9_99, 0),
        "2026-08": (9_99, 0),
        "2026-09": (9_99, 0),
        "2026-10": (9_99, 48_90),
    }


# --- year comparison -------------------------------------------------------------------


def test_year_comparison(imported) -> None:
    from hiro_budget.services import reports

    with imported.session() as s:
        txs.create_manual(
            s,
            imported.settings,
            ManualEntry(account_id(s, "Bargeld"), date(2025, 3, 10), -30_00, "Kiosk 2025"),
        )
        march = reports.year_comparison(s, 2025, 2026, last_month=3)
        whole = reports.year_comparison(s, 2025, 2026)
        cats_2026 = reports.category_totals(s, date(2026, 1, 1), date(2026, 3, 31))
    # January - March only: the cash purchase of April 2026 is not counted
    assert march.expenses[1] == sum(c.expense_cents for c in cats_2026)
    assert whole.expenses[1] == march.expenses[1] + 20_00
    assert march.expenses[0] == 30_00  # the 2025 purchase (uncategorized)
    assert [r.second for r in march.rows] == sorted((r.second for r in march.rows), reverse=True)
    assert march.income[1] > 0 and march.income[0] == 0


def test_year_row_change() -> None:
    from hiro_budget.services.reports import YearRow

    row = YearRow("k", "g", "n", first=200_00, second=300_00)
    assert row.difference == 100_00 and row.change == 0.5
    assert YearRow("k", "g", "n", first=0, second=10_00).change is None


def test_year_comparison_page(qtbot, imported) -> None:
    from hiro_budget.gui.report_pages import YearComparisonReport

    page = YearComparisonReport(imported)
    qtbot.addWidget(page)
    page.refresh(date(2026, 3, 1), date(2026, 4, 30), None)
    assert page.second_year.currentData() == 2026
    headers = [page.table.horizontalHeaderItem(c).text() for c in (1, 2)]
    assert headers == [str(page.first_year.currentData()), "2026"]
    expenses = page.table.item(page.table.rowCount() - 1, 0).text()
    assert expenses in ("Expenses", "Ausgaben")  # totals at the end
    assert page.view.chart().series()


# --- fixed costs & subscriptions -------------------------------------------------------


@pytest.fixture
def payments(app):
    """Cash bookings: a subscription, a yearly fee, shopping, a top-up, a cancelled one."""
    with app.session() as s:
        cash, giro = account_id(s, "Bargeld"), account_id(s, "ING Girokonto")

        def pay(day, cents, payee, transfer=None):
            txs.create_manual(
                s,
                app.settings,
                ManualEntry(cash, day, cents, payee, transfer_account_id=transfer),
            )

        for month in (6, 7, 8, 9, 10, 11):
            pay(date(2026, month, 3), -9_99, f"STREAMING {month}0815")  # numbers differ
        pay(date(2025, 11, 20), -120_00, "Verein e.V.")
        pay(date(2026, 11, 18), -120_00, "Verein e.V.")
        pay(date(2026, 4, 2), -45_10, "Baumarkt")
        pay(date(2026, 10, 2), -48_90, "Baumarkt")  # half a year apart, by chance
        for month in (6, 7, 8, 9, 10):
            pay(date(2026, month, 1), -50_00, "Top-up", transfer=giro)  # own budget account
        for month in (1, 2, 3, 4):
            pay(date(2026, month, 10), -15_00, "Old Gym")  # cancelled in April
    return app


def test_recurring_costs(payments) -> None:
    from hiro_budget.services import recurring

    with payments.session() as s:
        data = recurring.recurring_costs(s, date(2026, 11, 20))
    by_payee = {r.payee: r for r in data.items}
    stream = next(r for r in data.items if r.payee.startswith("STREAMING"))
    assert (stream.rhythm, stream.amount_cents, stream.count) == ("monthly", 9_99, 6)
    assert stream.monthly_cents == 9_99
    club = by_payee["Verein e.V."]
    assert club.rhythm == "yearly" and club.monthly_cents == 10_00
    assert "Baumarkt" not in by_payee  # two payments, amounts 8 % apart: no subscription
    assert "Top-up" not in by_payee  # money moved to another own account
    assert [r.payee for r in data.ended] == ["Old Gym"]
    assert data.monthly_cents == sum(r.monthly_cents for r in data.items)


def test_payee_key_ignores_numbers() -> None:
    from hiro_budget.services.recurring import payee_key

    assert payee_key("NETFLIX.COM 866-579") == payee_key("Netflix.com 1234") == "netflix com"


def test_recurring_page(qtbot, payments) -> None:
    from hiro_budget.gui.report_pages import RecurringReport

    page = RecurringReport(payments)
    qtbot.addWidget(page)
    page.refresh(date(2026, 1, 1), date(2026, 10, 20), None)
    payees = [page.table.item(r, 0).text() for r in range(page.table.rowCount())]
    assert "Old Gym" in payees  # ended ones are listed (in grey)
    # up to 20.10.2026 only the streaming subscription runs (the club fee is yearly)
    assert page.table.item(page.table.rowCount() - 1, 4).text().startswith("9,99")
    # the donut: the subscription, the variable costs; the slice's colour as a dot
    (series,) = page.view.chart().series()
    assert [s.value() for s in series.slices()] == [9.99, page.variable.monthly_cents / 100]
    assert (series.pieStartAngle(), series.pieEndAngle()) == (0, -360)  # counter-clockwise
    assert not page.table.item(0, 1).icon().isNull()
    assert page._center[0].text().startswith("9,99")
    legend = [label.text() for _, label, _, _ in page._legend]
    assert len(legend) == 2 and legend[1].startswith("Variable costs")


def test_variable_costs(payments) -> None:
    from hiro_budget.services import recurring

    until = date(2026, 10, 20)  # the last full month is September
    with payments.session() as s:
        costs = recurring.recurring_costs(s, until)
        data = recurring.variable_costs(s, costs, until)
    # 10/2025-09/2026, but the first expense is in 11/2025: 11 months
    assert (data.first_month, data.last_month, data.months) == ("2025-11", "2026-09", 11)
    # streaming 4 x 9.99, club 120, DIY store 45.10, old gym 4 x 15; no top-ups (transfers)
    assert data.expense_cents == 4 * 9_99 + 120_00 + 45_10 + 4 * 15_00
    assert data.fixed_cents == 4 * 9_99 + 4 * 15_00  # streaming and the ended gym
    assert data.monthly_cents == round((120_00 + 45_10) / 11)


def test_recurring_segments(payments) -> None:
    from types import SimpleNamespace

    from hiro_budget.gui.report_pages import RecurringReport

    def item(payee, category, group, cents):
        return SimpleNamespace(payee=payee, category=category, group_id=group, monthly_cents=cents)

    page = RecurringReport(payments)
    colors = {1: "#E4B9B4", 2: "#ADC4C3"}
    variable = SimpleNamespace(monthly_cents=500_00, first_month="2025-10", last_month="2026-09")

    # up to 7 payments: one slice each, by group, the variable costs last
    few = SimpleNamespace(
        items=[item("Bank", "Rent", 1, 900_00), item("Club", "Gym", 2, 40_00)]
        + [item("Power", "Energy", 1, 60_00)]
    )
    segments = page._segments(few, variable, colors)
    assert [(s.name, s.cents) for s in segments] == [
        ("Bank", 900_00),
        ("Power", 60_00),
        ("Club", 40_00),
        ("Variable costs", 500_00),
    ]
    bank, power, club = (s.brush.color() for s in segments[:3])
    assert bank.name() == "#e4b9b4" and power.hue() == bank.hue() and power != bank
    assert club.name() == "#adc4c3"
    assert segments[-1].brush.style() == Qt.BrushStyle.BDiagPattern

    # more payments: per category; more than 7 categories: the 6 largest and "Other"
    many = SimpleNamespace(
        items=[item(f"P{i}", f"C{i}", 1, (10 - i) * 100) for i in range(9)]
        + [item("P0b", "C0", 1, 50)]
    )
    segments = page._segments(many, None, colors)
    assert [s.name for s in segments] == [f"C{i}" for i in range(6)] + ["Other (3)"]
    assert segments[0].cents == 10_00 + 50 and len(segments[0].items) == 2
    assert segments[-1].lines == ["C6", "C7", "C8"]
    assert len(page._segments(many, variable, colors)) == 8


def test_category_slices(qtbot, payments) -> None:
    from hiro_budget.gui.reports_view import ReportsView
    from hiro_budget.services.reports import CategoryTotal

    view = ReportsView(payments)
    qtbot.addWidget(view)
    colors = {1: "#E4B9B4", 2: "#ADC4C3"}

    def cats(n):
        return [
            CategoryTotal(i, f"c{i}", "G", f"C{i}", (20 - i) * 100, group_id=1 + i % 2)
            for i in range(n)
        ]

    # up to 8 categories: all, by group (the larger first), in shades of the group colour
    slices = view._category_slices(cats(3), colors)
    assert [name for name, _, _ in slices] == ["C0", "C2", "C1"]
    assert slices[0][2].color().name() == "#e4b9b4" and slices[2][2].color().name() == "#adc4c3"
    assert len(view._category_slices(cats(8), colors)) == 8
    # more: the 7 largest and "Others"
    slices = view._category_slices(cats(10), colors)
    assert len(slices) == 8 and slices[-1][0] == "Others (3)"
    assert [c.category_name for c in slices[-1][1]] == ["C7", "C8", "C9"]


# --- mortgage --------------------------------------------------------------------------


def test_mortgage_progress(imported) -> None:
    from hiro_budget.services import mortgage_report

    with imported.session() as s:
        loan = account_id(s, "ING Baufinanzierung")
        txs.create_manual(  # a special repayment from the cash account
            s,
            imported.settings,
            ManualEntry(
                account_id(s, "Bargeld"),
                date(2026, 4, 5),
                -50_00,
                "Sondertilgung",
                transfer_account_id=loan,
            ),
        )
        data = mortgage_report.mortgage_progress(
            s, imported.settings, date(2026, 1, 1), date(2026, 4, 30)
        )
    assert data.loans == ["ING Baufinanzierung"]
    march = next(m for m in data.months if m.month == "2026-03")
    # the statement's instalment: ZINSEN 600,00 TILGUNG 400,00 (mortgage rule)
    assert (march.interest, march.principal, march.extra) == (600_00, 400_00, 0)
    april = next(m for m in data.months if m.month == "2026-04")
    assert april.extra == 50_00
    year = dict(data.years())[2026]
    assert (year.interest, year.principal, year.extra) == (600_00, 400_00, 50_00)
    assert year.total == 1_050_00


def test_mortgage_page(qtbot, imported) -> None:
    from hiro_budget.gui.report_pages import MortgageReport

    page = MortgageReport(imported)
    qtbot.addWidget(page)
    page.refresh(date(2026, 1, 1), date(2026, 4, 30), None)
    assert page.table.rowCount() == 2  # 2026 and the total
    assert page.table.item(0, 1).text().startswith("600,00")
    assert page.view.chart().series()
    page.refresh(date(2020, 1, 1), date(2020, 2, 1), None)
    assert page.table.rowCount() == 0 and page.note.text()


# --- depot -----------------------------------------------------------------------------


@pytest.fixture
def depot_app(config_dir, tmp_path):
    """Two depot statements (30.09. and 31.12.2026) with a purchase of 1.000 in between."""
    from hiro_budget.app import App
    from tests.conftest import write_statement_pdf
    from tests.fixtures import ing_securities as fx
    from tests.test_securities import SECURITIES_CONFIG

    (config_dir / "config.yaml").write_text(SECURITIES_CONFIG, encoding="utf-8")
    app = App.from_config_dir(config_dir)
    pages = [fx.DEPOT_PAGE, fx.DEPOT_PAGE.replace("per 30.09.2026", "per 31.12.2026")]
    with app.session() as s:
        for i, page in enumerate(pages):
            pdf = write_statement_pdf(tmp_path / f"Direkt_Depot_{i}.pdf", [page])
            import_service.import_file(s, app.settings, pdf)
        s.add(
            Transaction(
                account_id=account_id(s, "ING Direkt-Depot"),
                booking_date=date(2026, 11, 3),
                amount_cents=1_000_00,
                booking_type="Umbuchung",
                dedupe_key=new_manual_key(),
            )
        )
    return app


def test_depot_history(depot_app) -> None:
    from hiro_budget.services import depot_report
    from tests.fixtures import ing_securities as fx

    with depot_app.session() as s:
        depot = account_id(s, "ING Direkt-Depot")
        data = depot_report.depot_history(s, depot, date(2026, 1, 1), date(2026, 12, 31))
        later = depot_report.depot_history(s, depot, date(2026, 10, 1), date(2026, 12, 31))
    first, second = data.statements
    assert (first.day, second.day) == (date(2026, 9, 30), date(2026, 12, 31))
    assert first.invested == first.value == fx.DEPOT_TOTAL_CENTS
    assert second.invested == fx.DEPOT_TOTAL_CENTS + 1_000_00  # the purchase in between
    assert second.gain == -1_000_00  # same prices, 1.000 more invested
    assert sum(first.securities.values()) == first.value
    assert len(data.names()) == 2
    assert later.statements[0].invested == later.statements[0].value  # starts again


def test_depot_page(qtbot, depot_app) -> None:
    from hiro_budget.gui.report_pages import DepotReport

    page = DepotReport(depot_app)
    qtbot.addWidget(page)
    page.refresh(date(2026, 1, 1), date(2026, 12, 31), None)
    assert page.table.columnCount() == 3  # security and two statements
    assert page.depot.isHidden()  # only one depot: nothing to choose
    gain = page.table.item(page.table.rowCount() - 1, 2).text()
    assert gain.startswith("-1.000,00")
    page.refresh(date(2020, 1, 1), date(2020, 12, 31), None)
    assert page.table.rowCount() == 0 and page.note.text()


# --- top payees ------------------------------------------------------------------------


def test_top_payees(imported) -> None:
    from hiro_budget.services import reports

    with imported.session() as s:
        cash = account_id(s, "Bargeld")
        for day, cents, payee in (
            (date(2026, 3, 20), -12_00, "Kiosk 0815"),
            (date(2026, 3, 25), -8_00, "KIOSK 4711"),  # same payee, other numbers
            (date(2026, 3, 26), 5_00, "Kiosk"),  # money back: counts as income, not here
        ):
            txs.create_manual(s, imported.settings, ManualEntry(cash, day, cents, payee))
        payees = reports.top_payees(s, date(2026, 3, 1), date(2026, 3, 31))
        cats = reports.category_totals(s, date(2026, 3, 1), date(2026, 3, 31))
    kiosk = next(p for p in payees if p.key == "kiosk")
    assert (kiosk.expense_cents, kiosk.count, kiosk.average_cents) == (20_00, 2, 10_00)
    assert kiosk.name in ("Kiosk 0815", "KIOSK 4711")
    assert [p.expense_cents for p in payees] == sorted(
        (p.expense_cents for p in payees), reverse=True
    )
    assert sum(p.expense_cents for p in payees) <= sum(c.expense_cents for c in cats)


def test_payee_double_click_shows_its_bookings(qtbot, imported) -> None:
    from hiro_budget.gui.main_window import MainWindow
    from hiro_budget.gui.report_pages import TopPayeesReport

    window = MainWindow(imported)
    qtbot.addWidget(window)
    page = next(p for p in window.reports.pages if isinstance(p, TopPayeesReport))
    page.refresh(date(2026, 3, 1), date(2026, 3, 31), None)
    assert page.table.rowCount() == len(page.data) > 0
    page._open(0, 0)
    view = window.transactions
    assert window.tabs.currentWidget() is view
    assert view.search.text() == page.data[0].name
    assert view.current_filter().date_from == date(2026, 3, 1)
