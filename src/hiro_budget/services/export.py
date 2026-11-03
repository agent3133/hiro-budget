"""Export of bookings and report tables to CSV or Excel (.xlsx).

Bookings are exported one row per split, so split bookings can be summed per category
in a spreadsheet; the full booking amount is repeated in its own column.

CSV files use ``;`` as separator, decimal comma (German) or point (English) and UTF-8
with BOM so Excel opens umlauts and amounts correctly.
"""

from __future__ import annotations

import csv
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

from hiro_budget.services.reports import UNCATEGORIZED_KEY, CategoryTotal, MonthTotals
from hiro_budget.services.transactions import SplitView, TransactionRow

HEADERS = {
    "de": {
        "booking_date": "Buchungsdatum",
        "value_date": "Valuta",
        "account": "Konto",
        "type": "Buchungsart",
        "payee": "Empfänger",
        "counterparty": "Gegenpartei",
        "purpose": "Verwendungszweck",
        "split_amount": "Betrag",
        "booking_amount": "Buchungsbetrag",
        "group": "Kategoriegruppe",
        "category": "Kategorie",
        "transfer": "Umbuchung auf",
        "source": "Quelle",
        "memo": "Notiz Aufteilung",
        "notes": "Notiz",
        "mandate": "Mandat",
        "reference": "Referenz",
        "month": "Monat",
        "income": "Einnahmen",
        "expenses": "Ausgaben",
        "net": "Saldo",
        "share": "Anteil %",
        "one_off_income": "Einmalige Einnahmen",
        "one_off_expenses": "Einmalige Ausgaben",
        "one_off": "Einmalig",
        "yes": "ja",
        "uncategorized": "Unkategorisiert",
        "sheet_bookings": "Buchungen",
        "sheet_months": "Monate",
        "sheet_categories": "Kategorien",
    },
    "en": {
        "booking_date": "Booking date",
        "value_date": "Value date",
        "account": "Account",
        "type": "Booking type",
        "payee": "Payee",
        "counterparty": "Counterparty",
        "purpose": "Purpose",
        "split_amount": "Amount",
        "booking_amount": "Booking amount",
        "group": "Category group",
        "category": "Category",
        "transfer": "Transfer to",
        "source": "Source",
        "memo": "Split memo",
        "notes": "Notes",
        "mandate": "Mandate",
        "reference": "Reference",
        "month": "Month",
        "income": "Income",
        "expenses": "Expenses",
        "net": "Balance",
        "share": "Share %",
        "one_off_income": "One-off income",
        "one_off_expenses": "One-off expenses",
        "one_off": "One-off",
        "yes": "yes",
        "uncategorized": "Uncategorized",
        "sheet_bookings": "Bookings",
        "sheet_months": "Months",
        "sheet_categories": "Categories",
    },
}

# Columns holding money (written as numbers in EUR) and dates.
_MONEY = {
    "split_amount",
    "booking_amount",
    "income",
    "expenses",
    "net",
    "one_off_income",
    "one_off_expenses",
}
_DATES = {"booking_date", "value_date"}


def _h(language: str) -> dict[str, str]:
    return HEADERS.get(language, HEADERS["en"])


# --- tables --------------------------------------------------------------------------


def transactions_table(
    rows: Sequence[TransactionRow], language: str
) -> tuple[list[str], list[list[Any]]]:
    keys = [
        "booking_date", "value_date", "account", "type", "payee", "counterparty", "purpose",
        "split_amount", "booking_amount", "group", "category", "transfer", "source", "memo",
        "notes", "mandate", "reference",
    ]  # fmt: skip
    data = []
    for r in rows:
        splits: list[SplitView | None] = list(r.splits) or [None]  # no splits: one row
        for s in splits:
            data.append(
                [
                    r.booking_date,
                    r.value_date,
                    r.account_name,
                    r.booking_type,
                    r.payee,
                    r.counterparty,
                    r.purpose,
                    (s.amount_cents if s else r.amount_cents) / 100,
                    r.amount_cents / 100,
                    s.group_name if s else None,
                    s.category_name if s else None,
                    s.transfer_account_name if s else None,
                    s.source.value if s else None,
                    s.memo if s else None,
                    r.notes,
                    r.mandate_ref,
                    r.reference,
                ]
            )
    return keys, data


