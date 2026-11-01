# CSV formats: reference for an AI assistant

This file is written for a large language model (ChatGPT, Claude, …) that helps a user of the
Hiro Budget write a CSV import *format* for their bank. People read
[csv-import.en.md](csv-import.en.md) (German: [csv-import.de.md](csv-import.de.md)) instead.

## For the user: how to use this file

1. Prepare an **anonymised sample** of your CSV (section 1). Never give the assistant the
   whole real file.
2. Start a new chat with an AI assistant, paste this whole file and then:

   ```text
   Please write a CSV format plugin following the reference above.
   Bank / export: <e.g. "Sparkasse, Umsätze als CSV (CAMT)">
   The file is also / was also imported as: <nothing | PDF statements | another CSV>
   App account it belongs to: <exact account name in the app, or "new">
   Anonymised sample (first lines unchanged in structure):
   <paste here>
   ```

3. Create the folder the assistant names in `config/plugins/` with its two files, restart
   the app and test it as the assistant describes. Report error messages back to the
   assistant verbatim ("Settings → Plugins…" shows a plugin's error).

---

## 0. Your task (assistant)

Write **one CSV format plugin** (section 4.0) that makes the app
import the user's CSV export correctly: right account, right sign and amount, right dates,
balance check passing, no duplicates with bookings the user already has. Base every setting
on the sample; ask when the sample does not answer a question that matters (section 2). Do
not invent settings: only the keys in section 4 exist. An unknown or misspelt key makes the
app leave the plugin out; "Settings → Plugins…" shows `error: format format.csv.yaml: …
<key> Extra inputs are not permitted`.

Answer with the YAML entry, a short explanation of the non-obvious choices, and the test
steps (section 8).

## 1. Privacy: what the sample should contain

Statements contain personal data. Ask the user for a sample that keeps the **structure** and
replaces the **personal values**:

- keep: the lines above the header (with values replaced, keep the keys such as `IBAN`), the
  header line exactly, delimiter, quoting, encoding, date and amount formats, column order;
- keep unchanged: values of type/status/currency/product columns (needed for filters and
  renames), e.g. `Card Payment`, `COMPLETED`, `Lastschrift`, `vorgemerkt`;
- replace: names, IBANs, account and card numbers, e-mail addresses, references, purpose
  texts (`Max Muster`, `DE00 0000 0000 0000 0000 00`, `max@example.org`);
- amounts and balances may be changed, but **consistently**: each row's balance must still
  equal the previous balance plus the row's amount (minus fee), otherwise the balance order
  cannot be checked;
- 5–15 rows, including at least one incoming and one outgoing booking, and one example of
  every special row kind the user knows about (pending, reverted, fees, other currency).

If the user pastes real personal data, point it out and work with it without repeating it.
Never put personal data into the format, except the account `name` the user chose: an IBAN or
e-mail belongs into the app's account settings, not into the format (use `iban_from` or
`email_column`).

## 2. Questions to settle before writing

Answer from the sample; ask the user only what the sample cannot show.

1. Delimiter, encoding (broken umlauts `Ã¤` → the file is not UTF-8; `�` → it is not
   Latin-1 read as UTF-8), lines above the header.
2. Which columns: booking date, value date, counterparty, booking type, purpose (one or
   several), reference, amount, fee, balance, transaction ID.
3. Is the amount **one signed column** (negative = money out)? If the sign is in a separate
   column or debit/credit are separate columns, the export is not supported (section 6).
4. Is there a running balance per row? In which order do the rows add up (file order or
   reversed; by which date)?
5. Which rows must not become bookings: pending, reverted/declined, other currencies or
   products, internal moves that cancel out.
6. Which account: IBAN in the lines above the header, a fixed account name, or (payment
   services) the owner's e-mail address in a column.
7. Duplicates: was the same account already imported from another source (PDF statements,
   another export, a bank fetch)? Then fingerprints will differ and `match_by: date_amount`
   is needed, or `values` renames that reproduce the other source's texts exactly.
8. Is it a payment service whose payments are paid from a bank account that is imported too
   (PayPal-like)? Then consider `funding` (section 4.9).

## 3. How the app processes a CSV file (in this order)

