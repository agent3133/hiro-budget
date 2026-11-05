"""The change journal and undo (#86): every write can be undone to the byte."""

from __future__ import annotations

import datetime as dt
from datetime import date

import pytest
from sqlalchemy import select

from hiro_budget import ops
from hiro_budget.db.models import Account, Category, ChangeLog, Split, SplitSource
from hiro_budget.ops import OpContext, OpError, journal
from hiro_budget.services import config_edit
from hiro_budget.services import transactions as txs
from hiro_budget.services.transactions import ManualEntry


@pytest.fixture
def ctx(app) -> OpContext:
    return OpContext(app, source="test")


def call(ctx, op_name, /, **args):
    return ops.call(ctx, op_name, args)


def booking(ctx, cents=-2000, payee="Shop", category=None, day=date(2026, 3, 5), account="Bargeld"):
    with ctx.session() as s:
        acc = s.scalar(select(Account.id).where(Account.name == account))
        cat = s.scalar(select(Category.id).where(Category.key == category)) if category else None
        entry = ManualEntry(acc, day, cents, payee, category_id=cat)
        return txs.create_manual(s, ctx.settings, entry)


@pytest.fixture
def world(ctx) -> dict[str, int]:
    """Some history, made directly through services (not journaled)."""
    ids = {
        "pay": booking(ctx, 300000, "Employer", "einkommen.gehalt", date(2026, 3, 1)),
        "food": booking(ctx, -4500, "Rewe", "lebensmittel.supermarkt", date(2026, 3, 4)),
        "open": booking(ctx, -1300, "Kiosk", None, date(2026, 3, 6)),
        "big": booking(ctx, -8000, "Baumarkt", None, date(2026, 3, 8)),
    }
    ids["over"] = booking(ctx, -30000, "Metro", "lebensmittel.supermarkt", date(2026, 3, 12))
    ids["ml"] = booking(ctx, -900, "Corner Shop", "lebensmittel.supermarkt", date(2026, 3, 13))
    ids["stale"] = booking(ctx, -3500, "Vodafone GmbH", None, date(2026, 3, 14))
    with ctx.session() as s:
        giro = s.scalar(select(Account.id).where(Account.name == "ING Girokonto"))
        txs.mark_transfer(s, ids["big"], giro)
        # a suggestion of the classifier, and a categorization by a rule that no longer fits
        ml = s.scalars(select(Split).where(Split.transaction_id == ids["ml"])).one()
        ml.source = SplitSource.ML
        stale = s.scalars(select(Split).where(Split.transaction_id == ids["stale"])).one()
        stale.category_id = s.scalar(select(Category.id).where(Category.key == "freizeit.kleidung"))
        stale.source = SplitSource.RULE
    call(ctx, "assign_budget", category="lebensmittel.supermarkt", amount="200", month="2026-03")
    return ids


def state(ctx):
    snap = journal.snapshot(ctx)
    return snap.rows, snap.files


# Each case changes something; undone, everything must be as before.
CASES = {
    "categorize": lambda c, w: call(
        c, "categorize_bookings", ids=[w["open"]], category="Supermarkt"
    ),
    "split": lambda c, w: call(
        c,
        "split_booking",
        id=w["open"],
        part=["lebensmittel.supermarkt=-10", "lebensmittel.drogerie=-3"],
    ),
    "transfer": lambda c, w: call(c, "mark_transfer", id=w["open"], account="ING Extra-Konto"),
    "reset": lambda c, w: call(c, "reset_bookings", ids=[w["food"]]),
    "accept": lambda c, w: call(c, "accept_suggestions", ids=[w["ml"]]),
    "note": lambda c, w: call(c, "set_booking_note", id=w["food"], text="n"),
    "add": lambda c, w: call(
        c,
        "create_manual_booking",
        account="Bargeld",
        booking_date="2026-03-10",
        amount="-5",
        payee="X",
    ),
    "edit": lambda c, w: call(c, "update_manual_booking", id=w["food"], amount="-46,00"),
    "delete": lambda c, w: call(c, "delete_manual_booking", id=w["food"]),
    "delete_transfer": lambda c, w: call(c, "delete_manual_booking", id=w["big"]),
    "assign": lambda c, w: call(
        c, "assign_budget", category="Drogerie", amount="30", month="2026-03"
    ),
    "assign_zero": lambda c, w: call(
        c, "assign_budget", category="Supermarkt", amount="0", month="2026-03"
    ),
    "move": lambda c, w: call(
        c, "move_money", source="Supermarkt", target="Drogerie", amount="50", month="2026-03"
    ),
    "targets": lambda c, w: (
        call(c, "set_category_target", category="Supermarkt", type="monthly", amount=300),
        call(c, "assign_to_targets", month="2026-03"),
    ),
    "cover": lambda c, w: call(c, "cover_overspending", month="2026-03"),
    "copy": lambda c, w: call(c, "copy_last_month", month="2026-04"),
    "create_category": lambda c, w: call(c, "create_category", group="lebensmittel", name="Eis"),
    "rename_key": lambda c, w: call(
        c, "rename_category", category="lebensmittel.supermarkt", name="Markt", key="markt"
    ),
    "hide_category": lambda c, w: call(c, "hide_category", category="freizeit.kleidung"),
    "create_group": lambda c, w: call(c, "create_group", name="Tiere"),
    "rename_group": lambda c, w: call(
        c, "rename_group", group="lebensmittel", name="Essen", key="essen"
    ),
    "add_rule": lambda c, w: call(
        c, "add_rule", counterparty="(?i)kiosk", category="freizeit.sport_hobby", apply=True
    ),
    "recategorize": lambda c, w: call(c, "recategorize"),
}


