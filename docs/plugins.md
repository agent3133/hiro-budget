# Writing a plugin

Everything the app imports comes from plugins: the built-in importers for ING, Revolut and
PayPal as well as importers for other banks and payment services, written by you or
contributed by others. This guide is for writing one. For an AI assistant that writes an
importer from a sample of your statement, see [plugins.llm.md](plugins.llm.md).

| What you want to import | What to write | Guide |
|---|---|---|
| a CSV export | a **format**: one YAML file, no programming | [csv-import.en.md](csv-import.en.md), [csv-formats.llm.md](csv-formats.llm.md) |
| a PDF statement, or a CSV a format cannot describe | an **importer**: a Python class | this guide, [plugins.llm.md](plugins.llm.md) |
| code several importers of one bank share | a **library** plugin | section 6 |

---

## 1. What a plugin is

A folder with a `plugin.yaml` (the *manifest*) and the files it names:

```text
config/plugins/musterbank_pdf/      your own plugin (built-in ones: src/hiro_budget/plugins/)
  plugin.yaml
  importer.py
```

```yaml
name: musterbank_pdf          # = the folder name: lower case letters, digits, _
version: 1.0.0
api: 1                        # the plugin API version (plugin_api.API_VERSION)
description: Musterbank Kontoauszug as PDF
requires: []                  # other plugins this one uses (section 6)
importers:                    # Python classes: module (file in the folder) and class
  - {module: importer, class: MusterbankPdfImporter}
formats: []                   # CSV formats: *.csv.yaml files in the folder
enrichers: []                 # see section 6
```

Unknown keys are errors: a misspelt key would otherwise be ignored silently.

- **Built-in plugins** come with the app (`src/hiro_budget/plugins/`).
- **Your plugins** are folders in `config/plugins/`. A plugin of yours with the name of a
  built-in one replaces it, e.g. to try a fix before a release.
- **Plugins with program code** (importers, enrichers) of yours run only after you enabled
  them in "Settings → Plugins…": a plugin runs with the rights of the app. Formats and
  libraries without importers need no enabling.
- "Settings → Plugins…" lists every plugin with its status and, for a plugin that is left
  out, the reason. "Read again" picks up changes without restarting the app.

## 2. Look at the statement's text

An importer works on the **text** of the PDF, not on its look. See it as the importer will:

```sh
uv run hiro-budget dump-pdf statement.pdf            # or: hiro-budget.exe dump-pdf …
uv run hiro-budget dump-pdf statement.pdf --mask     # IBANs, e-mails, long numbers replaced
```

Each page is printed under a `===== page N =====` line. `--x-tolerance` (default 1.5) sets how
far apart characters may be and still form one word: if words stick together, lower it; if
words fall apart, raise it. Use the same value in the importer.

Typical findings: a header with IBAN, period and balances; a booking table that starts after
a column header and ends at a footer or page number; bookings over several lines (the first
with date, text and amount, then purpose lines); amounts in German format (`-1.234,56`); on
some statements money in and out in separate columns, so the sign only shows in the running
balance.

## 3. Write the importer

An importer is a class with a `name` and two methods. Import **only** from
`hiro_budget.plugin_api`; everything else in the app may change between versions.

```python
from pathlib import Path

from hiro_budget.plugin_api import ImportError_, ParsedStatement, pdf_first_page_text, pdf_page_texts


class MusterbankPdfImporter:
    name = "musterbank_pdf"   # stored with every import: never change it later

    def detect(self, path: Path) -> bool:
        """Cheap and specific: is this file mine? Never raise."""
        return path.suffix.lower() == ".pdf" and "Musterbank AG Kontoauszug" in pdf_first_page_text(path, 1.5)

    def parse(self, path: Path) -> ParsedStatement:
        """The statement, or ImportError_ with a message the user understands."""
        ...
```

A complete, tested example is in [plugins.llm.md](plugins.llm.md), section 5. The built-in
importers show more cases:

| Plugin | Shows |
|---|---|
| `ing_umsatzanzeige_pdf` | the simplest layout: one line per booking with the balance after it; newest first |
| `ing_giro_pdf` | bookings over several lines, booking regions per page, references |
| `revolut_pdf` | the sign from the running balance; several sections; reversed transactions left out |
| `ing_depot_pdf` | a *valuation* (holdings and their value) instead of bookings |
| `ing_loan_pdf` | a balance statement: sets the loan balance, bookings only for the check |

### What `plugin_api` offers

