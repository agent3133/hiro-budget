<!-- Thanks! Please keep real personal data out of code, tests and this description. -->

## What and why

## Checklist (see CONTRIBUTING.md)

- [ ] `uv run pytest -q` and `uv run ruff check src tests packaging scripts` pass
- [ ] Tests for the change (importers: detect, signs, balance check, a second import finds only duplicates)
- [ ] No real data: made-up names, IBANs with an invalid checksum, addresses at example.org
- [ ] Docs updated where the behaviour changes (UI texts: translations in `src/hiro_budget/i18n`)
- [ ] Importers: the importer `name` is final; only `hiro_budget.plugin_api` is imported from the app
