import pytest

from hiro_budget.money import format_cents, parse_german_amount


@pytest.mark.parametrize(
    ("text", "cents"),
    [
        ("0,00", 0),
        ("7,20", 720),
        ("-57,40", -5740),
        ("1.234,56", 123456),
        ("-2.345,67", -234567),
        ("1.000.000,01", 100000001),
        ("1234,56", 123456),
    ],
)
def test_parse_german_amount(text: str, cents: int) -> None:
    assert parse_german_amount(text) == cents


@pytest.mark.parametrize("text", ["", "12", "1,2", "1.23,45", "abc", "12.50"])
def test_parse_german_amount_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_german_amount(text)


def test_format_cents() -> None:
    assert format_cents(-123456) == "-1.234,56 €"
    assert format_cents(5, locale="en", currency=None) == "0.05"
    assert format_cents(123456789, locale="en") == "1,234,567.89 €"


@pytest.mark.parametrize(
    ("text", "cents"),
    [("600", 600_00), ("12,5", 12_50), ("1.234,56", 1234_56), ("1234.56", 1234_56),
     ("1.234", 1234_00), ("600 €", 600_00), ("-20", -20_00), ("0,01", 1)],
)  # fmt: skip
def test_parse_user_amount(text, cents) -> None:
    from hiro_budget.money import parse_user_amount

    assert parse_user_amount(text) == cents


@pytest.mark.parametrize("text", ["", "abc", "1,2,3", "12..5", "€"])
def test_parse_user_amount_rejects(text) -> None:
    from hiro_budget.money import parse_user_amount

    with pytest.raises(ValueError):
        parse_user_amount(text)


def test_apply_amount_input() -> None:
    from hiro_budget.money import apply_amount_input

    assert apply_amount_input("+50", 100_00) == 150_00
    assert apply_amount_input("-20,5", 100_00) == 79_50
    assert apply_amount_input("300", 100_00) == 300_00
    assert apply_amount_input("=-20", 100_00) == -20_00
