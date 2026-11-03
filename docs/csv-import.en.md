# CSV import

*Deutsche Fassung: [csv-import.de.md](csv-import.de.md). For configuring a format with the help
of an AI assistant: [csv-formats.llm.md](csv-formats.llm.md).*

Many banks offer their bookings as a CSV file. The app imports such files like PDF statements:
same import dialog, same duplicate check, same categorization. What the columns of a bank's
CSV mean is described once per bank in a *format*. Formats for three exports are included in
the app; for any other bank you add one yourself, as a small plugin in `config/plugins/`.

| Included format | File | Notes |
|---|---|---|
| ING Umsatzanzeige | Online banking → Umsätze → export as CSV | Same bookings as the Umsatzanzeige PDF: import either one, both are recognised as the same |
| Revolut | App → account → statement → Excel/CSV, **in English** | Bookings already imported from Revolut PDFs are recognised |
| PayPal | Activity → download, CSV, all transactions | Needs a PayPal account in the app, see below |

The included formats come with the app and are updated with it. Your own formats in
`config/plugins/` are yours: the app never changes them. A format of yours with the name of an
included plugin replaces it. (Formats used to be in `config/csv_formats.yaml`: on the first
start of a newer version the app moves your own formats from there to `config/plugins/` and
renames the file to `csv_formats.yaml.migrated`.)

---

## 1. Import a CSV file

1. Put the file into the import folder (default `input/`), or use "Import" → "Add files…".
2. Check the file's line in the list before importing:
   - **Account**: the account it goes to; "(new)" means the app will create it.
   - **Bookings / New / Duplicates**: how many rows became bookings and how many are stored
     already.
   - **Balance**: ✓ and the closing balance if the file has a balance column and every row
     adds up; ✗ and the difference if not (do not import, the format is probably wrong).
   - **Status**: "ready", "nothing new", an error message, or "overlaps … stored bookings"
     (you will be asked: choose **"Add missing"** when the same bookings came from another
     source, e.g. PDFs).
3. "Import N file(s)".

CSV exports may overlap: a booking is never imported twice. Like every import, a CSV import
can be undone under "File → Import history…".

### PayPal

PayPal payments are usually paid from your current account (debit card or direct debit), so
the current account already has them as "VISA PAYPAL *…" or "PayPal Europe". They are **not**
imported a second time. Instead the current account booking gets the PayPal payee and article
("Payment details" in the booking details; rules can use the field "Payment details"). Only
what does not touch the current account becomes a booking of the PayPal account: payments
from the PayPal balance, money received or sent.

Once, before the first import: "Accounts" → "New account", type "Payment service (PayPal)",
"E-mail" = the address of the PayPal account, "Save accounts". One app account per PayPal
account.

The current account books a PayPal payment a few days later. If that booking is not imported
yet, the payment waits and is linked after the next import or bank fetch. The message after
the import says how many are waiting.

---

## 2. Add a format for your bank

You need the CSV file, a text editor and about 15 minutes. With an AI assistant it is quicker:
see [csv-formats.llm.md](csv-formats.llm.md).

### 2.1 Look at the file

Open the CSV in a **text editor** (Notepad, VS Code), not in Excel: Excel shows numbers and
dates in its own way and may change the file when saving. Note:

| Question | Typical answers |
|---|---|
| Which character separates the columns? | `;` (German banks), `,`, tab |
| Are there lines above the column names? | ING: account, IBAN, period in `key;value` lines |
| What are the column names? | the line with "Date", "Amount", … |
| How are dates written? | `31.12.2026`, `2026-12-31`, `2026-12-31 14:05:00` |
| How are amounts written? | `-1.234,56` (decimal comma) or `-1234.56` (decimal point) |
| Is there **one** signed amount column? | needed: negative = money out |
| Is there a balance after each row? | allows the balance check |
| Is there a unique transaction ID per row? | best duplicate check |
| Do umlauts look broken (`Ã¤` or `�`)? | then the encoding is not UTF-8: try `latin-1` |

### 2.2 Write the format

A format is a small plugin: a folder in `config/plugins/` with two files. Copy the included
format that is most similar (they are listed in [csv-formats.llm.md](csv-formats.llm.md),
section 7) and change it. A complete example, `config/plugins/musterbank_csv/`:

`plugin.yaml`:

```yaml
name: musterbank_csv                      # = the folder name: lower case, digits, _
version: 1.0.0
api: 2
description: Musterbank, Umsätze as CSV
formats: [format.csv.yaml]
```

`format.csv.yaml`:

```yaml
name: Musterbank                          # any name, unique
institution: Musterbank                   # bank of accounts created by the import
kind: giro                                # account type of accounts created by the import
detect:
  header_has: [Buchungstag, Betrag, Empfänger]   # column names that identify the file
encoding: latin-1                         # default utf-8-sig
delimiter: ";"                            # default ","
date_format: "%d.%m.%Y"                   # default; 2026-12-31 is "%Y-%m-%d"
decimal_comma: true                       # default; false for 1234.56
account:
  name: Musterbank Girokonto              # or iban_from: IBAN (a "IBAN;…" line above the header)
columns:
  booking_date: Buchungstag
  value_date: Valuta
  counterparty: Empfänger
  booking_type: Buchungsart
  purpose: [Verwendungszweck, Kundenreferenz]
  amount: Betrag
  balance: Saldo
skip:
  - {column: Status, equals: vorgemerkt}
```