1. **Detection.** Only files ending in `.csv` (any case). The importers of plugins (PDF) are
   asked first, then the CSV formats of plugins: user plugins before built-in ones, each in
   the order of their folder names. A format matches if, reading the file with its
   `encoding` and `delimiter`, one of the **first 60 lines** contains every name in
   `detect.header_has` as a cell (cells are compared after stripping spaces, case-sensitive).
   The first matching format wins, so `header_has` must not also match another bank's file.
2. **Table.** The first line containing all `header_has` names is the header. Lines above it
   with at least two cells and a non-empty first cell become **metadata** (`key` → `value`,
   stripped), e.g. `IBAN;DE12 3456 …`. Every line below with at least one non-empty cell is a
   data row. Missing cells at the end of a row read as empty.
3. **Column references.** A reference is a header name (exact, after stripping; if two
   columns share a name, the **first** one) or a 0-based index (integer).
4. **`only`.** Rows that do not match **all** `only` filters are dropped completely, as if they
   were not in the file (they are not part of the balance either).
5. **Parsing each remaining row.**
   - `booking_date`: `datetime.strptime(cell, date_format)`, the date part is used. The whole
     cell must match (a time in the cell must be in `date_format`).
   - amount: the `amount` cell minus the `fee` cell (empty fee = 0).
   - amounts and balances: spaces and non-breaking spaces removed, a leading `+` removed;
     with `decimal_comma: true` every `.` is removed and `,` becomes the decimal point,
     otherwise every `,` is removed. The result must be a plain decimal number: no currency
     symbol, no parentheses, no trailing minus.
   - `balance`, `id`: read if configured.
   - Any parse error stops the import: `line N: <reason>` (N = line number in the file).
6. **Order.** With a `balance` column the app counts, for file order and for reversed order,
   the rows where `previous balance + amount ≠ balance`, and takes the order with fewer breaks
   (oldest first). On a tie, or without balance column, rows are reversed if the first date
   is later than the last.
7. **Balance check.** Every remaining break becomes a warning `line N: amount … does not match
   the balance change`. Opening balance = first row's balance − its amount; closing balance =
   last row's balance − the amounts of rows left out by `skip`. If opening + imported
   bookings ≠ closing, the file cannot be imported ("balance check failed"). Without balance
   column there is no check.
8. **`skip` and `funding`.** Rows matching **any** `skip` filter are left out (but stay part of
   the balance chain, since their balance is in the file). Funding rows are never skipped.
   Funding groups are formed (section 4.9).
9. **Account** (section 4.5). With `iban_from`, the app account with that IBAN is used,
   otherwise the account with the format's account name; if none exists, the import creates
   one with that name, `kind`, `institution` and IBAN (not for `email_column`, which requires a
   configured account).
10. **Booking fields.** `counterparty`, `booking_type` (both after `values` renames), `purpose`
    (configured cells joined with one space, empty ones left out), `value_date` (parsed with
    `date_format`; unparseable → empty), `reference`, `merchant`, raw row text. An empty
    `booking_type` becomes the format `name`. With `enrich: ING` the merchant and card
    purchase date are derived from the texts by the enricher a plugin registered for ING
    (replacing `merchant`).
11. **Duplicates.** A booking is "already stored" if its key exists. The key is built from the
    account (IBAN, else the PayPal e-mail, else the account name) and either the `id` cell
    (if `columns.id` is set and the cell is non-empty) or the **fingerprint**: booking date,
    amount, booking type, counterparty and purpose, with all whitespace removed and
    lowercased; identical bookings in one file get an occurrence number. With
    `match_by: date_amount` a booking is also stored if the account has an imported booking
    with the same booking date and amount (each stored booking matches once).
12. **Overlap.** If the account has imported bookings in the file's date range, the user is
    asked: "Add missing" (only non-duplicates) or "Overwrite" (replace the range).
13. After the import, bookings are categorized (rules, classifier) and transfers between own
    accounts are recognised as for PDF statements.

## 4. Schema

### 4.0 The plugin

A folder `config/plugins/<name>/` with two files. `<name>`: lower case letters, digits and
`_`, starting with a letter, ending in `_csv` by convention (e.g. `musterbank_csv`). A user
plugin with the name of a built-in one replaces it.

`plugin.yaml`:

```yaml
name: musterbank_csv       # exactly the folder name
version: 1.0.0
api: 1
description: Musterbank, Umsätze as CSV
requires: [ing_common]     # only with enrich: ING
formats: [format.csv.yaml]
```

