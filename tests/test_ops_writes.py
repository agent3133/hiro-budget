"""The write operations (#83): each one through ``ops.call``, checked with a read operation."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from hiro_budget import ops
from hiro_budget.db.models import Account, Category
from hiro_budget.ops import OpContext, OpError
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
        return txs.create_manual(
            s, ctx.settings, ManualEntry(acc, day, cents, payee, category_id=cat)
        )


def category_of(ctx, tx_id) -> str:
    return call(ctx, "get_booking", id=tx_id).splits[0].category or ""


# --- bookings -------------------------------------------------------------------------


def test_categorize_several_bookings(ctx):
    a, b = booking(ctx), booking(ctx, payee="Other")
    r = call(ctx, "categorize_bookings", ids=[a, b], category="Supermarkt")
    assert r.ids == [a, b] and "lebensmittel.supermarkt" in r.message
    assert category_of(ctx, a) == category_of(ctx, b) == "lebensmittel.supermarkt"
    assert "->" in ops.find("categorize_bookings").render(r, None)


def test_categorize_is_all_or_nothing(ctx):
    a = booking(ctx)
    with pytest.raises(OpError, match="no booking 999999"):
        call(ctx, "categorize_bookings", ids=[a, 999999], category="lebensmittel.supermarkt")
    assert category_of(ctx, a) != "lebensmittel.supermarkt"
    with pytest.raises(OpError, match="unknown category"):
        call(ctx, "categorize_bookings", ids=[a], category="nope")


def test_split_booking(ctx):
    a = booking(ctx, cents=-5000)
    r = call(
        ctx,
        "split_booking",
        id=a,
        part=["lebensmittel.supermarkt=-40,00", "lebensmittel.drogerie=-10.00"],
    )
    assert len(r.details) == 2
    got = call(ctx, "get_booking", id=a)
    assert [(s.category, s.amount) for s in got.splits] == [
        ("lebensmittel.supermarkt", "-40.00"),
        ("lebensmittel.drogerie", "-10.00"),
    ]
    with pytest.raises(OpError, match="do not add up"):
        call(ctx, "split_booking", id=a, part=["lebensmittel.supermarkt=-10"])
    with pytest.raises(OpError, match="CATEGORY=AMOUNT"):
        call(ctx, "split_booking", id=a, part=["nonsense"])


def test_mark_transfer_and_counter_booking(ctx):
    a = booking(ctx, cents=-3000)
    call(ctx, "mark_transfer", id=a, account="ING Girokonto")
    got = call(ctx, "get_booking", id=a)
    assert got.splits[0].transfer_account == "ING Girokonto"
    assert got.counterparts  # the mirror in the other account
    other = call(ctx, "list_bookings", account="ING Girokonto")
    assert other.total == 1 and other.bookings[0].amount == "30.00"
    with pytest.raises(OpError, match="own account"):
        call(ctx, "mark_transfer", id=a, account="Bargeld")
    # a counter-booking cannot be categorized
    with pytest.raises(OpError, match="counter-booking"):
        call(ctx, "categorize_bookings", ids=[other.bookings[0].id], category="Supermarkt")


def test_categorizing_one_side_of_a_transfer_pair_warns(ctx):
    out = booking(ctx, cents=-15000, payee="Revolut Top-up", account="ING Girokonto")
    inn = booking(ctx, cents=15000, payee="Einzahlung", account="Bargeld")
    call(ctx, "mark_transfer", id=out, account="Bargeld")
    call(ctx, "mark_transfer", id=inn, account="ING Girokonto")
    assert not call(ctx, "get_booking", id=inn).counterpart_of  # two independent bookings

    r = call(ctx, "categorize_bookings", ids=[out], category="lebensmittel.drogerie")
    assert len(r.warnings) == 1
    assert f"booking {out} is no longer a transfer" in r.warnings[0]
    assert f"partner {inn} (Bargeld, 150,00)" in r.warnings[0]
    assert "Warning: booking" in ops.find("categorize_bookings").render(r, None)
    assert call(ctx, "get_booking", id=inn).splits[0].transfer_account == "ING Girokonto"

    # categorizing it a second time, or a booking that was no transfer, does not warn
    r = call(ctx, "categorize_bookings", ids=[out], category="lebensmittel.supermarkt")
    assert r.warnings == []
    plain = booking(ctx, cents=-500, payee="Bäcker")
    assert call(ctx, "categorize_bookings", ids=[plain], category="Supermarkt").warnings == []


def test_reset_and_accept(ctx):
    a = booking(ctx, payee="Netflix International B.V.", category="sonstiges.sonstiges")
    r = call(ctx, "reset_bookings", ids=[a])
    assert "->" in r.details[0]

    # only a classifier suggestion can be accepted (#92); all or none
    from hiro_budget.db.models import Split, SplitSource

    sure = booking(ctx, payee="Rewe", category="lebensmittel.supermarkt")  # manual
    with pytest.raises(OpError, match=rf"no suggestion to accept on booking {sure}"):
        call(ctx, "accept_suggestions", ids=[sure])
    guess = booking(ctx, payee="Reve Filiale", category="lebensmittel.supermarkt")
    with ctx.session() as s:
        for split in s.scalars(select(Split).where(Split.transaction_id == guess)):
            split.source = SplitSource.ML
    assert call(ctx, "list_bookings", category="review").total == 1
    with pytest.raises(OpError, match="no suggestion to accept"):
        call(ctx, "accept_suggestions", ids=[guess, sure])
    assert call(ctx, "list_bookings", category="review").total == 1  # nothing was changed
    r = call(ctx, "accept_suggestions", ids=[guess])
    assert r.ids == [guess] and call(ctx, "list_bookings", category="review").total == 0


def test_note(ctx):
    a = booking(ctx)
    call(ctx, "set_booking_note", id=a, text="  for the party ")
    assert call(ctx, "get_booking", id=a).notes == "for the party"
    r = call(ctx, "set_booking_note", id=a, text="")
    assert "removed" in r.message and call(ctx, "get_booking", id=a).notes is None


def test_manual_booking_create_edit_delete(ctx):
    r = call(
        ctx,
        "create_manual_booking",
        account="Bargeld",
        booking_date="2026-04-02",
        amount="-12,50",
        payee="Bakery",
        category="lebensmittel.supermarkt",
    )
    (new_id,) = r.ids
    got = call(ctx, "get_booking", id=new_id)
    assert got.amount == "-12.50" and got.manual and got.payee == "Bakery"

    call(ctx, "update_manual_booking", id=new_id, amount="-13.00", notes="n")
    got = call(ctx, "get_booking", id=new_id)
    assert got.amount == "-13.00" and got.notes == "n"
    assert got.splits[0].category == "lebensmittel.supermarkt"  # kept
    with pytest.raises(OpError, match="nothing to change"):
        call(ctx, "update_manual_booking", id=new_id)

    call(ctx, "delete_manual_booking", id=new_id)
    with pytest.raises(OpError, match="no booking"):
        call(ctx, "get_booking", id=new_id)


def test_imported_bookings_cannot_be_deleted_or_edited(ctx, statement_pdf):
    call(ctx, "import_files", paths=[str(statement_pdf)])
    imported = call(ctx, "list_bookings", account="ING Girokonto", limit=1).bookings[0]
    assert not imported.manual
    with pytest.raises(OpError, match="imported bookings cannot be edited or deleted"):
        call(ctx, "delete_manual_booking", id=imported.id)
    with pytest.raises(OpError, match="no booking"):
        call(ctx, "delete_manual_booking", id=999999)


# --- budget ---------------------------------------------------------------------------


def month_line(ctx, month, key):
    b = call(ctx, "get_budget", month=month)
    return next(c for g in b.groups for c in g.lines if c.key == key), b


def test_assign_and_relative(ctx):
    booking(ctx, cents=100000, category="einkommen.gehalt", day=date(2026, 3, 1))
    r = call(
        ctx, "assign_budget", category="lebensmittel.supermarkt", amount="250", month="2026-03"
    )
    line, b = month_line(ctx, "2026-03", "lebensmittel.supermarkt")
    assert line.assigned == "250.00" and r.ready_to_assign == b.ready_to_assign
    call(ctx, "assign_budget", category="Supermarkt", amount="-50", month="2026-03", relative=True)
    assert month_line(ctx, "2026-03", "lebensmittel.supermarkt")[0].assigned == "200.00"
    with pytest.raises(OpError, match="negative"):
        call(ctx, "assign_budget", category="Supermarkt", amount="-1", month="2026-03")


def test_move_money(ctx):
    booking(ctx, cents=100000, category="einkommen.gehalt", day=date(2026, 3, 1))
    call(ctx, "assign_budget", category="lebensmittel.supermarkt", amount="100", month="2026-03")
    r = call(
        ctx,
        "move_money",
        source="lebensmittel.supermarkt",
        target="lebensmittel.drogerie",
        amount="30",
        month="2026-03",
    )
    assert r.ready_to_assign
    assert month_line(ctx, "2026-03", "lebensmittel.supermarkt")[0].assigned == "70.00"
    assert month_line(ctx, "2026-03", "lebensmittel.drogerie")[0].assigned == "30.00"
    call(
        ctx,
        "move_money",
        source="lebensmittel.drogerie",
        target="ready",
        amount="30",
        month="2026-03",
    )
    assert month_line(ctx, "2026-03", "lebensmittel.drogerie")[0].assigned == "0.00"
    with pytest.raises(OpError, match="less than"):
        call(ctx, "move_money", source="Supermarkt", target="ready", amount="500", month="2026-03")


def test_targets_cover_copy(ctx):
    booking(ctx, cents=500000, category="einkommen.gehalt", day=date(2026, 3, 1))
    call(ctx, "set_category_target", category="lebensmittel.supermarkt", type="monthly", amount=300)
    r = call(ctx, "assign_to_targets", month="2026-03")
    assert "assigned to" in r.message
    assert month_line(ctx, "2026-03", "lebensmittel.supermarkt")[0].assigned == "300.00"
    booking(ctx, cents=-35000, category="lebensmittel.supermarkt", day=date(2026, 3, 9))
    r = call(ctx, "cover_overspending", month="2026-03")
    assert "cover" in r.message
    assert month_line(ctx, "2026-03", "lebensmittel.supermarkt")[0].available == "0.00"
    r = call(ctx, "copy_last_month", month="2026-04")
    assert "copied" in r.message
    assert month_line(ctx, "2026-04", "lebensmittel.supermarkt")[0].assigned == "350.00"


def test_negative_ready_to_assign_is_a_warning(ctx):
    booking(ctx, cents=10000, category="einkommen.gehalt", day=date(2026, 3, 1))
    r = call(ctx, "assign_budget", category="Supermarkt", amount="60", month="2026-03")
    assert r.warnings == [] and "Warning" not in ops.find("assign_budget").render(r, None)
    free = Decimal(r.ready_to_assign)
    assert free > 0
    over = free + 40  # 40 euros more than there is
    r = call(ctx, "assign_budget", category="Drogerie", amount=str(over), month="2026-03")
    assert r.ready_to_assign == "-40.00"  # the write still happens, as in the GUI
    assert len(r.warnings) == 1 and "-40.00" in r.warnings[0]
    assert "Warning: Ready to Assign is negative" in ops.find("assign_budget").render(r, None)
    # a copy that over-assigns warns as well; moving money back to Ready to Assign clears it
    r = call(ctx, "copy_last_month", month="2026-04")
    assert (
        r.warnings and month_line(ctx, "2026-04", "lebensmittel.supermarkt")[0].assigned == "60.00"
    )
    # (Ready to Assign counts every month: the copy over-assigned April on top of March)
    for month in ("2026-03", "2026-04"):
        r = call(
            ctx, "move_money", source="Drogerie", target="ready", amount=str(over), month=month
        )
    assert r.warnings == [] and r.ready_to_assign == str(free - 60)


def test_assign_to_category_of_group_budget_is_refused(ctx):
    from hiro_budget.services import config_edit

    config_edit.save_group_budget(ctx.settings, "lebensmittel", True)
    ctx.app.sync_config()
    with pytest.raises(OpError, match="budgeted as a whole"):
        call(ctx, "assign_budget", category="lebensmittel.supermarkt", amount="10", month="2026-03")
    call(ctx, "assign_budget", category="group:lebensmittel", amount="10", month="2026-03")
    with pytest.raises(OpError, match="per category"):
        call(ctx, "assign_budget", category="group:wohnen", amount="10", month="2026-03")


# --- categories -----------------------------------------------------------------------


def test_category_lifecycle_keeps_comments(ctx):
    path = ctx.settings.categories_file
    header = path.read_text(encoding="utf-8").splitlines()[0]
    r = call(ctx, "create_category", group="Lebensmittel", name="Süßes & Eis")
    assert "lebensmittel.suesses_eis" in r.message
    keys = [c.key for g in call(ctx, "list_categories").groups for c in g.categories]
    assert "lebensmittel.suesses_eis" in keys
    with pytest.raises(OpError, match="already has"):
        call(ctx, "create_category", group="lebensmittel", name="x", key="suesses_eis")

    call(ctx, "rename_category", category="lebensmittel.suesses_eis", name="Eis", key="eis")
    keys = [c.key for g in call(ctx, "list_categories").groups for c in g.categories]
    assert "lebensmittel.eis" in keys and "lebensmittel.suesses_eis" not in keys

    call(ctx, "hide_category", category="lebensmittel.eis")
    keys = [c.key for g in call(ctx, "list_categories").groups for c in g.categories]
    assert "lebensmittel.eis" not in keys
    assert path.read_text(encoding="utf-8").splitlines()[0] == header


def test_create_category_refuses_a_duplicate_name(ctx):
    call(ctx, "create_category", group="lebensmittel", name="Eis")
    with pytest.raises(OpError, match=r"already has a category 'Eis' \(lebensmittel\.eis\)"):
        call(ctx, "create_category", group="lebensmittel", name=" eis ")
    keys = [c.key for g in call(ctx, "list_categories").groups for c in g.categories]
    assert keys.count("lebensmittel.eis") == 1 and "lebensmittel.eis_2" not in keys
    # the same name in another group is fine
    call(ctx, "create_category", group="freizeit", name="Eis")


def test_rename_category_key_moves_bookings(ctx):
    a = booking(ctx, category="lebensmittel.drogerie")
    call(ctx, "rename_category", category="lebensmittel.drogerie", name="Drogerie", key="bad")
    assert category_of(ctx, a) == "lebensmittel.bad"


def test_hide_category_in_use_by_rule_is_refused(ctx):
    used = ctx.settings.rules.rules[0].category
    with pytest.raises(OpError, match="still in use"):
        call(ctx, "hide_category", category=used)


def test_group_create_rename(ctx):
    call(ctx, "create_group", name="Haustiere")
    call(ctx, "create_category", group="haustiere", name="Futter")
    call(ctx, "rename_group", group="haustiere", name="Tiere", key="tiere")
    keys = [c.key for g in call(ctx, "list_categories").groups for c in g.categories]
    assert "tiere.futter" in keys
    with pytest.raises(OpError, match="unknown category group"):
        call(ctx, "rename_group", group="haustiere", name="x")


def test_set_target_and_remove(ctx):
    call(
        ctx,
        "set_category_target",
        category="lebensmittel.supermarkt",
        type="by_date",
        amount=120,
        target_date="2026-12-01",
    )
    cat = next(
        c
        for g in call(ctx, "list_categories").groups
        for c in g.categories
        if c.key == "lebensmittel.supermarkt"
    )
    assert cat.target == "120.00 by 2026-12-01"
    with pytest.raises(OpError, match="date"):
        call(
            ctx, "set_category_target", category="lebensmittel.supermarkt", type="by_date", amount=5
        )
    call(ctx, "set_category_target", category="lebensmittel.supermarkt", type="none")
    cat = next(
        c
        for g in call(ctx, "list_categories").groups
        for c in g.categories
        if c.key == "lebensmittel.supermarkt"
    )
    assert cat.target is None


# --- rules ----------------------------------------------------------------------------


def test_rule_lifecycle_and_apply(ctx):
    path = ctx.settings.rules_file
    header = path.read_text(encoding="utf-8").splitlines()[0]
    a = booking(ctx, payee="Zoo Frankfurt")
    before = len(call(ctx, "list_rules").rules)
    r = call(
        ctx,
        "add_rule",
        counterparty="(?i)zoo",
        category="freizeit.sport_hobby",
        position="first",
        apply=True,
    )
    assert "added" in r.message and any("categorization" in d for d in r.details)
    rules = call(ctx, "list_rules").rules
    assert len(rules) == before + 1 and rules[0].category == "freizeit.sport_hobby"
    assert category_of(ctx, a) == "freizeit.sport_hobby"

    rule_id = rules[0].id
    call(ctx, "update_rule", id=rule_id, purpose="(?i)ticket")
    assert call(ctx, "list_rules").rules[0].match["purpose"] == "(?i)ticket"
    with pytest.raises(OpError, match="nothing to change"):
        call(ctx, "update_rule", id=rule_id)
    with pytest.raises(OpError, match="unknown rule"):
        call(ctx, "update_rule", id="nope", purpose="x")

    call(ctx, "delete_rule", id=rule_id)
    assert len(call(ctx, "list_rules").rules) == before
    assert path.read_text(encoding="utf-8").splitlines()[0] == header


def test_rule_that_matches_nothing_says_why(ctx):
    booking(ctx, payee="Zoo Frankfurt")  # a manual booking: counterparty set, merchant empty
    t = call(ctx, "test_rule", merchant="(?i)zoo")
    assert t.matching == 0 and "use 'counterparty'" in t.hint
    assert "use 'counterparty'" in ops.find("test_rule").render(t, None)
    t = call(ctx, "test_rule", purpose="(?i)no such purpose")
    assert t.hint == "No booking matches these conditions."
    t = call(ctx, "test_rule", counterparty="(?i)zoo")
    assert t.matching == 1 and t.hint is None

    r = call(ctx, "add_rule", merchant="(?i)zoo", category="freizeit.sport_hobby", id="zoo_m")
    assert len(r.warnings) == 1 and "use 'counterparty'" in r.warnings[0]
    assert "Warning: No booking matches" in ops.find("add_rule").render(r, None)
    r = call(ctx, "update_rule", id="zoo_m", counterparty="(?i)zoo", merchant="(?i)")
    assert r.warnings == []  # now it matches
    r = call(ctx, "add_rule", counterparty="(?i)zoo", category="freizeit.sport_hobby", id="zoo_c")
    assert r.warnings == []
    for field in ("merchant", "counterparty"):
        text = ops.find("add_rule").args.model_fields[field].description
        assert "manual" in text


def test_test_rule_samples_total_is_the_match_count(ctx):
    for i in range(12):
        booking(ctx, payee="Kiosk Nord", day=date(2026, 3, 1 + i))
    t = call(ctx, "test_rule", counterparty="Kiosk Nord")
    assert t.matching == 12
    assert len(t.samples.bookings) == 10 and t.samples.total == 12  # was 10 (#91)
    assert "of 12" in ops.find("test_rule").render(t, None)


def test_add_rule_validation(ctx):
    with pytest.raises(OpError, match="regular expression"):
        call(ctx, "add_rule", purpose="(", category="freizeit.sport_hobby")
    with pytest.raises(OpError, match="at least one condition"):
        call(ctx, "add_rule", category="freizeit.sport_hobby")
    with pytest.raises(OpError, match="unknown category"):
        call(ctx, "add_rule", purpose="x", category="nope")


# --- maintenance ----------------------------------------------------------------------


def test_recategorize_and_train(ctx):
    r = call(ctx, "recategorize")
    assert r.message.startswith("categorization:")
    with pytest.raises(OpError, match="not enough training data"):
        call(ctx, "train")


def test_import_files(ctx, statement_pdf):
    r = call(ctx, "import_files", paths=[str(statement_pdf)])
    assert "1 of 1 files imported" in r.message
    again = call(ctx, "import_files", paths=[str(statement_pdf)])
    assert "0 of 1" in again.message and "already_imported" in again.details[0]
    assert call(ctx, "list_imports").imports
    with pytest.raises(OpError, match="does not exist"):
        call(ctx, "import_files", paths=["no/such/file.pdf"])