A plugin with formats only contains no program code and needs no enabling. All settings
are listed in [csv-formats.llm.md](csv-formats.llm.md), section 4. The most important rules:

- **Column names** must be written exactly as in the file (upper and lower case, spaces). If
  two columns have the same name, the first one is used; use its position instead (`0` is the
  first column).
- **`detect.header_has`** names a few columns that only this bank's export has. The app reads
  the first 60 lines of a file and uses the first format whose names all appear in one line.
- **`account`** decides which app account the bookings belong to. With an IBAN in the file
  (`iban_from`) the account is found by its IBAN; with `name` by its name, which must be
  exactly the name of the account in the app if it exists already.
- **`amount`** must be one column with a sign. A `fee` column, if the bank lists fees
  separately, is subtracted from it.
- **`only`** and **`skip`** leave rows out: `only` keeps rows matching *all* its filters (e.g.
  `State` = `COMPLETED`, one currency); `skip` drops rows matching *any* of its filters (e.g.
  pending bookings). A filter is `{column: …, equals: …}`, `{column: …, in: [a, b]}` or
  `{column: …, matches: <regular expression>}`.
- **`values`** renames values of `booking_type` or `counterparty`, e.g. to the spelling of your
  PDF statements so that your rules keep working.

### 2.3 Test it

1. Save both files, then "Settings → Plugins…" → "Read again" (or restart the app). The list
   shows your plugin as "active", or the error that leaves it out: it names the file and the
   setting. The import dialog names plugins that are left out, too.
2. "Import" → "Add files…" → your CSV. Nothing is imported yet.
3. Check the line: the right account, a plausible number of bookings, the period, ✓ in
   "Balance" (if the format has a balance column), no error in "Status". Select the line: the
   table below shows the bookings as they will be imported. Check dates, signs, payees.
4. Wrong? "Cancel", fix the format, restart, try again. Right? Import.

---

## 3. How duplicates are recognised

A booking that is already stored is never imported again. "Stored" is decided in this order:

1. **Transaction ID** (`columns.id`): if the file has one, a booking with the same ID on the
   same account is a duplicate. The most reliable way; PayPal has it.
2. **Fingerprint**: otherwise date, amount, booking type, counterparty and purpose
   (ignoring spaces and upper/lower case). The ING CSV and the ING PDFs produce the same
   fingerprints, so they recognise each other.
3. **Date and amount** (`match_by: date_amount`): also a duplicate if the account has an
   imported booking with the same date and amount, each stored booking counted once. For a
   CSV whose bookings came from another source with other texts before (Revolut: PDF and CSV
   describe the same booking differently).

If the file's period overlaps stored bookings, the dialog asks: **"Add missing"** imports only
what is new; "Overwrite" deletes the stored bookings of the period and imports the file
instead. Use "Overwrite" only when the stored bookings are wrong: manual categories are kept
only for bookings whose fingerprint is the same in both.

---

## 4. Limits

- **One signed amount column.** Files with separate debit and credit columns, or with a
  "D/C" or "S/H" column for the sign, cannot be imported yet.
- **Amounts** may contain a sign, thousands separators and spaces, but no currency symbol
  (`12,34 €`), no parentheses for negative numbers and no trailing minus (`12,34-`).
- **One account per file.** A file with several accounts or currencies: use `only` to read one
  of them; the others cannot be imported from the same export.
- **Dates** must match `date_format` exactly; a time after the date belongs into the format
  (`"%Y-%m-%d %H:%M:%S"`).
- PayPal-style linking (`funding`) needs a transaction ID column and a column that names the
  linked payment.

## 5. Troubleshooting

| Message | Cause |
|---|---|
| error: format format.csv.yaml: … Extra inputs are not permitted (Settings → Plugins…) | a misspelt setting, named in the line above it, e.g. `delimeter` |
| error: plugin.yaml: … / name '…' differs from its folder (Settings → Plugins…) | the manifest: a setting misspelt, or `name` is not the folder name |
| no importer recognizes … | no format's `header_has` matches: check the names and `delimiter` |
| header row with … not found | as above, or the header is below line 60 |
| column '…' not found | a name in `columns`, `only` or `skip` is spelled differently than in the file |
| line N: time data … does not match format … | `date_format` does not fit, or `booking_date` points to the wrong column |
| line N: not an amount | wrong column, `decimal_comma` wrong, or a currency symbol in the cells |
| balance check failed (difference …) | sign or fee wrong, rows missing (filtered by `only`/`skip`), or the balance column belongs to another account |
| line N: amount … does not match the balance change | same causes, for one row; rows in an unexpected order |
| no own account with e-mail … | PayPal: add the PayPal account with this e-mail in "Accounts" |
| Umlauts broken in the preview | wrong `encoding` (`latin-1` instead of `utf-8-sig` or the other way round) |
