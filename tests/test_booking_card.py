"""The booking card: labels keep their width next to long values (#39)."""

from PySide6.QtWidgets import QApplication, QFormLayout

from hiro_budget.gui.booking_card import ZWSP, BookingCard, FactLabel, breakable

ARN = "NR XXXX 4821 DARMSTADT DE KAUFUMSATZ 19.10 102.02 208969 ARN39120088582408494608686"


def test_long_tokens_get_wrap_points() -> None:
    assert breakable("short words stay") == "short words stay"
    broken = breakable(ARN)
    assert ZWSP in broken and broken.replace(ZWSP, "") == ARN
    assert breakable("DE00123456780123456789") == "DE00123456780123456789"  # an IBAN: 22


def test_fact_label_text_and_copy_without_wrap_points(qtbot) -> None:
    label = FactLabel()
    qtbot.addWidget(label)
    label.setText(ARN)
    assert label.text() == ARN
    label._copy()
    assert QApplication.clipboard().text() == ARN


def test_label_column_is_not_squeezed_by_a_long_value(qtbot) -> None:
    card = BookingCard()
    qtbot.addWidget(card)
    card.resize(335, 700)
    card.values["purpose"].setText(ARN)
    card.facts.setRowVisible(card.values["purpose"], True)
    card.show()
    qtbot.waitExposed(card)
    label = card.facts.itemAt(5, QFormLayout.ItemRole.LabelRole).widget()
    assert label.text() == card.tr("Purpose")
    assert label.width() >= label.sizeHint().width()
    assert card.values["purpose"].geometry().right() <= card.width()


# --- the payee's other bookings (#50) ---------------------------------------------------


def _streaming(app, amounts: list[int]) -> list[int]:
    """Monthly bookings at one payee (name varying in digits and case), oldest first."""
    from datetime import date

    from sqlalchemy import select

    from hiro_budget.db.models import Account
    from hiro_budget.services import transactions as txs

    with app.session() as s:
        cash = s.scalar(select(Account.id).where(Account.name == "Bargeld"))
        return [
            txs.create_manual(
                s,
                app.settings,
                txs.ManualEntry(cash, date(2026, 1 + i, 14), cents, f"STREAMFLIX.COM {100 + i}"),
            )
            for i, cents in enumerate(amounts)
        ]


def _cash(session) -> int:
    from sqlalchemy import select

    from hiro_budget.db.models import Account

    return session.scalar(select(Account.id).where(Account.name == "Bargeld"))


def test_payee_history(app) -> None:
    from datetime import date

    from hiro_budget.services import transactions as txs

    ids = _streaming(app, [-1599, -1599, -1599, -1799, -1799])
    with app.session() as s:
        h = txs.payee_history(s, ids[3])  # the first at the new price
        assert h.previous_amount == -1599
        assert sorted(h.ids) == sorted(ids)
        assert h.year_count == 4 and h.year_average == round((3 * -1599 - 1799) / 4)
        assert [b.id for b in h.last] == [ids[4], ids[2], ids[1], ids[0]]  # newest first
        assert txs.payee_history(s, ids[4]).previous_amount is None  # only 1 at the new one
        assert txs.payee_history(s, ids[1]).previous_amount is None  # not 3 steady before
        single = txs.create_manual(
            s, app.settings, txs.ManualEntry(_cash(s), date(2026, 6, 1), -500, "Unikat")
        )
        assert txs.payee_history(s, single) is None  # the payee's only booking


def test_card_shows_the_history_and_all_of_them(qtbot, app) -> None:
    from hiro_budget.gui.main_window import MainWindow

    ids = _streaming(app, [-1599, -1599, -1599, -1799])
    w = MainWindow(app)
    qtbot.addWidget(w)
    w._show_booking(ids[-1])
    card = w.transactions.card
    assert card.history.isVisibleTo(card)
    text = card.history.text()
    assert "-15,99" in text or "-15.99" in text
    assert "href" in text
    card.history.linkActivated.emit("#")
    view = w.transactions
    assert {r.id for r in view.model.rows()} == set(ids)
    assert "STREAMFLIX" in view.subset_bar.text()
    card.show_history(None)  # a payee with no other booking: no section
    assert not card.history.isVisibleTo(card)
