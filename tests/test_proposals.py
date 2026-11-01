"""Budget proposals from spending history."""

from datetime import date

from sqlalchemy import select

from hiro_budget.db.models import Account, Category
from hiro_budget.services import budget, proposals
from hiro_budget.services import transactions as txs
from hiro_budget.services.budget import Envelope
from hiro_budget.services.transactions import ManualEntry


def _setup_history(app, s) -> dict[str, int]:
    cash = s.scalar(select(Account.id).where(Account.name == "Bargeld"))
    cats = {k: s.scalar(select(Category.id).where(Category.key == k)) for k in (
        "lebensmittel.supermarkt", "versicherungen.haftpflicht_hausrat", "einkommen.gehalt",
        "freizeit.restaurants",
    )}  # fmt: skip

    def book(day: date, cents: int, key: str) -> None:
        txs.create_manual(
            s, app.settings, ManualEntry(cash, day, cents, "x", category_id=cats[key])
        )

    groceries = [400, 420, 380, 1000, 410, 390]  # one outlier month (Juni)
    for i, euros in enumerate(groceries):
        book(date(2026, 3 + i, 10), -euros * 100, "lebensmittel.supermarkt")
    book(date(2026, 4, 1), -480_00, "versicherungen.haftpflicht_hausrat")  # once a year
    book(date(2026, 5, 2), -60_00, "freizeit.restaurants")
    book(date(2026, 5, 20), 15_00, "freizeit.restaurants")  # refund
    book(date(2026, 3, 25), 3000_00, "einkommen.gehalt")  # income is never proposed
    return cats


def test_round_up() -> None:
    assert proposals.round_up(412_37, 10) == 420_00
    assert proposals.round_up(400_00, 10) == 400_00
    assert proposals.round_up(1, 5) == 5_00
    assert proposals.round_up(412_37, 0) == 412_37
    assert proposals.round_up(-50, 10) == 0


def test_regular_and_irregular_categories(app) -> None:
    with app.session() as s:
        cats = _setup_history(app, s)
        r = proposals.propose(s, app.settings, "2026-09")
    assert (r.history_from, r.history_to, r.history_months, r.months) == (
        "2026-03",
        "2026-08",
        6,
        6,
    )
    by_id = {p.category_id: p for p in r.proposals}

    groceries = by_id[cats["lebensmittel.supermarkt"]]
    assert groceries.regular and groceries.months_with_spending == 6
    assert groceries.median == 405_00  # outlier of 1000 has no effect
    assert groceries.proposal == 410_00  # rounded up to 10 EUR
    assert (groceries.minimum, groceries.maximum) == (380_00, 1000_00)

    insurance = by_id[cats["versicherungen.haftpflicht_hausrat"]]
    assert not insurance.regular
    assert insurance.proposal == 80_00  # 480 over 6 months of history

    fun = by_id[cats["freizeit.restaurants"]]
    assert not fun.regular and fun.proposal == 10_00  # (60 - 15) / 6 = 7.50 -> 10

    assert cats["einkommen.gehalt"] not in by_id


def test_fewer_months_considered(app) -> None:
    with app.session() as s:
        cats = _setup_history(app, s)
        r = proposals.propose(s, app.settings, "2026-09", months=3, round_to=0)
    groceries = next(p for p in r.proposals if p.category_id == cats["lebensmittel.supermarkt"])
    assert r.months == 3
    assert groceries.spending == [1000_00, 410_00, 390_00]
    assert groceries.proposal == 410_00  # median of the last 3 months


def test_no_history(app) -> None:
    with app.session() as s:
        r = proposals.propose(s, app.settings, "2026-03")
    assert r.history_months == 0 and r.proposals == []


def test_apply_proposals(app) -> None:
    with app.session() as s:
        cats = _setup_history(app, s)
        r = proposals.propose(s, app.settings, "2026-09")
        chosen = {p.category_id: p.proposal for p in r.proposals if p.proposal}
        assert proposals.apply_proposals(s, "2026-09", chosen) == len(chosen)
        b = budget.compute_month(s, app.settings, "2026-09")
        assert b.category(cats["lebensmittel.supermarkt"]).assigned == 410_00
        # Assigned amounts show up in the next proposal as current values.
        again = proposals.propose(s, app.settings, "2026-09")
        assert (
            next(
                p for p in again.proposals if p.category_id == cats["lebensmittel.supermarkt"]
            ).assigned
            == 410_00
        )


def test_proposal_dialog(qtbot, app) -> None:
    from hiro_budget.gui.proposal_dialog import COL_KIND, COL_MAX, COL_MEDIAN, ProposalDialog

    with app.session() as s:
        cats = _setup_history(app, s)
        rta = budget.compute_month(s, app.settings, "2026-09").ready_to_assign
    dlg = ProposalDialog(app, "2026-09", rta)
    qtbot.addWidget(dlg)
    assert "6" in dlg.history.text()
    chosen = dlg.selected()
    assert chosen[Envelope.of(cats["lebensmittel.supermarkt"])] == 410_00
    assert chosen[Envelope.of(cats["versicherungen.haftpflicht_hausrat"])] == 80_00
    dlg._select(lambda p: p.category_id == cats["lebensmittel.supermarkt"])
    assert list(dlg.selected()) == [Envelope.of(cats["lebensmittel.supermarkt"])]
    # clicking an amount takes it over as the proposal and selects the row
    row = next(
        i
        for i, p in enumerate(dlg.result_data.proposals)
        if p.category_id == cats["lebensmittel.supermarkt"]
    )
    p = dlg.result_data.proposals[row]
    dlg.take_over(row, COL_MAX)
    assert dlg.selected()[p.envelope] == p.maximum
    dlg.take_over(row, COL_MEDIAN)
    assert dlg.selected()[p.envelope] == p.median
    dlg.take_over(row, COL_KIND)  # not an amount: nothing happens
    assert dlg.selected()[p.envelope] == p.median
    dlg._select(lambda p: p.category_id == cats["lebensmittel.supermarkt"])
    dlg.months.setValue(3)  # reload with a shorter period
    assert dlg.result_data.months == 3
    dlg._apply()
    assert dlg.applied > 0
    with app.session() as s:
        assert budget.get_assigned(s, "2026-09", cats["lebensmittel.supermarkt"]) == 410_00
