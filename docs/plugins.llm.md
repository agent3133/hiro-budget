# Importer plugins: reference for an AI assistant

This file is written for a large language model that helps a user of the Family Finance
Analyser write an **importer plugin** (Python) for a bank or payment service, usually for its
PDF statements. People read [plugins.md](plugins.md). For a CSV export, a format without code
is enough: use [csv-formats.llm.md](csv-formats.llm.md) instead.

## For the user: how to use this file

1. Get the statement's text as the importer will see it, with obvious personal data masked:

   ```sh
   hiro-budget dump-pdf statement.pdf --mask > sample.txt
   ```

   Then open `sample.txt` and replace **names, addresses and purpose texts** by hand
   (`--mask` only replaces IBANs, e-mail addresses and numbers of six digits or more). Keep
   dates, amounts, booking types and the layout. Change amounts only consistently (balances
   must still add up). Two statements, or one with two pages, are better than one page.
2. Start a new chat with an AI assistant, paste this whole file and then:

   ```text
   Please write an importer plugin following the reference above.
   Bank / document: <e.g. "Musterbank, monthly Kontoauszug as PDF">
   Account type: <giro | savings | credit_card | wallet | depot | loan>
   The same account is also imported from: <nothing | CSV export | FinTS>
   Sample (dump-pdf --mask, names replaced):
   <paste sample.txt>
   ```

3. Create the folder and files the assistant names in `config/plugins/`, then follow its test
   steps. Report errors verbatim.

---

## 0. Your task (assistant)

Write **one plugin folder**: `plugin.yaml` and `importer.py` (one importer class), plus a
test module the user can contribute later. The importer must turn every statement of this
kind into a `ParsedStatement` whose balances check out, with stable booking texts. Base every
pattern on the sample; ask when the sample does not show something that matters (section 2).
Use only what section 4 lists. Never put the user's personal data into the code or tests.

## 1. Privacy

The sample should be masked as above. If the user pastes unmasked personal data, say so and
work with it without repeating it. Tests and examples use made-up data only: names like
`Max Muster`, IBANs with an **invalid** checksum (`DE12 3456 7890 1234 5678 90`), addresses at
`example.org`. The app's test suite rejects IBANs with a valid checksum and other e-mail
domains in tests (`tests/test_privacy.py`).

## 2. Questions to settle

1. **Detection:** a text on the first page that only this document has (bank name together
   with the document title). Must not match the bank's other documents (e.g. a securities
   statement of the same bank).
2. **Header:** IBAN (or account number), account/product name, statement number and date,
   period, opening and closing balance. Which of them exist?
3. **Booking region:** where the booking table starts (a column header line) and ends on each
   page (footer, page number, "new balance" line); lines to skip inside (repeated headers,
   carry-over lines such as "Übertrag").
4. **Booking lines:** which line starts a booking (usually a date at the start and an amount
   at the end) and which lines continue it (purpose, references). The order of booking date
   and value date.
5. **Amounts and sign:** German or English format; sign in the amount, in a separate column
   (`S`/`H`, `-`/`+`), or only visible through the running balance (then derive it from the
   change of the balance, like the built-in `revolut_pdf`).
6. **Order:** oldest or newest first (the app wants oldest first).
7. **Special rows:** reversed or pending bookings, carry-overs, sub-totals, fees on their own
   line, several accounts or currencies in one document.
8. **Other sources of the same account** (CSV export, FinTS): booking types and texts should
   then match those sources so that duplicates are recognised (section 3).

## 3. How the app uses the result

1. **Detection:** for every file, the importers of all plugins are asked (`detect`) in order:
   user plugins before built-in ones, by folder name; the first `True` wins. `detect` must be
   cheap, specific and never raise (an exception counts as `False` and is reported).
2. **Parsing:** `parse(path)` returns a `ParsedStatement` or raises `ImportError_(message)`;
   the message is shown to the user.
