"""Search by amount (#43): "17,99", "-17,99", "17", "100..200", ">500" in the bookings
search and the title bar search (both use ``TxFilter.text``)."""

import pytest

from hiro_budget.services import import_service
from hiro_budget.services import transactions as txs
from hiro_budget.services.transactions import AmountQuery, parse_amount_query


@pytest.mark.parametrize(
    ("text", "query"),
    [
        ("17,99", AmountQuery(1799, 1799)),
        ("17.99", AmountQuery(1799, 1799)),
        ("17,99 €", AmountQuery(1799, 1799)),
        ("-17,99", AmountQuery(-1799, -1799, signed=True)),
        ("+17,99", AmountQuery(1799, 1799, signed=True)),
        ("17", AmountQuery(1700, 1799)),
        ("1.234,56", AmountQuery(123456, 123456)),
        ("1,234.56", AmountQuery(123456, 123456)),
        ("1.234", AmountQuery(123400, 123499)),
        ("100..200", AmountQuery(10000, 20099)),
        ("100,50..200,25", AmountQuery(10050, 20025)),
        ("-200..-100", AmountQuery(-20000, -10000, signed=True)),
        (">500", AmountQuery(50001, None)),
        (">= 500", AmountQuery(50000, None)),
        ("<50", AmountQuery(0, 4999)),
        ("<-50", AmountQuery(None, -5001, signed=True)),
    ],
)
def test_parse_amount_query(text, query) -> None:
    assert parse_amount_query(text) == query


@pytest.mark.parametrize("text", ["rewe", "", "17,999", "1,2,3", "..5", ">", "netflix 17"])
def test_not_an_amount(text) -> None:
    assert parse_amount_query(text) is None


def test_filter_by_amount_and_text(app, statement_pdf) -> None:
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
        everything = txs.list_transactions(s)
        hw = next(r for r in everything if "Handwerker" in (r.payee or r.purpose))
        assert hw.amount_cents == -120_00

        def found(text: str) -> set[int]:
            return {r.id for r in txs.list_transactions(s, txs.TxFilter(text=text))}

        assert hw.id in found("120,00")
        assert hw.id in found("120")
        assert hw.id in found("-120")
        assert hw.id not in found("+120")
        assert hw.id in found("100..150")
        assert hw.id in found(">100")
        assert hw.id not in found("<100")
        assert hw.id in found("Handwerker")  # text still works
        expected = {r.id for r in everything if abs(r.amount_cents) > 100_00}
        assert found(">100") >= expected
        assert txs.count_transactions(s, txs.TxFilter(text="120,00")) >= 1
