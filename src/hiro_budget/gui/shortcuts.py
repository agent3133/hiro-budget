"""All keyboard shortcuts in one table (#72).

The table makes the key bindings (``bind``, ``apply``), the tooltips (``tip``) and the
F1 overview (``shortcuts_dialog``), so a key is written down once. Each entry is one row
of the overview; ``section`` is also its scope: ``window`` keys work everywhere, a page's
keys only on that page (tests check that no key is bound twice where both work).
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QT_TRANSLATE_NOOP, QCoreApplication, Qt
from PySide6.QtGui import QKeySequence, QShortcut

from hiro_budget.gui.common import with_keys

CONTEXT = "ShortcutsDialog"  # translations of the texts below
Key = str | QKeySequence.StandardKey
STD = QKeySequence.StandardKey
PAGE_COUNT = 6  # pages in the sidebar (Ctrl+1 … Ctrl+6); a test checks it


@dataclass(frozen=True)
class Shortcut:
    id: str
    section: str  # a key of SECTIONS; also where the keys work
    text: str  # source text of the action, translated in CONTEXT
    keys: tuple[Key, ...] = ()  # shown in the overview, bound in this order
    also: tuple[Key, ...] = ()  # bound too, not shown (Enter beside Return)
    sep: str = " / "  # between the shown keys (" … " for a range)
    prefix: str = ""  # before the keys, e.g. "Double-click"
    label: str = ""  # in place of keys, e.g. "Drag & drop"
    bound: bool = True  # False: only documented; Qt or another entry does it
    hidden: bool = False  # bound, not in the overview


SECTIONS: dict[str, str] = {
    "window": QT_TRANSLATE_NOOP("ShortcutsDialog", "Everywhere"),
    "budget": QT_TRANSLATE_NOOP("ShortcutsDialog", "Budget"),
    "transactions": QT_TRANSLATE_NOOP("ShortcutsDialog", "Bookings"),
    "accounts": QT_TRANSLATE_NOOP("ShortcutsDialog", "Accounts"),
    "editors": QT_TRANSLATE_NOOP("ShortcutsDialog", "Accounts, categories, rules"),
    "reports": QT_TRANSLATE_NOOP("ShortcutsDialog", "Reports"),
    "config_view": QT_TRANSLATE_NOOP("ShortcutsDialog", "Categories & rules"),
    "lists": QT_TRANSLATE_NOOP("ShortcutsDialog", "Lists and dialogs"),
}


TABLE: tuple[Shortcut, ...] = (
    # -- everywhere --
    Shortcut(
        "next_page",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Next page"),
        ("Ctrl+Tab",),
    ),
    Shortcut(
        "previous_page",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Previous page"),
        ("Ctrl+Shift+Tab",),
    ),
    Shortcut(
        "go_to_page",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Go to a page"),
        tuple(f"Ctrl+{i + 1}" for i in range(PAGE_COUNT)),
        sep=" … ",
    ),
    Shortcut(
        "back_forward",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Back / forward (also the mouse buttons)"),
        ("Alt+Left", "Alt+Right"),
    ),
    Shortcut(
        "regions",
        "window",
        QT_TRANSLATE_NOOP(
            "ShortcutsDialog", "Next / previous area of the page (sidebar, filters, list, details)"
        ),
        ("F6", "Shift+F6"),
    ),
    Shortcut(
        "sidebar",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Collapse or expand the sidebar"),
        ("Ctrl+B",),
    ),
    Shortcut(
        "search",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Search pages, commands, accounts, bookings"),
        ("Ctrl+K",),
    ),
    Shortcut(
        "search_box",
        "window",
        QT_TRANSLATE_NOOP(
            "ShortcutsDialog", "In the search: choose a result; Return opens it, Esc closes it"
        ),
        ("Up", "Down"),
        bound=False,  # the search box's own keys (search.SearchBox.keyPressEvent)
    ),
    Shortcut(
        "import",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Import statements"),
        ("Ctrl+I",),
    ),
    Shortcut(
        "fetch",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Fetch from the bank"),
        ("Ctrl+Shift+I",),
    ),
    Shortcut(
        "new_booking",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "New booking"),
        ("Ctrl+N",),
    ),
    Shortcut(
        "find",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Search bookings"),
        (STD.Find,),
    ),
    Shortcut(
        "save",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Save the changes of the current page"),
        (STD.Save,),
    ),
    Shortcut(
        "save_all",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Save the changes of all pages"),
        ("Ctrl+Shift+S",),
    ),
    Shortcut(
        "export",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Export the bookings (current filter)"),
        ("Ctrl+Shift+E",),
    ),
    Shortcut(
        "light_dark",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Switch between light and dark"),
        ("Ctrl+Shift+L",),
    ),
    Shortcut("refresh", "window", QT_TRANSLATE_NOOP("ShortcutsDialog", "Refresh"), (STD.Refresh,)),
    Shortcut(
        "context_menu",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Context menu of the selected row"),
        ("Shift+F10",),
    ),
    Shortcut(
        "menus",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Open a menu of the sidebar (File, View, …)"),
        label=QT_TRANSLATE_NOOP("ShortcutsDialog", "Alt + first letter"),
        bound=False,  # mnemonics of the menu titles (navigation.add_menu)
    ),
    Shortcut(
        "help",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "This overview"),
        (STD.HelpContents,),
    ),
    Shortcut(
        "guide",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "User guide"),
        ("Shift+F1",),
    ),
    Shortcut(
        "regex_help",
        "window",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "In a regex field: the regex cheat sheet"),
        ("F1",),
        bound=False,  # the help action decides (main_window.show_shortcuts)
    ),
    Shortcut("quit", "window", "Quit", (STD.Quit,), hidden=True),
    # -- budget --
    Shortcut(
        "budget_month",
        "budget",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Previous / next month"),
        ("Alt+PgUp", "Alt+PgDown"),
    ),
    Shortcut(
        "budget_today",
        "budget",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Current month"),
        ("Alt+Home",),
    ),
    Shortcut(
        "budget_assign",
        "budget",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Assign an amount to the selected row"),
        ("Return", "F2"),
        also=("Enter",),
    ),
    Shortcut(
        "move_money",
        "budget",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Move money from or to the selected row"),
        ("Ctrl+M",),
    ),
    Shortcut(
        "propose_budget",
        "budget",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Propose a budget"),
        ("Ctrl+P",),
    ),
    Shortcut(
        "expand_groups",
        "budget",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Expand / collapse all groups"),
        ("Ctrl+Shift+Down", "Ctrl+Shift+Up"),
    ),
    Shortcut(
        "undo_redo",
        "budget",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Undo / redo"),
        (STD.Undo, STD.Redo),
    ),
    # -- bookings --
    Shortcut(
        "booking_category",
        "transactions",
        QT_TRANSLATE_NOOP(
            "ShortcutsDialog",
            "Change the category; while reviewing, Return accepts the suggestion",
        ),
        ("Return", "F2"),
        also=("Enter",),
    ),
    Shortcut(
        "split",
        "transactions",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Split the selected booking"),
        ("Ctrl+D",),
    ),
    Shortcut(
        "delete_bookings",
        "transactions",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Delete manual bookings"),
        (STD.Delete,),
    ),
    Shortcut(
        "search_to_list",
        "transactions",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "From the search box or the details back to the list"),
        ("Esc",),
    ),
    Shortcut(
        "quick_filter",
        "transactions",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "All / uncategorized / to review"),
        ("Alt+1", "Alt+2", "Alt+3"),
    ),
    Shortcut(
        "clear_filters",
        "transactions",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Clear the filters (account, category, search)"),
        ("Ctrl+Shift+F",),
    ),
    Shortcut(
        "period_step",
        "transactions",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Previous / next period"),
        ("Alt+PgUp", "Alt+PgDown"),
    ),
    Shortcut(
        "note",
        "transactions",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Note for the selected booking"),
        ("Ctrl+E",),
    ),
    Shortcut(
        "create_rule",
        "transactions",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Create a rule from the selected booking"),
        ("Ctrl+R",),
    ),
    # -- the lists of accounts, categories and rules (and the split parts) --
    Shortcut(
        "item_new",
        "editors",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "New account, category, rule or split part"),
        ("Ctrl+Shift+N",),
    ),
    Shortcut(
        "group_new",
        "editors",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "New category group"),
        ("Ctrl+Shift+G",),
    ),
    Shortcut(
        "item_remove",
        "editors",
        QT_TRANSLATE_NOOP(
            "ShortcutsDialog", "Remove the selected account, category or rule (until saved)"
        ),
        ("Del",),
    ),
    Shortcut(
        "item_move",
        "editors",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Move the selected entry up / down"),
        ("Alt+Up", "Alt+Down"),
    ),
    Shortcut(
        "item_edit",
        "editors",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Edit the name of an account, the pattern of a rule"),
        ("F2",),
    ),
    Shortcut(
        "revert",
        "editors",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Revert the unsaved changes (asks first)"),
        ("Ctrl+Shift+R",),
    ),
    # -- reports --
    Shortcut(
        "report_tab",
        "reports",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Next / previous report"),
        ("Ctrl+PgDown", "Ctrl+PgUp"),
    ),
    Shortcut(
        "report_n",
        "reports",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Report 1 … 9 (in the order of the tabs)"),
        tuple(f"Alt+{i}" for i in range(1, 10)),
        sep=" … ",
    ),
    Shortcut(
        "report_period",
        "reports",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Previous / next period"),
        ("Alt+PgUp", "Alt+PgDown"),
    ),
    # -- categories & rules --
    Shortcut(
        "config_pane",
        "config_view",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Between categories and rules"),
        ("Ctrl+PgDown", "Ctrl+PgUp"),
    ),
    Shortcut(
        "rename_category",
        "config_view",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Rename a category or group, or change its key"),
        ("F2",),
        prefix=QT_TRANSLATE_NOOP("ShortcutsDialog", "Double-click"),
        bound=False,  # the tree's edit trigger
    ),
    Shortcut(
        "drag_categories",
        "config_view",
        QT_TRANSLATE_NOOP(
            "ShortcutsDialog",
            "Reorder categories or move them to another group;\n"
            "bookings, budget amounts and rules move with them",
        ),
        label=QT_TRANSLATE_NOOP("ShortcutsDialog", "Drag & drop"),
        bound=False,
    ),
    Shortcut(
        "category_key",
        "config_view",
        QT_TRANSLATE_NOOP(
            "ShortcutsDialog",
            "Identifies a category in rules and the configuration;\na changed key is updated there",
        ),
        label=QT_TRANSLATE_NOOP("ShortcutsDialog", "Key"),
        bound=False,
    ),
    Shortcut(
        "remove_category",
        "config_view",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Hides a category; its bookings and history are kept"),
        label=QT_TRANSLATE_NOOP("ShortcutsDialog", "Remove"),
        bound=False,
    ),
    # -- lists and dialogs (Qt's own keys) --
    Shortcut(
        "next_field",
        "lists",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Next / previous field"),
        ("Tab", "Shift+Tab"),
        bound=False,
    ),
    Shortcut(
        "move_in_list",
        "lists",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Move in a list"),
        ("Up", "Down"),
        bound=False,
    ),
    Shortcut(
        "tick",
        "lists",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "Tick a checkbox"),
        ("Space",),
        bound=False,
    ),
    Shortcut(
        "ok_cancel",
        "lists",
        QT_TRANSLATE_NOOP("ShortcutsDialog", "OK / cancel"),
        ("Return", "Esc"),
        bound=False,
    ),
)

_BY_ID = {s.id: s for s in TABLE}


def get(shortcut_id: str) -> Shortcut:
    return _BY_ID[shortcut_id]


def translate(text: str) -> str:
    return QCoreApplication.translate(CONTEXT, text) if text else ""


def sequences(shortcut_id: str, with_also: bool = True) -> list[QKeySequence]:
    """The key sequences of an entry: the shown ones in their order, then ``also``."""
    entry = get(shortcut_id)
    keys = entry.keys + (entry.also if with_also else ())
    return [QKeySequence(k) for k in keys]


def native(sequence: QKeySequence) -> str:
    return sequence.toString(QKeySequence.SequenceFormat.NativeText)


def keys_text(shortcut_id: str) -> str:
    """The keys as the overview shows them, in the user's language (Strg+Z)."""
    entry = get(shortcut_id)
    if entry.label:
        return translate(entry.label)
    shown = [native(s) for s in sequences(shortcut_id, with_also=False)]
    shown = [s for s in shown if s]
    if entry.sep == " … " and len(shown) > 2:
        shown = [shown[0], shown[-1]]
    text = entry.sep.join(shown)
    if entry.prefix:
        text = f"{translate(entry.prefix)}, {text}" if text else translate(entry.prefix)
    return text


