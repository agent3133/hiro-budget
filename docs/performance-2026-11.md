# Performance work, November 2026 (#54–#62)

On 2026-11-02 a profile of the whole app (#62) found eight slow spots, filed as #54–#61.
All eight were fixed on 2026-11-03 on branch `perf`, one commit per issue (plus a follow-up
for #56). The app now writes nothing at start, an import no longer freezes the window, and
the screens that took 0.6–1.8 s on ten years of bookings take 0.03–0.3 s. Every logic change
was checked to give byte-identical results.

## Method

- **Data:** the demo database (8,451 bookings, 10 years, budget from 2021-11), a fresh copy
  for each measurement. The real database (≈1,900 bookings) hides most of these problems.
- **From source:** Python 3.12, a script drives the real `MainWindow` (1600×950, Rosé Pine
  theme). Each step is timed until the event queue is empty, as the median of 3 runs.
  `cProfile` found the cause of each hot step.
- **From the exe:** Nuitka builds of `main` (before) and `perf` (after), measured from
  outside the process; see [The exe](#the-exe).
- **Equivalence:** for every change to logic the outputs were written as JSON before and
  after the change and compared byte for byte (listed per issue below).
- **Tests:** 651 tests pass (642 before; 9 new tests, one or two per issue).

## Results from source

| # | what | before | after |
|---|---|---|---|
| #55 | window frozen after an import (classifier training) | 3,125 ms | 56–71 ms |
| #54 | `App()` at start / SQL writes per start | 298–340 ms / 622 | 81–118 ms / 0 |
| #54 | recategorize | 2,843–3,135 ms | 723–749 ms |
| #56 | bookings: first build / full refresh / `reload_filters` | 827 / 655 / 761 ms | 301 / 242 / 254 ms |
| #56 | refresh everything (F5) with the bookings page hidden | 774 ms | 85 ms |
| #58 | assign a category to 20 selected bookings / to 1 | 1,767 / 93 ms | 125 / 44 ms |
| #57 | Categories & rules: later visit / after a booking edit | 595 / 607 ms | 31 / 114 ms |
| #59 | `budget_vs_actual`, everything (service) | 493 ms | 58 ms |
| #60 | report tabs, everything: Overview / Mortgage / Budget vs. actual / Net worth / Savings rate | 567 / 948 / 669 / 778 / 743 ms | 365 / 194 / 194 / 231 / 210 ms |
| #61 | return to the overview, nothing changed | 167 ms | 23 ms |
| | start until the window is shown | 1,749–1,760 ms | 1,488–1,645 ms |

## The exe

Both branches were built with `packaging/build_exe.py` (Nuitka 4.3.1, MSVC) on 2026-11-03:
`main` at `ec38676` (before) and `perf` at `8229dc8` (after). The two builds are the same
size: 236 / 237 MB without the demo, the exe 109 MB each.

A script measured each exe from outside the process. Each run used a fresh copy of the
demo that the build ships. It timed how long the window took to appear, then sent page
shortcuts (`Ctrl+1`…`Ctrl+6`) to the app's window. After each key, it timed how long the GUI
thread stayed busy: `SendMessageTimeout(WM_NULL)` pings every 5 ms until they answered at
once for 0.3 s. Each step had to leave only the main window visible. Results are the median
of 5 runs after one warm-up. The CLI commands are whole processes, median of 3.

| what (exe, demo) | # | before (main) | after (perf) |
|---|---|---|---|
| start until the window is visible | #54 | 1,578 ms | **1,205 ms** |
| start until idle, bookings page prebuilt | #54, #56 | 3,366 ms | **2,706 ms** |
| Categories & rules, first visit | #57 | 804 ms | **244 ms** |
| Categories & rules, later visit | #57 | 686 ms | **33 ms** |
| return to the overview (from the budget) | #61 | 194 ms | **43 ms** |
| overview again after the other pages | #61 | 217 ms | **52 ms** |
| budget page, first visit | | 155 ms | 145 ms |
| bookings page (prebuilt) | | 58 ms | 55 ms |
| reports page, first visit (last 12 months) | #60 | 222 ms | 204 ms |
| accounts page, first visit | | 178 ms | 207 ms |
| `hiro-budget.exe recategorize` (whole process) | #54 | 4,481 ms | **1,414 ms** |
| `hiro-budget.exe train` (whole process) | #55 | 4,549 ms | 3,957 ms |

- **Start:** the exe starts 0.37 s faster because nothing is written at start anymore (#54).
  It is idle 0.66 s sooner because the bookings page builds faster (#56).
- **`train`:** this command still trains in the foreground. It is 0.6 s faster only because
  the samples load faster. In the app, training now runs in the background (#55).
- **Accounts page, first visit:** +29 ms. The runs are tight (205–215 vs. 177–188 ms); the
  cause was not investigated.
- **Not measured in the exe:** refresh with F5, assigning categories (#58), report tabs over
  "everything" (#59, #60) and an import (#55). These need mouse input or dialogs. The F5
  sent from outside did not trigger the refresh (52 ms on both builds, and the bookings page
  was not stale afterwards), so those rows were dropped. The source measurements above
  cover them.

## Per issue

### #55 Classifier training froze the window

- **Cause:** after every import, bank fetch and Retrain, the classifier was trained in the
  GUI thread: 3.1 s on the demo, 3.4 s on the real data.
- **Change:** `gui/training.py` reads the samples in the GUI thread (now plain columns:
  351 → 54 ms) and fits and saves the model in a daemon thread. The status bar says it is
  learning. A second request during a run trains once more afterwards. `Classifier.save`
  replaces the model file atomically.
- **Outcome:** the window is blocked for 56–71 ms; the longest pause of the event loop during
  training is 35 ms. The training itself is not faster.
- **Equivalence:** the 7,361 training samples are identical.

### #54 Counter-bookings rewritten at every start

- **Cause:** `sync_counterparts` replaced the split of every counter-booking at every start
  (311 deletes + 311 inserts, split ids growing by 311 per start). Found on the way: the
  categorizer did the same to all 5,400 non-manual bookings on every recategorization.
- **Change:** splits are replaced only when they differ. Counter-bookings, their splits and
  accounts are loaded in a few queries instead of one lazy load per transfer.
- **Outcome:** start: 0 writes, `App()` 340 → ~100 ms. Recategorize: 3.0 → 0.73 s, SQL
  statements 14,216 → 93.
- **Equivalence:** all transaction and split rows after 4 starts and 3 recategorizations.

### #56 Bookings page

- **Cause:** `list_transactions` built 17,000 ORM objects to fill the rows. `refresh_all`
  reloaded the page also while it was hidden. The ⇄ of transfers is in neither Inter nor
  Segoe UI: the first one cost 190 ms of Windows font fallback.
- **Change:** plain columns in three queries; a hidden page reloads when shown. Transfers
  show **↔**, which both fonts have.
- **Outcome:** `list_transactions` 615 → 205 ms; first build 827 → 301 ms.
- **Equivalence:** the rows for 19 filters and 4 single rows.

### #58 Assigning several bookings

- **Cause:** one session, one commit and a full refresh per booking. Each refresh redrew the
  hidden accounts page (table and balance chart twice).
- **Change:** one session and one refresh for all selected bookings. A booking that cannot
  take the category (a counter-booking) is skipped with one message. A hidden accounts page
  updates only its numbers and the sidebar.
- **Outcome:** 20 bookings 1,767 → 125 ms; 1 booking 93 → 44 ms.

### #57 Categories & rules

- **Cause:** every visit reloaded all bookings (with one lazy load each) and matched all 44
  rules against them again.
- **Change:** the loaded bookings stay until bookings change. They are read as plain columns
  (330 → 31 ms). Hits are counted rule by rule, each distinct text searched once
  (188 → 54 ms).
- **Outcome:** a later visit 595 → 31 ms; a visit after an edit 607 → 114 ms.
- **Equivalence:** samples and hits of all demo rules; a test compares with the plain loop.

### #59 Budget vs. actual

- **Cause:** each month was computed by replaying every month from the budget start, so the
  report grew with the square of the months.
- **Change:** `compute_months` makes one pass and yields every month; `compute_month` is
  its one-month case.
- **Outcome:** everything (60 months) 493 → 58 ms.
- **Equivalence:** all 66 months with and without balances, and 20 report variants.

### #60 Reports over long periods

- **Cause:** the fixed-cost detection and the mortgage report loaded every booking or split
  as ORM objects. Found on the way: every refresh also computed and drew the hidden overview
  tab.
- **Change:** plain columns; the mortgage report filters in SQL. The overview tab is computed
  when shown or exported.
- **Outcome:** `mortgage_progress` 236 → 23 ms, `recurring_costs` 263 → 54 ms; the Mortgage
  tab over everything 948 → 194 ms.
- **Equivalence:** fixed costs, costs per month and mortgage, for everything, 12 months and
  each account.

### #61 Overview recomputed on every visit

- **Cause:** `showEvent` always refreshed the start page.
- **Change:** it refreshes only after bookings, the budget or the configuration changed, or
  on a new day.
- **Outcome:** a return without changes 167 → 23 ms.

## What went wrong on the way

The first fix for the ⇄ named Segoe UI Symbol as a fallback font. The test timed an untimed
text first and then the arrow, so it showed 0.4 ms. In a fresh process the fallback only
moved the 200 ms of font loading to the first text of any kind, at every start
(`MainWindow()` 205 → 390 ms). The startup comparison caught it; the fix was reverted for
the ↔. Lesson (now in CLAUDE.md): time the first text of a fresh process too.

## Changes users notice

- Transfers show ↔ instead of ⇄.
- After an import the window stays usable; the status bar says the classifier is learning.
- Assigning a category to a selection that includes a counter-booking shows one message.

## Limits and open points

- An edit of one booking takes 44 ms, mostly `account_balances` (3 queries per account).
- The overview after a change still takes ~140 ms; the overview report tab over everything
  365 ms (chart drawing ~110 ms, fixed-cost detection ~120 ms).
- Classifier training is as slow as before, only in the background.
- Found, not changed: with the period "everything" the "Income and expenses per month" chart
  shows no visible bars, and the category trend shows only 12 months (also on `main`).
