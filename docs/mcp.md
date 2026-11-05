# MCP server for LLM agents

`hiro-budget mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server over
stdio. An agent such as the Hiro agent can then read your budget, find uncategorized
bookings, categorize them, create rules and assign money, with the same operations as the
[command line](cli.md). Every change is journaled and can be undone.

> **Privacy.** What a tool returns (bookings, payees, balances) is sent to the model the
> agent uses. Use it with a model and a provider you trust with that data. Try it on the demo
> first (`hiro-budget demo` makes a fresh copy of fictional data; point `--config-dir` at its
> `config` folder).

## Set it up in the Hiro agent

A stdio server is a command with arguments. The agent starts it itself and passes only `PATH`
and the environment you configure, so give the configuration folder as an argument.

| Setting | Value |
|---|---|
| Command | the full path of the program, e.g. `C:\Tools\Hiro Budget\hiro-budget.exe` (from source: `.venv\Scripts\hiro-budget.exe` of the checkout) |
| Arguments, one per line | `--config-dir`<br>`C:\Users\you\Hiro Budget\config`<br>`mcp` |

Use full paths. The exe also finds `config\` next to itself, so `mcp` alone works there.

**First try:** add `--read-only` as a last argument (a line of its own). The server then
offers only the tools that read; nothing can change.

**Only these tools:** the agent's tool filter limits what the model may call. A sensible set
to start with: `get_overview`, `list_bookings`, `get_booking`, `list_categories`,
`list_rules`, `test_rule`, `get_budget`, and for working through bookings also
`categorize_bookings` and `undo_change`.

The server is started by the agent and ends with it.

## What the tools are

The tools are the operations of the command line, named in snake case:

| | tools |
|---|---|
| Reading (`readOnlyHint`) | `get_overview`, `list_accounts`, `get_budget`, `list_bookings`, `get_booking`, `list_categories`, `list_rules`, `test_rule`, `report_monthly`, `report_categories`, `report_years`, `report_payees`, `report_budget_vs_actual`, `report_net_worth`, `explain_value`, `list_imports`, `list_changes` |
| Changes that can be undone (`destructiveHint: false`) | `categorize_bookings`, `split_booking`, `mark_transfer`, `accept_suggestions`, `reset_bookings`, `set_booking_note`, `create_manual_booking`, `update_manual_booking`, `delete_manual_booking`, `create_category`, `rename_category`, `hide_category`, `create_group`, `rename_group`, `set_category_target`, `add_rule`, `update_rule`, `delete_rule`, `assign_budget`, `move_money`, `assign_to_targets`, `cover_overspending`, `copy_last_month`, `recategorize` |
| Cannot be undone (`destructiveHint: true`, the agent asks first) | `import_files`, `train`, `undo_change` |

`categorize_bookings`, `assign_budget` and `set_booking_note` are also marked idempotent. A
client that asks before every tool unless it is read only or not destructive therefore asks
for the three in the last row only.

Amounts are euros as strings with a dot (`"-12.34"`); arguments also accept `"-12,34"`.
Categories are addressed by key (`lebensmittel.supermarkt`) or exact name; bookings by the id
`list_bookings` shows. Results come as text and as structured JSON (protocol 2025-06-18).
A mistake of the model (an unknown category, a booking that cannot be edited) is a tool
error with a message, often with a suggestion; the model can correct it and try again.

## Undoing what an agent did

All changes of one run of the server share a **session id**.

```text
hiro-budget history                       # newest first, with the session of each change
hiro-budget undo --session <id>           # take back everything that run did, newest first
hiro-budget undo <change id>              # or one change
```

An agent can do this itself with `list_changes` and `undo_change`. An undo is refused, and
changes nothing, if something it touched was modified since. Before the first change of a
session a backup is made (`..._mcp`), and the open app window loads the changes within about
two seconds.

## Problems

- *The agent shows no tools.* Run the command by hand in a terminal; it must wait silently
  for input (stop it with Ctrl+C). A configuration error is printed and exits with 2.
- *Something else is printed by the server.* Only protocol lines go to stdout; everything
  else (log lines) goes to stderr, which the agent usually shows in its log. Warnings and
  errors, with their traceback, are also written to the app's log file
  (`data/logs/hiro-budget.log`, Help → Open log folder in the app).
- *"database is locked".* The app and the server share the database and wait up to 5 seconds
  for each other's write. Close a stuck window or try again.
- The server itself is the plugin `mcp` (Settings → Plugins…), built on plugin API 3
  ([plugins.md](plugins.md), section 8).