@pytest.mark.parametrize("case", CASES)
def test_undo_restores_everything(ctx, world, case):
    before = state(ctx)
    CASES[case](ctx, world)
    changed = state(ctx) != before
    last = call(ctx, "list_changes", limit=1).changes[0]
    if not changed:
        pytest.skip("this change was a no-op on the fixture")
    assert last.items > 0 and last.undoable
    count = len(call(ctx, "list_changes", limit=50).changes)
    for _ in range(count):  # a case may have made two entries
        if state(ctx) == before:
            break
        call(ctx, "undo_change", last=True)
    assert state(ctx) == before


def test_rule_edit_and_delete_are_undone(ctx, world):
    call(ctx, "add_rule", counterparty="(?i)kiosk", category="freizeit.sport_hobby", id="kiosk")
    after_add = state(ctx)
    call(ctx, "update_rule", id="kiosk", purpose="(?i)x")
    call(ctx, "delete_rule", id="kiosk")
    call(ctx, "undo_change", last=True)  # the delete
    call(ctx, "undo_change", last=True)  # the update
    assert state(ctx) == after_add


def test_transfer_mirror_comes_back(ctx, world):
    mirror_before = call(ctx, "list_bookings", account="ING Girokonto").total
    assert mirror_before == 1
    call(ctx, "delete_manual_booking", id=world["big"])
    assert call(ctx, "list_bookings", account="ING Girokonto").total == 0
    call(ctx, "undo_change", last=True)
    assert call(ctx, "list_bookings", account="ING Girokonto").total == 1
    got = call(ctx, "get_booking", id=world["big"])
    assert got.counterparts  # linked again


def test_conflict_refuses_the_whole_undo(ctx, world):
    call(ctx, "categorize_bookings", ids=[world["open"]], category="Supermarkt")
    first = call(ctx, "list_changes", limit=1).changes[0].id
    # someone changes the booking's category afterwards (outside the journal)
    with ctx.session() as s:
        drogerie = s.scalar(select(Category.id).where(Category.key == "lebensmittel.drogerie"))
        txs.set_category(s, world["open"], drogerie)
    snapshot = state(ctx)
    with pytest.raises(OpError, match=f"change {first}.*changed since: split"):
        call(ctx, "undo_change", id=first)
    assert state(ctx) == snapshot
    log = call(ctx, "list_changes", limit=1).changes[0]
    assert not log.undone


def test_undo_in_the_wrong_order_conflicts(ctx, world):
    call(ctx, "set_booking_note", id=world["food"], text="one")
    first = call(ctx, "list_changes", limit=1).changes[0].id
    call(ctx, "set_booking_note", id=world["food"], text="two")
    with pytest.raises(OpError, match="changed since"):
        call(ctx, "undo_change", id=first)


def test_config_file_conflict(ctx, world):
    call(ctx, "create_group", name="Tiere")
    gid = call(ctx, "list_changes", limit=1).changes[0].id
    config_edit.save_group_budget(ctx.settings, "lebensmittel", False)  # touches categories.yaml
    ctx.settings.categories_file.write_text(
        ctx.settings.categories_file.read_text(encoding="utf-8") + "\n# edited by hand\n",
        encoding="utf-8",
        newline="\n",
    )
    with pytest.raises(OpError, match="file categories.yaml"):
        call(ctx, "undo_change", id=gid)


