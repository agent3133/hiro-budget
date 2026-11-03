"""The shortcut table (gui/shortcuts.py): one key, one action where both work (#72)."""

from collections import defaultdict

from PySide6.QtGui import QKeySequence

from hiro_budget.gui import shortcuts
from hiro_budget.gui.main_window import MainWindow


def _bound_keys() -> dict[str, list[tuple[str, str]]]:
    """Scope -> [(key, entry id)] of every key the table binds."""
    out: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for entry in shortcuts.TABLE:
        if not entry.bound:
            continue
        for seq in shortcuts.sequences(entry.id):
            if not seq.isEmpty():
                out[entry.section].append((seq.toString(), entry.id))
    return out


def test_no_key_has_two_actions_where_both_work(qapp) -> None:
    bound = _bound_keys()
    clashes = []
    for scope, keys in bound.items():
        seen: dict[str, str] = {}
        others = bound["window"] if scope != "window" else []
        if scope in ("accounts", "config_view"):  # the editors' keys work there too
            others = others + bound["editors"]
        for key, owner in others:
            seen[key] = owner
        for key, owner in keys:
            if key in seen and seen[key] != owner:
                clashes.append((scope, key, seen[key], owner))
            seen[key] = owner
    assert clashes == []


def test_page_keys_match_the_pages(qapp) -> None:
    assert shortcuts.PAGE_COUNT == len(MainWindow.PAGES)
    assert shortcuts.keys_text("go_to_page") == " … ".join(
        QKeySequence(k).toString(QKeySequence.SequenceFormat.NativeText)
        for k in ("Ctrl+1", f"Ctrl+{shortcuts.PAGE_COUNT}")
    )


def test_every_entry_has_a_known_section_and_unique_id() -> None:
    ids = [e.id for e in shortcuts.TABLE]
    assert len(ids) == len(set(ids))
    assert all(e.section in shortcuts.SECTIONS for e in shortcuts.TABLE)


def test_tooltips_name_the_key(qapp) -> None:
    native = QKeySequence("Alt+Right").toString(QKeySequence.SequenceFormat.NativeText)
    assert shortcuts.tip("Forward", "back_forward", 1) == f"Forward ({native})"
    assert shortcuts.tip("Nothing", "menus") == "Nothing"  # no keys: the text alone