def months_table(months: Sequence[MonthTotals]) -> tuple[list[str], list[list[Any]]]:
    """One-off columns: of one-off categories, whether they are in income/expenses or not."""
    keys = ["month", "income", "expenses", "net", "one_off_income", "one_off_expenses"]
    data = [
        [
            m.month,
            m.income_cents / 100,
            m.expense_cents / 100,
            m.net_cents / 100,
            m.one_off_income_cents / 100,
            m.one_off_expense_cents / 100,
        ]
        for m in months
    ]
    return keys, data


def categories_table(
    cats: Sequence[CategoryTotal], language: str
) -> tuple[list[str], list[list[Any]]]:
    total = sum(c.expense_cents for c in cats if c.expense_cents > 0)
    keys = ["group", "category", "expenses", "share", "one_off"]
    data = []
    for c in cats:
        name = _h(language)["uncategorized"] if c.key == UNCATEGORIZED_KEY else c.category_name
        share = round(c.expense_cents / total * 100, 1) if total else None
        one_off = _h(language)["yes"] if c.one_off else ""
        data.append([c.group_name, name, c.expense_cents / 100, share, one_off])
    return keys, data


# --- writers -------------------------------------------------------------------------


# Spreadsheets run a cell that starts with one of these as a formula (OWASP "CSV
# injection"); booking texts come from third parties (#74).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_value(value: Any, language: str) -> str:
    if isinstance(value, str):
        return "'" + value if value.startswith(_FORMULA_START) else value
    if value is None:
        return ""
    if isinstance(value, date):
        return value.strftime("%d.%m.%Y") if language == "de" else value.isoformat()
    if isinstance(value, float):
        text = f"{value:.2f}"
        return text.replace(".", ",") if language == "de" else text
    return str(value)


def write_csv(path: Path, keys: list[str], data: list[list[Any]], language: str) -> None:
    headers = _h(language)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow([headers[k] for k in keys])
        for row in data:
            w.writerow([_csv_value(v, language) for v in row])


def write_xlsx(
    path: Path, sheets: list[tuple[str, list[str], list[list[Any]]]], language: str
) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    headers = _h(language)
    wb = Workbook()
    wb.remove(wb.active)
    for title, keys, data in sheets:
        ws = wb.create_sheet(headers[title])
        ws.append([headers[k] for k in keys])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in data:
            ws.append(row)
            for cell in ws[ws.max_row]:
                if cell.data_type == "f":  # openpyxl takes text starting with "=" as a formula
                    cell.data_type = "s"
        for col, key in enumerate(keys, start=1):
            letter = get_column_letter(col)
            fmt = "#,##0.00 €" if key in _MONEY else "DD.MM.YYYY" if key in _DATES else None
            if fmt:
                for cell in ws[letter][1:]:
                    cell.number_format = fmt
            width = max(
                [len(headers[key])] + [len(_csv_value(r[col - 1], language)) for r in data[:500]]
            )
            ws.column_dimensions[letter].width = min(60, width + 2)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    wb.save(path)


# --- public API ----------------------------------------------------------------------


def export_transactions(path: Path, rows: Sequence[TransactionRow], language: str) -> None:
    keys, data = transactions_table(rows, language)
    if path.suffix.lower() == ".xlsx":
        write_xlsx(path, [("sheet_bookings", keys, data)], language)
    else:
        write_csv(path, keys, data, language)


def export_report(
    path: Path,
    months: Sequence[MonthTotals],
    cats: Sequence[CategoryTotal],
    language: str,
) -> list[Path]:
    """Write monthly totals and category totals.

    Excel: one file with two sheets. CSV: two files (``*_<months>.csv``, ``*_<categories>.csv``).
    """
    m_keys, m_data = months_table(months)
    c_keys, c_data = categories_table(cats, language)
    if path.suffix.lower() == ".xlsx":
        write_xlsx(
            path,
            [("sheet_months", m_keys, m_data), ("sheet_categories", c_keys, c_data)],
            language,
        )
        return [path]
    h = _h(language)
    months_path = path.with_name(f"{path.stem}_{h['sheet_months'].lower()}.csv")
    cats_path = path.with_name(f"{path.stem}_{h['sheet_categories'].lower()}.csv")
    write_csv(months_path, m_keys, m_data, language)
    write_csv(cats_path, c_keys, c_data, language)
    return [months_path, cats_path]
