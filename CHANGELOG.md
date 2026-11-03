# Changelog

## 0.2.0

- Budget: a progress bar under each target; Move money suggests the line that needs money.
- Bookings: split a booking in the detail card, assign several bookings at once, running
  balance, the payee's other bookings in the card, search by amount (also in Ctrl+K).
- Accounts: balance chart of the selected account, IBANs in groups of four, a note for
  accounts without bookings for a long time.
- Reports: expenses donut by budget group, partial months marked, year comparison cut at
  the last day with data, overdue fixed costs, mortgage payoff forecast with special
  repayments.
- Rules: search field, hit count per rule, show the matched bookings; regex cheat sheet.
- Keyboard: one shortcut table for the whole app (F1 overview), keys for lists, filters,
  months, report tabs, charts and the main actions; F6 moves between areas.
- Help → User guide in the app (German and English, with screenshots).
- File → Restore backup; Settings: move the data folder (database, classifier, backups).
- Faster on large data: bookings list, budget vs. actual, reports and counter-bookings;
  classifier training in the background.
- Plugin API 2: one interface for file importers, CSV formats and bank connectors (FinTS
  is now a connector plugin). Plugins written for API 1 need updating.
- Fixes: English UI showed 53 German texts; misspelt keys in the YAML files are errors;
  formula-like booking texts stay text in XLSX/CSV exports; errors are logged with their
  traceback.

## 0.1.0 (first public release)

- Budget along YNAB lines: assign money to categories, targets, move money, proposals.
- Imports: ING Kontoauszug, Umsatzanzeige (PDF and CSV), Depotauszug, mortgage statement;
  Revolut (PDF and CSV); PayPal CSV with payments linked to their bank bookings; FinTS
  fetch for ING accounts.
- Categorization: YAML rules, transfers between own accounts, mortgage split, a local
  classifier.
- Reports: overview, net worth, budget vs. actual, savings rate, cash flow (Sankey), year
  comparison, fixed costs, mortgage, depot, payees; breakdown of every number.
- Plugins: every importer is a plugin; own CSV formats and importer plugins in
  `config/plugins/`, Settings → Plugins.
- Demo: ten years of a fictional family (`hiro-budget demo`, `Start demo.cmd`).
- German and English; Windows exe (unsigned).
