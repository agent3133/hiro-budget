# Contributing

Thanks for helping! The most welcome contributions are **importers for more banks and payment
services**: every user of a bank benefits from one importer. Bug reports with a clear
description are welcome too (never attach real statements; see "Privacy").

## Importers

| Your bank offers | Contribute | How |
|---|---|---|
| a CSV export | a CSV format plugin (YAML, no code) | [docs/csv-import.en.md](docs/csv-import.en.md) |
| only PDF statements (or a CSV a format cannot describe) | an importer plugin (Python) | [docs/plugins.md](docs/plugins.md) |

An AI assistant can write either from an anonymised sample:
[docs/csv-formats.llm.md](docs/csv-formats.llm.md), [docs/plugins.llm.md](docs/plugins.llm.md).

A pull request for an importer adds files only:

- `src/hiro_budget/plugins/<name>/` with `__init__.py`, `plugin.yaml` and the module(s)
  or `format.csv.yaml`. `<name>`: the institution and the document, e.g. `musterbank_pdf`,
  `musterbank_csv`.
- `tests/test_<name>.py` and a made-up statement in `tests/fixtures/<name>.py` (text pages for
  a PDF, rendered by `write_statement_pdf` from `tests/conftest.py`; CSV text for a format).

Checklist (reviewers go through it too):

- [ ] `detect` is cheap, specific (the bank's other documents are not matched) and never
      raises; it is tested with a file of another bank.
- [ ] The balance check passes for the fixture (opening + bookings = closing), and a fixture
      with a wrong amount is refused or warned about.
- [ ] Signs: money out is negative; tested for at least one booking each way.
- [ ] A second import of the same statement finds only duplicates.
- [ ] The importer `name` is final (it is stored with every import); booking texts are
      deterministic.
- [ ] Only `hiro_budget.plugin_api` is imported from the app; no new third-party package.
- [ ] No real data (below); `tests/test_privacy.py` passes.
- [ ] `uv run pytest -q` and `uv run ruff check src tests packaging scripts` pass;
      `uv run ruff format src tests packaging` applied.

## How contributions are merged

Development happens in a separate main repository; this GitHub repository receives the
releases, issues and pull requests. A pull request is reviewed and discussed here; once
accepted, the change is applied in the main repository (crediting you as co-author) and
comes back here with the next release, at which point the pull request is closed with a
link to it. So please do not worry if a pull request is closed rather than merged: look for
your change in the release notes.

## Privacy

Statements contain personal data. Nothing real goes into the repository, issues or pull
requests: no statements, no screenshots of them, no real names, IBANs, account or card
numbers, e-mail addresses or purpose texts. Fixtures use made-up values: names like
`Max Muster`, IBANs with an **invalid** checksum (e.g. `DE12 3456 7890 1234 5678 90`),
addresses at `example.org`. `hiro-budget dump-pdf statement.pdf --mask` helps to look at a
statement's text; it does not remove names and addresses.

## Development

```sh
uv sync                                  # Python 3.12, dependencies
uv run pytest -q                         # all tests (GUI tests run headless)
uv run ruff check src tests packaging scripts  # lint, line length 100
uv run ruff format src tests packaging
uv run hiro-budget demo                  # the app on the demo (generated the first time)
```

- Code, comments, commit messages and docs are in English; the user interface is German and
  English (`self.tr(...)`; see "Translations" in the README after changing UI texts).
- Services take a database session and return data; Qt stays in `gui/`.
- Configuration is YAML; personal configuration (`config/*.yaml`, `config/plugins/`) and data
  (`input/`, `data/`) are never committed.
- Larger changes: please open an issue first to agree on the approach.