`format.csv.yaml` holds the format: one mapping with the keys below (not a list). Types:
`str`, `int`, `bool`, `ref` = column name (str) or 0-based
index (int), `filter` = section 4.6.

### 4.1 Identification

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | str | required | Unique name. Also the booking type of rows without one. |
| `institution` | str | `""` | Bank name for accounts the import creates; prefix for `name_from`. |
| `kind` | `giro`, `savings`, `cash`, `credit_card`, `wallet`, `depot`, `loan` | `giro` | Account type for accounts the import creates. `wallet` = payment service (PayPal). |
| `detect.header_has` | list of str, ≥ 1 | required | Header cells that identify the export. Choose 3–5 distinctive names. |

### 4.2 Reading

| Key | Type | Default | Meaning |
|---|---|---|---|
| `encoding` | str | `utf-8-sig` | Python codec. `utf-8-sig` reads UTF-8 with or without BOM; German banks often need `latin-1` (or `cp1252` for `€` in the file). |
| `delimiter` | one character | `,` | Use `";"`, `","` or `"\t"` (quoted in YAML). Quoting with `"` is standard CSV. |
| `date_format` | str | `%d.%m.%Y` | Python `strptime` format for `booking_date` and `value_date`, e.g. `%Y-%m-%d`, `"%Y-%m-%d %H:%M:%S"`, `%d/%m/%Y`, `%d.%m.%y`. Quote it in YAML when it contains `:` or `%` at the start. |
| `decimal_comma` | bool | `true` | `true`: `1.234,56`; `false`: `1,234.56` or `1234.56`. |

### 4.3 Columns (`columns:`)

| Key | Type | Required | Meaning |
|---|---|---|---|
| `booking_date` | ref | yes | Date the booking counts for (booking/completed date, not the purchase date). |
| `amount` | ref | yes | Signed amount, negative = money out. |
| `value_date` | ref | no | Value date; for card payments often the purchase date. |
| `counterparty` | ref | no | Payee or payer. |
| `merchant` | ref | no | Shop name, if separate from the counterparty (rules can match it). |
| `booking_type` | ref | no | Kind of booking (Lastschrift, Card Payment, …). |
| `purpose` | ref or list of ref | no | Purpose text; several columns are joined with spaces. |
| `reference` | ref | no | Mandate or end-to-end reference. |
| `fee` | ref | no | Fee charged in addition to `amount`, positive; the booking is `amount − fee`. Do not set it if `amount` already includes the fee. |
| `balance` | ref | no | Account balance after the row. Strongly recommended: order detection and balance check. |
| `id` | ref | no | Unique, stable transaction ID. Best duplicate check; required for `funding`. |

### 4.4 Values

`values:` maps a field to renames: `{booking_type: {"Card Payment": Kartenzahlung}}`. Only
`booking_type` and `counterparty` are renamed. Keys must match the cell exactly (after
stripping).

### 4.5 Account (`account:`), at least one key

| Key | Meaning |
|---|---|
| `email_column: <column name>` | Payment services: the most frequent non-empty value of this column among outgoing rows (amount < 0), lowercased, is the owner's e-mail; the app account in `own_accounts` with that `email` is used. Takes precedence over all other keys. Error if no such account: the user adds it in "Accounts" (type "Payment service (PayPal)", field "E-mail"). |
| `iban_from: <metadata key>` | The IBAN is the value of that `key;value` line above the header (spaces removed, uppercased). The app account with this IBAN is used. Can be combined with `name`/`name_from` for the name of a new account. |
| `name: <account name>` | A fixed account. Must equal the app's account name exactly if the account exists. |
| `name_from: <metadata key>` | Account name = `institution` + " " + that metadata value. |

Without `email_column` and `name`, and if `name_from` finds nothing, the account name is the
format `name`.

### 4.6 Filters (`only`, `skip`, `funding.rows`)

A filter is a mapping with `column` (ref) and one or more of:

- `equals: <str>`: the cell equals the value;
- `in: [<str>, …]`: the cell equals one of the values;
- `matches: <regex>`: Python `re.search` on the cell (unanchored: use `^…$` for whole cells;
  `(?i)` for case-insensitive).

The filter matches if any of its given conditions holds. Cells are stripped first.

