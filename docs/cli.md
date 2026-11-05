# Command line

Almost everything the app does is also a command: read budgets, bookings, accounts and
reports as text in the terminal, and change categories, rules and the budget. The same
operations serve the [MCP server](mcp.md) for LLM agents, so the two cannot drift apart.

```text
hiro-budget [--config-dir DIR] <command> [options]
hiro-budget --help                      # the command tree
hiro-budget <command> --help            # the options of a command
```

The app finds its data in `./config`, in the folder next to the exe, or in `--config-dir`.
Every command takes `--json` (before or after the command) and prints the result as JSON
instead of text. `import --json` asks nothing: a statement that overlaps stored bookings is
skipped unless `--on-overlap` says otherwise.

## Conventions

| | |
|---|---|
| Amounts | text output in the app's German format (`1.234,56`); `--json` and MCP give euros as a string with a dot (`"-12.34"`, negative = money out). Input takes `-12.34`, `-12,34` and `1.234,56` |
| Months, dates | `YYYY-MM`, `YYYY-MM-DD` |
| Categories | the key `group.category` or the exact name; a category that does not exist suggests the closest keys. `uncategorized`, `review` (suggestions to confirm), `transfers` and `group:<key>` also work as a *filter* |
| Accounts | name or IBAN |
| Bookings | the numeric id every list shows |
| Lists | paged: `--limit` (default 50) and `--offset`; the last line says how many there are |
| Exit codes | `0` ok, `1` the operation failed (the message is on stderr), `2` usage or configuration error |

## Reading

```text
hiro-budget overview                          Ready to Assign, open work, this month, net worth
hiro-budget accounts list                     balances, last booking, statement gaps
hiro-budget budget show [MONTH]               groups and categories: assigned, activity, available
hiro-budget tx list [--from D --to D --account A --category C --text T --amount Q
                     --limit N --offset N]    bookings, newest first
hiro-budget tx show ID                        one booking: splits, mirror bookings, payee history
hiro-budget categories list                   groups and categories with their keys and targets
hiro-budget rules list                        rules with their hits
hiro-budget rules test --purpose REGEX ...    what a rule would match, nothing is saved
hiro-budget report monthly|categories|payees [--from D --to D --account A --no-one-offs]
hiro-budget report years 2025 2026 [--last-month 10]
hiro-budget report budget 2026-09 2026-11     budget vs. actual
hiro-budget report net-worth [--months 12]
hiro-budget report explain expenses|income|saved|budget ...    the bookings behind a number
hiro-budget imports list                      imported statements and gaps
```

`--amount` takes the query of the bookings page: `17,99` (plus or minus), `-17,99` (only
that expense), `17` (17,00 to 17,99), `100..200`, `>500`, `<=50`.

Examples:

```text
hiro-budget tx list --category uncategorized --from 2026-10-01
hiro-budget budget show 2026-11
hiro-budget report explain expenses --from 2026-10-01 --to 2026-10-31 --category wohnen.energie
```

## Changing

```text
hiro-budget tx categorize ID... CATEGORY        e.g. tx categorize 123 124 lebensmittel.supermarkt
hiro-budget tx split ID --part CAT=AMOUNT ...   the amounts add up to the booking, with its sign
hiro-budget tx transfer ID ACCOUNT              a transfer to another own account
hiro-budget tx accept ID... | tx reset ID...    confirm suggestions | categorize automatically again
hiro-budget tx note ID TEXT
hiro-budget tx add --account A --date D --amount -12,50 --payee P [--category C]
hiro-budget tx edit ID [...]   |   tx delete ID                  bookings entered by hand
hiro-budget categories add|rename|hide|target ...   groups add|rename ...
hiro-budget rules add --counterparty REGEX --category C [--position first] [--apply]
hiro-budget rules edit ID ... | rules delete ID
hiro-budget budget assign CATEGORY AMOUNT [--month M] [--relative]
hiro-budget budget move FROM TO AMOUNT         FROM or TO can be `ready` (Ready to Assign)
hiro-budget budget targets|cover|copy [MONTH]  fund targets | cover overspending | copy last month
hiro-budget import PATH... [--on-overlap add|overwrite|cancel]
hiro-budget recategorize | train
```

Categories and rules are written to `categories.yaml` and `rules.yaml` the way the app's own
editor does it: comments and the layout stay. Budget lines of a group that is budgeted as a
whole are addressed as `group:<key>`.

## Safety: backup, journal, undo

- Before the first change of a session a **backup** is made (`<date>_<time>_cli`, see
  "File → Restore backup…"). If it fails, nothing is changed. Off with
  `backup.before_write: false` in `config.yaml`.
- Every change is **journaled**: who, what, and the state of every booking, split, budget
  line, category and configuration file before and after.

  ```text
  hiro-budget history [--session ID] [--limit N]
  hiro-budget undo ID | undo --last | undo --session ID
  ```

  An undo checks first that everything the change touched is still exactly as the change
  left it. If something was modified since, **nothing** is undone and the message names it:
  undo newer changes first, or a whole session (newest first). Imports and training are in the
  journal but cannot be undone: restore the backup. The journal keeps 90 days.
- The open app window notices changes made from the command line within about two seconds
  and loads them (it asks first if you have unsaved edits).
- A change takes about a quarter of a second more on a large database, for the journal.