def tip(text: str, shortcut_id: str, index: int = 0) -> str:
    """Tooltip text with the entry's key (its ``index``-th one), e.g. "Undo (Strg+Z)"."""
    keys = sequences(shortcut_id, with_also=False)
    return with_keys(text, keys[index]) if index < len(keys) else text


def bind(
    shortcut_id: str,
    parent,
    slot,
    context: Qt.ShortcutContext = Qt.ShortcutContext.WindowShortcut,
    index: int | None = None,
) -> list[QShortcut]:
    """QShortcuts on ``parent`` for the entry's keys (only the ``index``-th shown key if
    given, e.g. one of "Undo / redo")."""
    seqs = sequences(shortcut_id)
    if index is not None:
        seqs = [seqs[index]]
    made = []
    for seq in seqs:
        shortcut = QShortcut(seq, parent, slot)
        shortcut.setContext(context)
        made.append(shortcut)
    return made


def apply(action, shortcut_id: str) -> None:
    """The entry's keys on a QAction (the first one is shown in menus)."""
    action.setShortcuts(sequences(shortcut_id))


def rows() -> list[tuple[str, list[tuple[str, str]]]]:
    """The overview: (section title, [(keys, action)]) in table order."""
    out: dict[str, list[tuple[str, str]]] = {}
    for entry in TABLE:
        if entry.hidden:
            continue
        out.setdefault(entry.section, []).append((keys_text(entry.id), translate(entry.text)))
    return [(translate(SECTIONS[section]), items) for section, items in out.items()]
