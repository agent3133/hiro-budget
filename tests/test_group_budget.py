"""Budgeting a whole category group as one envelope (``budget: group``)."""

from datetime import date

import pytest
from PySide6.QtWidgets import QMessageBox
from sqlalchemy import select

from hiro_budget.config import CategoriesConfig, TargetDef, load_settings
from hiro_budget.db.models import BudgetAssignment, CategoryGroup
from hiro_budget.services import budget, config_edit, proposals
from hiro_budget.services.budget import Envelope
from tests.test_budget import APRIL, MARCH, MAY, cash_booking, cat, check_invariant


def group_id(s, key: str) -> int:
    return s.scalar(select(CategoryGroup.id).where(CategoryGroup.key == key))


def saved_group(app, key: str = "lebensmittel"):
    """Group definition as written to categories.yaml."""
    groups = load_settings(app.settings.config_dir).categories.groups
    return next(g for g in groups if g.key == key)


def by_group(app, key: str = "lebensmittel", on: bool = True) -> None:
    config_edit.save_group_budget(app.settings, key, on)
    app.sync_config()


def test_config_validation() -> None:
    def groups(**extra):
        return {"groups": [{"key": "g", "name": "G", "categories": [], **extra}]}

    assert CategoriesConfig.model_validate(groups(budget="group")).groups[0].budget_by_group
    with pytest.raises(ValueError, match="income groups"):
        CategoriesConfig.model_validate(groups(income=True, budget="group"))
    with pytest.raises(ValueError, match="needs 'budget: group'"):
        CategoriesConfig.model_validate(groups(target={"type": "monthly", "amount": 5}))


def test_group_shares_one_envelope(app) -> None:
    by_group(app)
    with app.session() as s:
        gid = group_id(s, "lebensmittel")
        budget.set_assigned(s, MARCH, Envelope("group", gid), 50_00)
        cash_booking(app, s, date(2026, 3, 5), -30_00, "lebensmittel.supermarkt")
        cash_booking(app, s, date(2026, 3, 6), -15_00, "lebensmittel.baeckerei")
        b = check_invariant(s, app.settings, MARCH)
        g = b.group(gid)
        assert g.budget_by_group
        assert (g.budget.assigned, g.budget.activity, g.budget.available) == (
            50_00,
            -45_00,
            5_00,
        )
        assert (g.assigned, g.activity, g.available) == (50_00, -45_00, 5_00)
        supermarkt = b.category(cat(s, "lebensmittel.supermarkt"))
        assert supermarkt.in_group_budget and supermarkt.activity == -30_00
        assert supermarkt.available == 0 and supermarkt.assigned == 0
        assert b.envelope(Envelope("group", gid)) is g.budget
        assert supermarkt not in b.envelopes

        # Leftover carries over to April; overspending in May is taken from June's RTA.
        cash_booking(app, s, date(2026, 5, 3), -20_00, "lebensmittel.drogerie")
        april = check_invariant(s, app.settings, APRIL)
        assert april.group(gid).budget.carry == 5_00
        may = check_invariant(s, app.settings, MAY)
        assert may.group(gid).available == -15_00
        june = check_invariant(s, app.settings, "2026-06")
        assert june.overspent_last_month == -15_00


def test_switching_keeps_ready_to_assign(app) -> None:
    with app.session() as s:
        sm, bk = cat(s, "lebensmittel.supermarkt"), cat(s, "lebensmittel.baeckerei")
        budget.set_assigned(s, MARCH, sm, 40_00)
        budget.set_assigned(s, MARCH, bk, 10_00)
        budget.set_assigned(s, APRIL, bk, 5_00)
        cash_booking(app, s, date(2026, 3, 5), -45_00, "lebensmittel.baeckerei")
        before = budget.compute_month(s, app.settings, APRIL).ready_to_assign
    by_group(app)
    with app.session() as s:
        gid = group_id(s, "lebensmittel")
        rows = s.scalars(select(BudgetAssignment).order_by(BudgetAssignment.month)).all()
        assert [(r.month, r.group_id, r.category_id, r.amount_cents) for r in rows] == [
            (MARCH, gid, None, 50_00),
            (APRIL, gid, None, 5_00),
        ]
        # Bakery was overspent by 35 before; in the shared envelope it is covered.
        b = check_invariant(s, app.settings, APRIL)
        assert b.ready_to_assign == before + 35_00
        assert b.group(gid).budget.available == 10_00
    by_group(app, on=False)
    with app.session() as s:
        rows = s.scalars(select(BudgetAssignment).order_by(BudgetAssignment.month)).all()
        assert [(r.month, r.category_id, r.amount_cents) for r in rows] == [
            (MARCH, sm, 50_00),
            (APRIL, sm, 5_00),
        ]
        check_invariant(s, app.settings, APRIL)
    assert not saved_group(app).budget_by_group