def test_undo_a_whole_session(ctx, world):
    before = state(ctx)
    agent = OpContext(ctx.app, source="mcp")
    call(agent, "categorize_bookings", ids=[world["open"]], category="Supermarkt")
    call(agent, "set_booking_note", id=world["open"], text="agent")
    call(agent, "create_category", group="lebensmittel", name="Eis")
    call(ctx, "set_booking_note", id=world["food"], text="mine")  # another session
    mine = state(ctx)
    assert mine != before
    r = call(ctx, "undo_change", session=agent.session_id)
    assert "undone: change" in r.message and len(r.message.split(",")) == 3
    expected_notes_only = call(ctx, "get_booking", id=world["food"]).notes
    assert expected_notes_only == "mine"  # the other session's change stays
    assert call(ctx, "get_booking", id=world["open"]).notes is None
    changes = call(ctx, "list_changes", session=agent.session_id).changes
    assert [c.undone for c in changes if c.operation != "undo_change"] == [True] * 3
    with pytest.raises(OpError, match="nothing to undo in session"):
        call(ctx, "undo_change", session=agent.session_id)


def test_undone_entry_cannot_be_undone_twice(ctx, world):
    call(ctx, "set_booking_note", id=world["food"], text="x")
    first = call(ctx, "list_changes", limit=1).changes[0].id
    call(ctx, "undo_change", id=first)
    with pytest.raises(OpError, match="undone already"):
        call(ctx, "undo_change", id=first)
    with pytest.raises(OpError, match="no change 99999"):
        call(ctx, "undo_change", id=99999)


def test_import_is_journaled_but_not_undoable(ctx, statement_pdf):
    call(ctx, "import_files", paths=[str(statement_pdf)])
    entry = call(ctx, "list_changes", limit=1).changes[0]
    assert entry.operation == "import_files" and not entry.undoable and entry.items == 0
    with pytest.raises(OpError, match="cannot be undone; restore a backup"):
        call(ctx, "undo_change", id=entry.id)


def test_undo_needs_exactly_one_selector(ctx, world):
    with pytest.raises(OpError, match="exactly one"):
        call(ctx, "undo_change")
    with pytest.raises(OpError, match="exactly one"):
        call(ctx, "undo_change", id=1, last=True)


def test_reads_are_not_journaled(ctx, world):
    n = len(call(ctx, "list_changes", limit=500).changes)
    call(ctx, "list_bookings")
    call(ctx, "get_budget")
    assert len(call(ctx, "list_changes", limit=500).changes) == n


def test_journal_records_who_and_what(ctx, world):
    call(ctx, "set_booking_note", id=world["food"], text="x")
    entry = call(ctx, "list_changes", session=ctx.session_id).changes[0]  # newest first
    assert entry.source == "test" and entry.operation == "set_booking_note"
    assert "Set the note" in entry.summary and "text=x" in entry.summary
    assert render_has(ctx, "set_booking_note")


def render_has(ctx, name) -> bool:
    text = ops.find("list_changes").render(call(ctx, "list_changes"), None)
    return name.replace("_", " ") in text or "note" in text


def test_times_are_local_like_undone_at_and_the_pruning(ctx, world):
    """SQLite's now() is UTC: history showed 10:38 for a change made at 11:38 (#87)."""
    start = dt.datetime.now().replace(microsecond=0)
    call(ctx, "set_booking_note", id=world["food"], text="now")
    call(ctx, "undo_change", last=True)
    end = dt.datetime.now()
    with ctx.session() as s:
        noted = s.scalars(select(ChangeLog).where(ChangeLog.operation == "set_booking_note"))
        log = noted.one()
        assert start <= log.created_at <= end
        assert log.undone_at is not None and start <= log.undone_at <= end
    shown = call(ctx, "list_changes", limit=1).changes[0].time
    assert start <= dt.datetime.fromisoformat(shown) <= end


def test_old_entries_are_pruned(ctx, world):
    call(ctx, "set_booking_note", id=world["food"], text="old")
    with ctx.session() as s:
        for log in s.scalars(select(ChangeLog)):  # the fixture's entries too
            log.created_at = dt.datetime.now() - dt.timedelta(days=journal.KEEP_DAYS + 1)
    call(ctx, "set_booking_note", id=world["food"], text="new")
    ops_listed = [c.summary for c in call(ctx, "list_changes", limit=50).changes]
    assert len(ops_listed) == 1 and "text=new" in ops_listed[0]


def test_failed_write_that_changed_nothing_leaves_no_entry(ctx, world):
    n = len(call(ctx, "list_changes", limit=500).changes)
    with pytest.raises(OpError):
        call(ctx, "categorize_bookings", ids=[world["open"], 999999], category="Supermarkt")
    assert len(call(ctx, "list_changes", limit=500).changes) == n


def test_undoable_flags_in_the_registry():
    flags = {o.name: o.undoable for o in ops.registry()}
    assert flags["categorize_bookings"] and flags["assign_budget"] and flags["recategorize"]
    assert not flags["import_files"] and not flags["train"] and not flags["undo_change"]
    assert not flags["list_bookings"]  # reads: nothing to undo