| Name | Use |
|---|---|
| `ParsedStatement`, `ParsedTransaction`, `ParsedHolding`, `ParsedLink` | what `parse` returns (section 4) |
| `ImportError_` | raise it when the file cannot be parsed; the message is shown |
| `pdf_page_texts(path, x_tolerance)` | the text of every page (as `dump-pdf`) |
| `pdf_first_page_text(path, x_tolerance)` | the first page, for `detect`; empty if no readable PDF |
| `parse_german_amount("-1.234,56")` | `-123456` (cents) |
| `module("plugin", "module")` | a module of a required plugin (section 6) |
| `enricher_for(bank_code=…, institution=…)` | a bank's enricher (section 6) |
| `API_VERSION` | the API version of the running app |

## 4. What the app does with the statement

All money is in **integer cents**; negative = money out.

**`ParsedStatement`**

| Field | Meaning |
|---|---|
| `importer` | your importer's `name` |
| `institution`, `account_name`, `account_kind`, `account_iban`, `currency` | the account. The app finds it by IBAN, else by name; if none exists, the import creates it with these values. `account_kind`: `giro`, `savings`, `cash`, `credit_card`, `wallet`, `depot`, `loan` |
| `statement_no`, `statement_date` | shown in the import history |
| `period_start`, `period_end` | the days the statement covers (for overlaps and missing statements) |
| `opening_balance_cents`, `closing_balance_cents` | **the balance check**: opening + bookings must equal closing, or the file is refused ("balance check failed"). Set both whenever the statement shows them |
| `transactions` | the bookings, oldest first |
| `warnings` | shown in the import dialog (e.g. a row whose amount does not match the balance) |
| `valuation`, `holdings` | securities account statements: the holdings on `period_end` instead of bookings |
| `links`, `dedupe_scope`, `match_by_date_amount` | payment services and special duplicate handling (see the CSV engine) |

**`ParsedTransaction`**: `booking_date`, `amount_cents`, `booking_type` (required);
`counterparty`, `purpose`, `value_date`, `mandate_ref`, `reference`, `merchant`,
`card_purchase_date`, `raw_text` (the lines it came from, shown in the details),
`external_id` (a unique transaction ID, if the statement has one).

**Duplicates.** A booking is stored once. The key is the account (IBAN, else name) plus the
`external_id` if set, else date, amount, booking type, counterparty and purpose (whitespace
and case ignored). So: keep these texts **stable** between versions of your importer, and
make them equal to another source of the same account if possible (e.g. booking types as on
the bank's other statements).

**Then** the app checks overlaps with stored bookings (asks: add missing or overwrite),
replaces matching manual entries, leaves out days fetched per FinTS, and categorizes (rules
on counterparty, merchant, purpose, booking type; the classifier on these texts).

## 5. Try it

1. Put the folder into `config/plugins/`.
2. "Settings → Plugins…" → "Read again". Your plugin shows "off: contains program code": tick
   it, "Save", confirm. It must show "active"; otherwise the status names the error.
3. "Import" → "Add files…" → a statement. Nothing is imported yet. Check account, period,
   number of bookings, "Balance" ✓, "Status"; select the file and check the bookings.
4. Change the code, "Settings → Plugins…" → "Read again", add the file again.

## 6. Libraries and enrichers

Code shared by several importers of one bank goes into a **library plugin** (no
`importers`), e.g. the built-in `ing_common`. An importer plugin declares it:

```yaml
requires: [musterbank_common]
```

and reaches its modules through the plugin API, so that a replaced library is used
everywhere:

```python
from hiro_budget.plugin_api import module

layout = module("musterbank_common", "layout")
```

A plugin whose requirement is missing is left out with that reason.

An **enricher** derives the merchant and card purchase date from a bank's booking texts:
`function(counterparty, purpose, booking_date) -> (merchant, purchase_date)`. Register it in
the manifest; FinTS uses it for fetched bookings of that bank code, CSV formats with
`enrich: <institution>`:

```yaml
enrichers:
  - {bank_codes: ['50010517'], institution: ING, function: card_texts:enrich}
```

## 7. Contribute it

To have your importer in the app for everyone, open a pull request; see
[CONTRIBUTING.md](../CONTRIBUTING.md). In short:

- the plugin as `src/hiro_budget/plugins/<name>/` with `__init__.py`, `plugin.yaml` and
  the module(s);
- a test module `tests/test_<name>.py` with a **made-up** statement as text pages in
  `tests/fixtures/<name>.py` (the `write_statement_pdf` helper in `tests/conftest.py` renders
  them into a PDF): detect, header, signs, balance check, a full import, and a second
  import that finds only duplicates;
- no real data anywhere: made-up names, IBANs whose checksum is invalid (e.g.
  `DE12 3456 7890 1234 5678 90`), addresses at `example.org`. `tests/test_privacy.py`
  checks IBANs and e-mail addresses.
