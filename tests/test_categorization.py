from datetime import date

import pytest
from sqlalchemy import select

from hiro_budget.app import App
from hiro_budget.config import RuleDef
from hiro_budget.db.models import Account, Category, Split, SplitSource, Transaction
from hiro_budget.services.categorization.classifier import Classifier, features
from hiro_budget.services.categorization.mortgage import extract_split
from hiro_budget.services.categorization.pipeline import (
    Categorizer,
    load_transactions,
    recategorize_all,
    train_classifier,
)
from hiro_budget.services.categorization.rules import CompiledRule

_counter = 0


def make_tx(
    session, *, amount, counterparty="", purpose="", merchant=None, booking_type="Lastschrift"
):
    global _counter
    _counter += 1
    acc = session.scalar(select(Account).where(Account.name == "ING Girokonto"))
    tx = Transaction(
        account=acc,
        booking_date=date(2026, 3, 1),
        amount_cents=amount,
        booking_type=booking_type,
        counterparty=counterparty,
        purpose=purpose,
        merchant=merchant,
        dedupe_key=f"k{_counter}",
    )
    session.add(tx)
    session.flush()
    return tx


def category_key(tx):
    return [s.category.key if s.category else None for s in tx.splits]


# --- rules -----------------------------------------------------------------------


def _rule(**match):
    return CompiledRule.compile(RuleDef(id="r", category="x.y", match=match))


def test_rule_requires_all_fields(app) -> None:
    with app.session() as s:
        tx = make_tx(s, amount=-6300, merchant="RMV APP RMV SMART AP")
        assert _rule(merchant="(?i)rmv").matches(tx)
        assert _rule(merchant="(?i)rmv", amount_max=-50).matches(tx)
        assert not _rule(merchant="(?i)rmv", amount_min=0).matches(tx)
        assert not _rule(merchant="(?i)rmv", booking_type="^Gutschrift$").matches(tx)
        assert _rule(account="Giro").matches(tx)


def test_rule_on_missing_merchant_does_not_match(app) -> None:
    with app.session() as s:
        tx = make_tx(s, amount=-100, counterparty="REWE")
        assert not _rule(merchant="(?i)rewe").matches(tx)


# --- mortgage --------------------------------------------------------------------


def test_extract_mortgage_split() -> None:
    parts = extract_split("ING-DIBA AG 9988 ZINSEN 1.000,00 TILGUNG 500,00", -150000)
    assert (parts.interest_cents, parts.principal_cents) == (-100000, -50000)


def test_extract_mortgage_split_rejects_wrong_sum() -> None:
    assert extract_split("ZINSEN 1.000,00 TILGUNG 500,00", -149900) is None
    assert extract_split("no split info", -150000) is None


# --- pipeline --------------------------------------------------------------------


def test_pipeline_order_and_sources(app) -> None:
    with app.session() as s:
        rewe = make_tx(s, amount=-2345, counterparty="VISA REWE", merchant="REWE MUSTERSTADT")
        mortgage = make_tx(
            s, amount=-100000, counterparty="Muster", purpose="ZINSEN 600,00 TILGUNG 400,00"
        )
        saving = make_tx(s, amount=-50000, counterparty="Muster", purpose="Extra-Konto Sparen")
        unknown = make_tx(s, amount=-999, counterparty="Unbekannt GmbH")
        stats = Categorizer(s, app.settings).categorize([rewe, mortgage, saving, unknown])

        assert category_key(rewe) == ["lebensmittel.supermarkt"]
        assert rewe.splits[0].source == SplitSource.RULE
        assert rewe.splits[0].rule_id == "supermarkt"

        assert [sp.amount_cents for sp in mortgage.splits] == [-60000, -40000]
        assert category_key(mortgage) == ["wohnen.baufinanzierung", None]
        assert mortgage.splits[1].transfer_account.name == "ING Baufinanzierung"

        assert saving.is_transfer
        assert saving.splits[0].transfer_account.name == "ING Extra-Konto"

        assert unknown.splits[0].is_uncategorized
        assert stats.counts == {"rule": 1, "mortgage": 1, "transfer": 1, "uncategorized": 1}


def test_manual_splits_are_never_overwritten(app) -> None:
    with app.session() as s:
        tx = make_tx(s, amount=-2345, merchant="REWE")
        cat = s.scalar(select(Category).where(Category.key == "freizeit.restaurants"))
        tx.splits = [Split(amount_cents=-2345, category=cat, source=SplitSource.MANUAL)]
        s.flush()
        recategorize_all(s, app.settings)
        assert category_key(tx) == ["freizeit.restaurants"]


def test_split_amounts_sum_to_booking(app) -> None:
    with app.session() as s:
        for amount in (-100000, -2345, 4711):
            make_tx(s, amount=amount, merchant="REWE", purpose="ZINSEN 600,00 TILGUNG 400,00")
        recategorize_all(s, app.settings)
        for tx in load_transactions(s):
            assert sum(sp.amount_cents for sp in tx.splits) == tx.amount_cents


# --- classifier ------------------------------------------------------------------


def test_classifier_learns_from_rule_categorized_bookings(app) -> None:
    with app.session() as s:
        for i in range(6):
            make_tx(s, amount=-1000 - i, merchant=f"REWE FILIALE {i}")
            make_tx(s, amount=-500 - i, merchant=f"BAECKEREI SCHMIDT {i}")
        recategorize_all(s, app.settings)
        clf = train_classifier(s, app.settings)
        assert clf is not None and clf.n_samples == 12
        assert app.settings.model_path.exists()

        # Neither rule matches "REVE" (typo); the classifier should still pick groceries.
        tx = make_tx(s, amount=-1234, merchant="REVE FILIALE 9")
        Categorizer(s, app.settings).categorize([tx])
        assert tx.splits[0].source == SplitSource.ML
        assert category_key(tx) == ["lebensmittel.supermarkt"]
        assert 0 < tx.splits[0].confidence <= 1


def test_old_scikit_learn_model_is_trained_again(app) -> None:
    """A model of the scikit-learn version (classifier.joblib) is replaced at the start."""
    with app.session() as s:
        for i in range(6):
            make_tx(s, amount=-1000 - i, merchant=f"REWE FILIALE {i}")
            make_tx(s, amount=-500 - i, merchant=f"BAECKEREI SCHMIDT {i}")
        recategorize_all(s, app.settings)
    model = app.settings.model_path
    assert model.suffix == ".npz"
    model.parent.mkdir(parents=True, exist_ok=True)
    model.with_suffix(".joblib").write_bytes(b"old scikit-learn model")
    assert not model.exists()
    App(app.settings)
    assert Classifier.load(model).n_samples == 12


def test_classifier_needs_enough_data() -> None:
    assert Classifier.train([("a", "x.y")] * 3, min_samples=10) is None
    assert Classifier.train([("a", "x.y")] * 20, min_samples=10) is None  # one class only


@pytest.mark.parametrize("amount", [-100, 100])
def test_features_contain_direction(app, amount) -> None:
    with app.session() as s:
        tx = make_tx(s, amount=amount, merchant="Shop 123")
        text = features(tx)
        assert text.startswith("__out__" if amount < 0 else "__in__")
        assert "123" not in text