| Key | Type | Meaning |
|---|---|---|
| `only` | list of filter | Keep a row only if **every** filter matches. For: state = completed, one currency, one product/sub-account. Dropped rows are treated as not in the file. |
| `skip` | list of filter | Leave out a row if **any** filter matches. For: pending rows that are already in the balance column, internal moves that cancel out. Skipped rows are subtracted from the closing balance, so the check still passes. |

Rule of thumb: rows **without a balance value** or not reflected in the balance (reverted,
declined, other currencies) → `only`. Rows that **are** in the running balance but must not
become bookings → `skip`.

### 4.7 Duplicates

| Key | Values | Meaning |
|---|---|---|
| `match_by` | `fingerprint` (default), `date_amount` | `date_amount`: also treat a booking as stored if the account has an imported booking with the same date and amount. Use it when the same account was or will be imported from another source whose texts differ (PDF statements, another CSV layout). Not needed when `id` is set and the account is only ever imported from this export. |

### 4.8 Enrich

`enrich: ING` (an institution or a bank code) derives merchant and purchase date from the
booking texts with the enricher a plugin registered for that bank; the built-in library
plugin `ing_common` does it for ING's card texts (`VISA …`, `NR XXXX … KAUFUMSATZ dd.mm`).
The format's plugin then needs `requires: [ing_common]`. If no plugin enriches the name, the
file cannot be imported.

### 4.9 Funding (payment services paid from an imported bank account)

```yaml
funding:
  rows: <filter>          # which rows pull money from the bank account / card
  linked_by: <ref>        # column of a funding row that holds the id of the payment it funds
  bank_match: <regex>     # the bank booking's counterparty or purpose must match, e.g. (?i)paypal
```

