# Changelog

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