3. **Balance check:** if `opening_balance_cents` and `closing_balance_cents` are both set,
   opening + sum of booking amounts must equal closing, or the file is refused ("balance
   check failed"). If the statement shows a running balance, also compare every booking
   with it and append a warning for each mismatch (`warnings` are shown in the dialog).
4. **Account:** found by `account_iban`, else by `account_name`; if none exists, created at
   import with `account_name`, `account_kind`, `institution`, `account_iban`, `currency`.
5. **Duplicates:** each booking gets a key: account (IBAN, else name) + `external_id` if set,
   else booking date, amount, booking type, counterparty, purpose (all whitespace removed,
   lower case), with an occurrence number for identical bookings. A stored key is a
   duplicate. So texts must be **deterministic** and stay the same in later versions.
6. **Overlap:** if stored bookings exist in `period_start`..`period_end` (widened to the
   booking dates), the user chooses "add missing" or "overwrite".
7. **Afterwards:** categorization by rules and a classifier on `counterparty`, `merchant`,
   `purpose`, `booking_type`. A good `counterparty` (the payee) and `merchant` (for card
   payments: the shop) make categorization work.

## 4. The API (`from hiro_budget.plugin_api import …`, `api: 1`)

Import nothing else from `hiro_budget`. The standard library and `pdfplumber` (through
the helpers) are available; do not add other third-party packages.

```python
@dataclass
class ParsedTransaction:
    booking_date: date
    amount_cents: int             # negative = money out
    booking_type: str             # e.g. "Lastschrift", "Gutschrift", "Kartenzahlung"
    counterparty: str = ""        # payee or payer
    purpose: str = ""             # purpose text, continuation lines joined with spaces
    value_date: date | None = None
    mandate_ref: str | None = None
    reference: str | None = None
    merchant: str | None = None   # card payments: the shop
    card_purchase_date: date | None = None
    raw_text: str = ""            # the original lines, "\n"-joined
    external_id: str | None = None  # a unique transaction ID, if the statement has one

@dataclass
class ParsedStatement:
    importer: str                 # the importer's name
    institution: str
    account_name: str
    account_kind: str             # giro, savings, cash, credit_card, wallet, depot, loan
    account_iban: str | None      # without spaces
    currency: str = "EUR"
    statement_no: str | None = None
    statement_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    opening_balance_cents: int | None = None
    closing_balance_cents: int | None = None
    transactions: list[ParsedTransaction] = []   # oldest first
    warnings: list[str] = []
    valuation: bool = False       # securities statement: holdings instead of bookings
    holdings: list[ParsedHolding] = []
    # links, dedupe_scope, match_by_date_amount: payment services (see the CSV engine)
```

| Helper | Returns |
|---|---|
| `ImportError_(message)` | the exception for files that cannot be parsed |
| `pdf_page_texts(path, x_tolerance=3)` | `list[str]`, the text of each page (as `dump-pdf`, which uses 1.5) |
| `pdf_first_page_text(path, x_tolerance=3)` | `str`, `""` if the file is no readable PDF |
| `parse_german_amount("-1.234,56")` | `-123456`; raises `ValueError` otherwise |
| `module(plugin, name)` | a module of a plugin listed in `requires` |
| `enricher_for(bank_code=…, institution=…)` | a function `(counterparty, purpose, booking_date) -> (merchant, purchase_date)` or `None` |

Manifest (`plugin.yaml`), unknown keys rejected:

```yaml
name: musterbank_pdf          # = folder name; [a-z][a-z0-9_]*
version: 1.0.0
api: 1
description: Musterbank Kontoauszug as PDF
importers:
  - {module: importer, class: MusterbankPdfImporter}
```

## 5. Worked example

A made-up statement, as `dump-pdf` prints it:

```text
===== page 1 =====
Musterbank AG Kontoauszug Nr. 10/2026
IBAN DE11 2222 3333 4444 5555 66
Zeitraum 01.10.2026 - 31.10.2026
Alter Kontostand 1.000,00 EUR
Datum Text Betrag
02.10.2026 Lastschrift REWE Markt GmbH -45,10
Einkauf Filiale 123
05.10.2026 Gutschrift Arbeitgeber GmbH 2.500,00
Gehalt Oktober
1/2
===== page 2 =====
Musterbank AG Kontoauszug Nr. 10/2026
Datum Text Betrag
30.10.2026 Dauerauftrag Max Muster -900,00
Miete Oktober
Neuer Kontostand 2.554,90 EUR
2/2
```

`config/plugins/musterbank_pdf/importer.py`:

```python
"""Musterbank Kontoauszug as PDF.

Header: IBAN, period, old and new balance. Bookings: a line with date, type, counterparty
and amount, then purpose lines; the table starts after "Datum Text Betrag" on every page
and ends at the page number or the new balance.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from hiro_budget.plugin_api import (
    ImportError_,
    ParsedStatement,
    ParsedTransaction,
    parse_german_amount,
    pdf_first_page_text,
    pdf_page_texts,
)

X_TOLERANCE = 1.5
DATE = r"\d{2}\.\d{2}\.\d{4}"
AMOUNT = r"-?\d{1,3}(?:\.\d{3})*,\d{2}"
BOOKING = re.compile(rf"^({DATE}) (\S+) (.+?) ({AMOUNT})$")
IBAN = re.compile(r"IBAN ([A-Z]{2}\d{2}(?: ?\d{4}){4} ?\d{2})")
PERIOD = re.compile(rf"Zeitraum ({DATE}) - ({DATE})")
OPENING = re.compile(rf"Alter Kontostand ({AMOUNT}) EUR")
CLOSING = re.compile(rf"Neuer Kontostand ({AMOUNT}) EUR")
TABLE_START = "Datum Text Betrag"
PAGE_NUMBER = re.compile(r"^\d+/\d+$")


def _date(text: str) -> date:
    d, m, y = text.split(".")
    return date(int(y), int(m), int(d))


def booking_lines(pages: list[str]) -> list[str]:
    lines = []
    for page in pages:
        rows = [line.strip() for line in page.splitlines() if line.strip()]
        if TABLE_START not in rows:
            continue
        for line in rows[rows.index(TABLE_START) + 1 :]:
            if PAGE_NUMBER.match(line) or CLOSING.match(line):
                break
            lines.append(line)
    return lines


def parse_pages(pages: list[str]) -> ParsedStatement:
    text = "\n".join(pages)
    iban, period = IBAN.search(text), PERIOD.search(text)
    opening, closing = OPENING.search(text), CLOSING.search(text)
    if not (iban and period and opening and closing):
        raise ImportError_("statement header not found (IBAN, period, balances)")
    transactions: list[ParsedTransaction] = []
    for line in booking_lines(pages):
        if m := BOOKING.match(line):
            transactions.append(
                ParsedTransaction(
                    booking_date=_date(m.group(1)),
                    amount_cents=parse_german_amount(m.group(4)),
                    booking_type=m.group(2),
                    counterparty=m.group(3),
                    raw_text=line,
                )
            )
        elif transactions:  # a purpose line of the booking above
            t = transactions[-1]
            t.purpose = f"{t.purpose} {line}".strip()
            t.raw_text += "\n" + line
    if not transactions:
        raise ImportError_("no bookings found")
    return ParsedStatement(
        importer=MusterbankPdfImporter.name,
        institution="Musterbank",
        account_name="Musterbank Girokonto",
        account_kind="giro",
        account_iban=iban.group(1).replace(" ", ""),
        period_start=_date(period.group(1)),
        period_end=_date(period.group(2)),
        opening_balance_cents=parse_german_amount(opening.group(1)),
        closing_balance_cents=parse_german_amount(closing.group(1)),
        transactions=transactions,
    )


class MusterbankPdfImporter:
    name = "musterbank_pdf"

    def detect(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        return "Musterbank AG Kontoauszug" in pdf_first_page_text(path, X_TOLERANCE)

    def parse(self, path: Path) -> ParsedStatement:
        try:
            pages = pdf_page_texts(path, X_TOLERANCE)
        except Exception as e:
            raise ImportError_(f"cannot read {path.name}: {e}") from e
        return parse_pages(pages)
```

Notes: `parse_pages` works on text, so tests need no PDF; `detect` and `parse` are thin.
A statement with a running balance per booking would also compare each booking with it and
add warnings; one with in/out columns would take the sign from the balance change.

Test (`tests/test_musterbank_pdf.py` for a contribution; fixture pages in
`tests/fixtures/musterbank.py`):

```python
from hiro_budget.plugins.musterbank_pdf.importer import parse_pages
from tests.fixtures import musterbank as fx


def test_statement():
    stmt = parse_pages(fx.PAGES)
    assert stmt.account_iban == "DE11222233334444555566"
    assert stmt.balance_ok and stmt.warnings == []
    assert [(t.booking_type, t.amount_cents) for t in stmt.transactions] == [
        ("Lastschrift", -4510),
        ("Gutschrift", 250000),
        ("Dauerauftrag", -90000),
    ]
    assert stmt.transactions[0].purpose == "Einkauf Filiale 123"


def test_import_twice(app, tmp_path):
    from hiro_budget.services import import_service
    from tests.conftest import write_statement_pdf

    pdf = write_statement_pdf(tmp_path / "musterbank.pdf", fx.PAGES)
    with app.session() as s:
        assert import_service.import_file(s, app.settings, pdf).added == 3
        again = write_statement_pdf(tmp_path / "copy.pdf", fx.PAGES + [""])
        assert import_service.preview(s, again, settings=app.settings).new_count == 0
```

## 6. Not supported: say so

- Scanned statements (images without a text layer): `dump-pdf` prints nothing useful; the
  app has no OCR.
- Statements whose layout differs per month in ways the sample does not show: ask for more
  samples instead of guessing.
- Third-party packages beyond the standard library and `pdfplumber`.

## 7. Your answer

1. The folder name, `plugin.yaml` and `importer.py`; short comments on non-obvious patterns.
2. Assumptions and what the user should check (e.g. "I assumed bookings never span pages").
3. Test steps:
   1. Create `config/plugins/<name>/` with both files.
   2. "Settings → Plugins…" → "Read again"; tick the plugin (it contains code), "Save",
      confirm. It must show "active", else send the error shown there.
   3. "Import" → "Add files…" → a real statement. Check account, period, number of
      bookings, "Balance" ✓ and the bookings (dates, signs, payees). Nothing is imported
      before "Import".
   4. After a change: "Read again" and add the file again.
4. For a contribution: the test module and a made-up fixture (section 1), see
   [CONTRIBUTING.md](../CONTRIBUTING.md).

Before answering, check: `name` in the manifest equals the folder and the class's `name` is
stable; every regex matches the sample's lines; the sample's balances add up with your
signs; `detect` cannot raise; only `plugin_api` is imported from the app.