Requires `columns.id`. The app groups each funded payment (the row whose `id` is named in a
funding row's `linked_by` cell) with every row naming it (funding rows, refunds). Each funding
row becomes a *linked payment* with amount = −(funding row amount), i.e. as the bank booking
shows it. The group's net amount (usually 0) is imported as one booking with the payment's
fields; a partly funded payment thus imports the part paid from the service's balance. After
every import and bank fetch, waiting linked payments are matched to bank bookings of other
accounts: same amount, booked on the payment's date or up to 10 days later, counterparty or
purpose matching `bank_match`; the bank booking gets the payee and purpose as "Payment
details". A funding row whose payment is not in the file produces a warning.

## 5. Decision rules

- **Booking date**: the date the bank books (completed, "Buchungstag"), not the order or
  purchase date: the balance adds up by it. Purchase date → `value_date`.
- **Fees**: check one row with a fee. If `previous balance + amount = balance`, the amount
  already includes the fee: no `fee`. If `previous balance + amount − fee = balance`: set `fee`.
- **Order**: you do not set it; the app finds it from the balance. Make sure the sample's
  balances are consistent so the user's test shows it.
- **Duplicates**: with a stable `id`, use it. Without, the fingerprint is fine as long as this
  export is the only source of the account. If the user imported the account from PDFs or
  another export before, use `match_by: date_amount`; renames in `values` alone rarely
  reproduce another source's texts exactly.
- **Booking types**: if the user has rules on booking types or other sources for the same
  account, rename them with `values.booking_type` to the spelling the user already has
  (ask for it).
- **`detect.header_has`**: distinctive names only. `Date` and `Amount` alone would match many
  banks' exports; add bank-specific ones (`Transaktionscode`, `Wertstellungsdatum`, `State`).
- **Account**: prefer `iban_from` when the file has the IBAN; otherwise `name` with the exact
  existing account name (ask for it).
- **Quoting in YAML**: quote values with `:`, `#`, `%` at the start, leading/trailing spaces,
  `*`, `&`, `!`, `[`, `{`, or that YAML would read as a number, date or boolean (`"0"`,
  `"2026-01-01"`, `"yes"`).

## 6. Not supported: say so instead of guessing

- Debit and credit in two columns, or the sign in a separate column (`S`/`H`, `D`/`C`,
  `Debit`/`Credit`): the importer needs one signed amount column. Workaround the user can do:
  none inside the app; tell them it is a missing feature (they may open an issue).
- Currency symbols in amount cells, negative amounts in parentheses, trailing minus.
- Several accounts in one file distinguished only by a column: possible with one format per
  account using `only`, but each format needs a different `detect.header_has`, which the same
  export cannot provide. So only one account per export can be imported.
- Header names that change from export to export (e.g. contain the date or account number).
- Files that are not CSV (XLSX, CAMT/MT940 XML): the user must export CSV.

## 7. Worked examples

These are the formats of the built-in plugins (`src/hiro_budget/plugins/<name>/`, the
authoritative copy), each with an anonymised sample of the export. Their `plugin.yaml` is as
in section 4.0; `ing_umsatzanzeige_csv` requires `ing_common`.

### 7.1 ING Umsatzanzeige (metadata lines, IBAN, Latin-1, newest first)

```text
Umsatzanzeige;Datei erstellt am: 01.11.2026 10:00

IBAN;DE00 0000 0000 0000 0000 00
Kontoname;Girokonto
Bank;ING
Kunde;Max Muster
Zeitraum;01.10.2026 - 01.11.2026
Saldo;1.000,00;EUR

Sortierung;Datum absteigend

In der CSV-Datei finden Sie alle bereits gebuchten Umsätze. …

Buchung;Wertstellungsdatum;Auftraggeber/Empfänger;Buchungstext;Verwendungszweck;Referenz;Saldo;Währung;Betrag;Währung
30.10.2026;30.10.2026;VISA SHOP;Lastschrift;NR XXXX 0000 STADT KAUFUMSATZ 28.10 12,34;;1.000,00;EUR;-12,34;EUR
29.10.2026;29.10.2026;Arbeitgeber GmbH;Gehalt/Rente;Gehalt Oktober;;1.012,34;EUR;1.000,00;EUR
```

`plugins/ing_umsatzanzeige_csv/format.csv.yaml`:

```yaml
name: ING Umsatzanzeige
institution: ING
detect:
  header_has: [Buchung, Wertstellungsdatum, Auftraggeber/Empfänger, Verwendungszweck]
encoding: latin-1
delimiter: ";"
account:
  iban_from: IBAN
  name_from: Kontoname
columns:
  booking_date: Buchung
  value_date: Wertstellungsdatum
  counterparty: Auftraggeber/Empfänger
  booking_type: Buchungstext
  purpose: Verwendungszweck
  reference: Referenz
  amount: Betrag
  balance: Saldo
values:
  # as on the PDF statements, so that the same booking is recognised in both
  booking_type:
    "Dauerauftrag / Terminueberweisung": Dauerauftrag/Terminueberw.
    Gutschrift aus Dauerauftrag: Gutschrift/Dauerauftrag
    Gutschrift Echtzeitüberweisung: Gutschrift
    Überweisung: Ueberweisung
    "Zins / Dividende WP": Zins/Dividende WP
enrich: ING
```

Notes: "Währung" appears twice; it is not needed. The renames reproduce the PDF statements'
booking types, so the fingerprints equal those of the PDFs.

### 7.2 Revolut (decimal point, date with time, fee, states, other source of the same account)

```text
Type,Product,Started Date,Completed Date,Description,Amount,Fee,Currency,State,Balance
Card Payment,Current,2026-10-01 09:12:00,2026-10-02 10:00:00,Shop,-7.44,0.07,EUR,COMPLETED,92.49
Card Payment,Current,2026-10-02 18:00:00,,Bakery,-3.50,0.00,EUR,REVERTED,
Transfer,Current,2026-10-03 08:00:00,2026-10-03 08:00:00,Balance migration to another region or legal entity,-92.49,0.00,EUR,COMPLETED,0.00
Transfer,Current,2026-10-03 08:00:00,2026-10-03 08:00:00,Balance migration to another region or legal entity,92.49,0.00,EUR,COMPLETED,92.49
Deposit,Current,2026-10-04 12:00:00,2026-10-04 12:00:00,Google Pay deposit by *0000,50.00,0.00,EUR,COMPLETED,142.49
```

`plugins/revolut_csv/format.csv.yaml`:

```yaml
name: Revolut
institution: Revolut
detect:
  header_has: [Type, Product, Started Date, Completed Date, Description, Fee, State, Balance]
date_format: "%Y-%m-%d %H:%M:%S"
decimal_comma: false
account:
  name: Revolut
columns:
  booking_date: Completed Date
  value_date: Started Date
  counterparty: Description
  merchant: Description
  booking_type: Type
  amount: Amount
  fee: Fee
  balance: Balance
only:
  - {column: Product, equals: Current}
  - {column: Currency, equals: EUR}
  - {column: State, equals: COMPLETED}
skip:
  # the balance moving between Revolut entities (pairs that cancel out)
  - {column: Description, matches: "^Balance migration to another region or legal entity$"}
values:
  # as on the PDF statements
  booking_type:
    Card Payment: Kartenzahlung
    Card Refund: Kartengutschrift
    Deposit: Einzahlung
    Transfer: Überweisung
# bookings already imported from Revolut PDF statements are recognised by date and amount
match_by: date_amount

```

Notes: the balance moves by `Amount − Fee` and adds up by `Completed Date`. Reverted rows have
no completed date and no balance → `only`. The balance migration pair is in the balance but
cancels out → `skip`. The account was imported from Revolut PDFs before, whose texts differ →
`match_by: date_amount` (in tests: every PDF booking recognised).

### 7.3 PayPal (id, e-mail account, pending rows, funding from the bank)

```text
Datum,Uhrzeit,Zeitzone,Name,Typ,Status,Währung,Brutto,Gebühr,Netto,Absender E-Mail-Adresse,Empfänger E-Mail-Adresse,Transaktionscode,…,Artikelbezeichnung,…,Zugehöriger Transaktionscode,…,Guthaben,…,Betreff,Hinweis,…,Auswirkung auf Guthaben
01.03.2026,12:00:00,CET,Shop Ltd,PayPal Express-Zahlung,Abgeschlossen,EUR,"-9,09","0,00","-9,09",max@example.org,shop@example.com,A1,…,Jacke,…,,…,"-9,09",…,,,…,Soll
01.03.2026,12:00:00,CET,,Allgemeine Gutschrift auf Kreditkarte,Abgeschlossen,EUR,"9,09","0,00","9,09",,max@example.org,F1,…,,…,A1,…,"0,00",…,,,…,Haben
```

`plugins/paypal_csv/format.csv.yaml`:

```yaml
name: PayPal
institution: PayPal
kind: wallet
detect:
  header_has: [Transaktionscode, Zugehöriger Transaktionscode, Auswirkung auf Guthaben]
account:
  # the own account (kind: wallet) whose email is this address
  email_column: Absender E-Mail-Adresse
columns:
  booking_date: Datum
  counterparty: Name
  booking_type: Typ
  purpose: [Artikelbezeichnung, Betreff, Hinweis]
  amount: Netto
  balance: Guthaben
  id: Transaktionscode
skip:
  - {column: Status, equals: Ausstehend}
funding:
  rows:
    column: Typ
    in:
      - Allgemeine Gutschrift auf Kreditkarte
      - Allgemeine Abbuchung von Kreditkarte
      - Bankgutschrift auf PayPal-Konto
  linked_by: Zugehöriger Transaktionscode
  bank_match: (?i)paypal
```

Notes: `Netto` already includes the fee, so no `fee`. Pending rows are part of `Guthaben` →
`skip`, not `only`; pending funding rows are not skipped (funding rows never are). Here the
funding rows are the card/direct-debit pulls from the current account (positive), and the
refund to the card (negative). Most rows of a typical export are such pairs: they become
linked payments, and only a few bookings remain for the PayPal account itself.

## 8. Your answer

1. The folder name and both files (`plugin.yaml`, `format.csv.yaml`), the format with a
   short comment on each non-obvious line.
2. Assumptions you made and what the user should check (e.g. "I assumed the export is always
   newest first; the balance check will tell").
3. Test steps:
   1. Create `config/plugins/<name>/` with both files, then "Settings → Plugins…" → "Read
      again". The plugin must show "active"; otherwise send the error shown there.
   2. "Import" → "Add files…" → the real CSV. Nothing is imported yet.
   3. Check: account, number of bookings, period, "Balance" ✓, "Status" without error; select
      the line and check dates, signs and payees of a few bookings in the table below.
   4. If the account already has bookings for the period: "New" should be only what is
      really new, "Duplicates" the rest.
   5. Cancel to change something; import when it is right.

Before answering, check: every column name is spelled exactly as in the sample; the date
format fits every date in the sample; amounts with fee give the sample's balances; every row
kind the user wants left out is covered by `only` or `skip`; no personal data in the format.