def test_group_target_and_operations(app) -> None:
    by_group(app)
    config_edit.save_group_target(
        app.settings, "lebensmittel", TargetDef(type="monthly", amount=400)
    )
    assert saved_group(app).target.amount == 400
    with app.session() as s:
        gid = group_id(s, "lebensmittel")
        env = Envelope("group", gid)
        b = budget.compute_month(s, app.settings, MARCH)
        assert b.group(gid).budget.needed == 400_00
        assert b.group(gid).budget in [e for e in b.envelopes if e.underfunded]
        r = budget.assign_to_targets(s, app.settings, MARCH)
        assert r.amount >= 100_00  # cash 100 is all there is
        assert budget.get_assigned(s, MARCH, env) == min(400_00, r.amount)

        fun = cat(s, "freizeit.restaurants")
        budget.move_money(s, MARCH, env, fun, 10_00)
        assert budget.get_assigned(s, MARCH, fun) == 10_00
        assert budget.copy_last_month(s, app.settings, APRIL) == 2
        assert budget.get_assigned(s, APRIL, env) == budget.get_assigned(s, MARCH, env)
        check_invariant(s, app.settings, APRIL)

    # Switching back removes the group target.
    by_group(app, on=False)
    assert saved_group(app).target is None


def test_group_target_requires_group_budget(app) -> None:
    with pytest.raises(Exception, match="not budgeted as a whole"):
        config_edit.save_group_target(
            app.settings, "lebensmittel", TargetDef(type="monthly", amount=1)
        )


def test_save_categories_keeps_group_budget(app) -> None:
    by_group(app)
    config_edit.save_group_target(app.settings, "lebensmittel", TargetDef(type="monthly", amount=9))
    config_edit.save_categories(app.settings, app.settings.categories)
    g = saved_group(app)
    assert g.budget_by_group and g.target.amount == 9


def test_proposal_for_group(app) -> None:
    by_group(app)
    with app.session() as s:
        for month in (3, 4, 5):
            cash_booking(app, s, date(2026, month, 5), -20_00, "lebensmittel.supermarkt")
            cash_booking(app, s, date(2026, month, 6), -5_00, "lebensmittel.baeckerei")
        r = proposals.propose(s, app.settings, "2026-06", round_to=0)
        gid = group_id(s, "lebensmittel")
        lines = [p for p in r.proposals if p.group_id == gid]
        assert len(lines) == 1 and lines[0].is_group
        assert lines[0].regular and lines[0].proposal == 25_00
        proposals.apply_proposals(s, "2026-06", {lines[0].envelope: 30_00})
        assert budget.get_assigned(s, "2026-06", Envelope("group", gid)) == 30_00


def test_budget_view_group_rows(qtbot, app, monkeypatch) -> None:
    from hiro_budget.gui.budget_view import COL_NAME, ENV_ROLE, GROUP_ROLE, BudgetView
    from hiro_budget.gui.move_money_dialog import MoveMoneyDialog

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    view = BudgetView(app)
    qtbot.addWidget(view)
    view.hide_empty.setChecked(False)  # no bookings in this test
    view.go(MARCH)
    view.set_group_budget("lebensmittel", True)
    with app.session() as s:
        gid = group_id(s, "lebensmittel")
    env = Envelope("group", gid)
    item = next(
        view.tree.topLevelItem(i)
        for i in range(view.tree.topLevelItemCount())
        if view.tree.topLevelItem(i).data(COL_NAME, GROUP_ROLE) == "lebensmittel"
    )
    assert item.data(COL_NAME, ENV_ROLE) == env
    assert all(item.child(j).data(COL_NAME, ENV_ROLE) is None for j in range(item.childCount()))
    view.set_assigned(env, 70_00)
    assert view.envelope(env).assigned == 70_00

    dlg = MoveMoneyDialog(view.data, view, from_id=env, cents=20_00)
    assert dlg.source.currentData() == env
    dlg._accept()
    assert dlg.from_id == env and dlg.to_id is None

    view.set_group_budget("lebensmittel", False)
    assert view.envelope(env) is None
    with app.session() as s:
        assert budget.get_assigned(s, MARCH, cat(s, "lebensmittel.supermarkt")) == 70_00
