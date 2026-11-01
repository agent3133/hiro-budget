"""Changing category and group keys in the category editor."""

import pytest
from PySide6.QtWidgets import QMessageBox
from sqlalchemy import select

from hiro_budget.config import TargetDef
from hiro_budget.db.models import Category, CategoryGroup, Split
from hiro_budget.gui.config_view import Drop
from hiro_budget.gui.main_window import MainWindow
from hiro_budget.services import import_service


@pytest.fixture
def errors(monkeypatch) -> list[str]:
    shown: list[str] = []
    for name in ("information", "warning"):
        monkeypatch.setattr(QMessageBox, name, lambda *a, **k: QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    monkeypatch.setattr(QMessageBox, "critical", lambda _p, _t, text, *a: shown.append(text))
    return shown


@pytest.fixture
def editor(qtbot, app, statement_pdf, errors):
    with app.session() as s:
        import_service.import_file(s, app.settings, statement_pdf)
    w = MainWindow(app)
    qtbot.addWidget(w)
    return w.config_view.categories


def item(editor, group: str, category: str | None = None):
    tree = editor.tree
    g = next(
        tree.topLevelItem(i)
        for i in range(tree.topLevelItemCount())
        if tree.topLevelItem(i).text(1) == group
    )
    if category is None:
        return g
    return next(g.child(i) for i in range(g.childCount()) if g.child(i).text(1) == category)


def category(app, key: str) -> Category | None:
    with app.session_factory() as s:
        return s.scalar(select(Category).where(Category.key == key))


def test_new_category_key_follows_its_name(editor, config_dir) -> None:
    editor.tree.setCurrentItem(item(editor, "kinder"))
    editor._add_category()
    new = editor.tree.currentItem()
    new.setText(0, "Schulessen")
    assert new.text(1) == "schulessen"
    new.setText(0, "Schulessen Grundschule")
    assert new.text(1) == "schulessen_grundschule"
    editor.save()
    text = (config_dir / "categories.yaml").read_text(encoding="utf-8")
    assert "key: schulessen_grundschule" in text


def test_typed_key_wins_over_the_name(editor) -> None:
    editor.tree.setCurrentItem(item(editor, "kinder"))
    editor._add_category()
    new = editor.tree.currentItem()
    new.setText(1, "Essen Schule")
    assert new.text(1) == "essen_schule"  # normalized
    new.setText(0, "Schulessen")
    assert new.text(1) == "essen_schule"


def test_renaming_a_key_keeps_bookings_and_updates_rules(app, editor, config_dir) -> None:
    old = category(app, "lebensmittel.supermarkt")
    with app.session_factory() as s:
        splits_before = s.scalars(select(Split.id).where(Split.category_id == old.id)).all()
    assert splits_before  # the REWE booking is categorized by the supermarkt rule

    item(editor, "lebensmittel", "supermarkt").setText(1, "markt")
    editor.save()

    renamed = category(app, "lebensmittel.markt")
    assert renamed is not None and renamed.id == old.id
    assert category(app, "lebensmittel.supermarkt") is None
    with app.session_factory() as s:
        assert s.scalars(select(Split.id).where(Split.category_id == old.id)).all() == splits_before
    rules = (config_dir / "rules.yaml").read_text(encoding="utf-8")
    assert "lebensmittel.markt" in rules and "lebensmittel.supermarkt" not in rules
    assert item(editor, "lebensmittel", "markt") is not None  # editor reloaded


def test_renaming_a_group_key_updates_the_configuration(app, editor, config_dir) -> None:
    group_id = category(app, "wohnen.baufinanzierung").group_id
    item(editor, "wohnen").setText(1, "haus")
    editor.save()
    assert category(app, "haus.baufinanzierung").group_id == group_id
    with app.session_factory() as s:
        assert s.get(CategoryGroup, group_id).key == "haus"
    config = (config_dir / "config.yaml").read_text(encoding="utf-8")
    assert "interest_category: haus.baufinanzierung" in config
    assert app.settings.app.mortgage[0].interest_category == "haus.baufinanzierung"


def test_swapping_two_keys(app, editor) -> None:
    a, b = category(app, "lebensmittel.supermarkt"), category(app, "lebensmittel.baeckerei")
    item(editor, "lebensmittel", "supermarkt").setText(1, "tmp")
    item(editor, "lebensmittel", "baeckerei").setText(1, "supermarkt")
    item(editor, "lebensmittel", "tmp").setText(1, "baeckerei")
    editor.save()
    assert category(app, "lebensmittel.baeckerei").id == a.id
    assert category(app, "lebensmittel.supermarkt").id == b.id


def test_key_of_a_removed_category_is_refused(app, editor, config_dir, errors) -> None:
    editor.tree.setCurrentItem(item(editor, "wohnen", "energie"))
    editor._remove()
    editor.save()  # hidden in the database, keeps its history
    assert category(app, "wohnen.energie").hidden

    item(editor, "wohnen", "nebenkosten").setText(1, "energie")
    before = (config_dir / "categories.yaml").read_text(encoding="utf-8")
    editor.save()
    assert errors and "still used by a removed category" in errors[-1]
    assert (config_dir / "categories.yaml").read_text(encoding="utf-8") == before
    assert category(app, "wohnen.nebenkosten") is not None


def children(editor, group: str) -> list[str]:
    g = item(editor, group)
    return [g.child(i).text(1) for i in range(g.childCount())]


def test_drag_category_within_its_group(app, editor, config_dir) -> None:
    item(editor, "lebensmittel")  # supermarkt, baeckerei, drogerie
    editor.move_item(
        item(editor, "lebensmittel", "drogerie"),
        item(editor, "lebensmittel", "supermarkt"),
        Drop.AboveItem,
    )
    assert children(editor, "lebensmittel") == ["drogerie", "supermarkt", "baeckerei"]
    editor.move_item(
        item(editor, "lebensmittel", "drogerie"),
        item(editor, "lebensmittel", "baeckerei"),
        Drop.BelowItem,
    )
    assert children(editor, "lebensmittel") == ["supermarkt", "baeckerei", "drogerie"]
    assert not editor.renames()
    editor.save()
    groups = {g.key: g for g in app.settings.categories.groups}
    assert [c.key for c in groups["lebensmittel"].categories][-1] == "drogerie"


def test_move_category_to_another_group(app, editor, config_dir) -> None:
    groups = {g.key: g for g in app.settings.categories.groups}
    groups["lebensmittel"].categories[0].target = TargetDef(type="monthly", amount=600)
    old = category(app, "lebensmittel.supermarkt")
    with app.session_factory() as s:
        splits = s.scalars(select(Split.id).where(Split.category_id == old.id)).all()
    assert splits

    editor.move_item(
        item(editor, "lebensmittel", "supermarkt"), item(editor, "sonstiges"), Drop.OnItem
    )
    assert children(editor, "sonstiges")[-1] == "supermarkt"  # end of the group
    assert editor.renames().categories == {"lebensmittel.supermarkt": "sonstiges.supermarkt"}
    editor.save()

    moved = category(app, "sonstiges.supermarkt")
    assert moved.id == old.id and moved.group_id == category(app, "sonstiges.online").group_id
    with app.session_factory() as s:
        assert s.scalars(select(Split.id).where(Split.category_id == old.id)).all() == splits
    rules = (config_dir / "rules.yaml").read_text(encoding="utf-8")
    assert "sonstiges.supermarkt" in rules and "lebensmittel.supermarkt" not in rules
    cats = {f"{g.key}.{c.key}": c for g in app.settings.categories.groups for c in g.categories}
    assert cats["sonstiges.supermarkt"].target == TargetDef(type="monthly", amount=600)


def test_moved_category_gets_a_free_key(app, editor) -> None:
    editor.tree.setCurrentItem(item(editor, "sonstiges"))
    editor._add_category()
    editor.tree.currentItem().setText(1, "supermarkt")
    editor.move_item(
        item(editor, "lebensmittel", "supermarkt"),
        item(editor, "sonstiges", "online"),
        Drop.AboveItem,
    )
    assert children(editor, "sonstiges")[0] == "supermarkt_2"
    editor.save()
    assert category(app, "sonstiges.supermarkt_2").name == "Supermarkt"


def test_move_category_above_a_group_ends_the_previous_group(editor) -> None:
    editor.move_item(
        item(editor, "lebensmittel", "supermarkt"), item(editor, "mobilitaet"), Drop.AboveItem
    )
    assert children(editor, "lebensmittel")[-1] == "supermarkt"
    first = editor.tree.topLevelItem(0)
    editor.move_item(item(editor, "lebensmittel", "supermarkt"), first, Drop.AboveItem)
    assert children(editor, "lebensmittel")[-1] == "supermarkt"  # nothing above the first


def test_drag_group(app, editor) -> None:
    def order() -> list[str]:
        return [editor.tree.topLevelItem(i).text(1) for i in range(editor.tree.topLevelItemCount())]

    before = order()
    editor.move_item(item(editor, "sonstiges"), item(editor, "wohnen"), Drop.AboveItem)
    assert order()[order().index("wohnen") - 1] == "sonstiges"
    # dropped on a category: after that category's group
    editor.move_item(item(editor, "sonstiges"), item(editor, "kinder", "betreuung"), Drop.OnItem)
    assert order()[order().index("kinder") + 1] == "sonstiges"
    assert sorted(order()) == sorted(before)
    assert item(editor, "sonstiges").childCount() == 3  # categories move along
    editor.save()
    keys = [g.key for g in app.settings.categories.groups]
    assert keys[keys.index("kinder") + 1] == "sonstiges"
