"""Categorization pipeline: transfers -> mortgage split -> rules -> ML -> uncategorized.

Bookings with a manual split are never touched. All other splits are recomputed from
scratch each time the pipeline runs on a booking.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from hiro_budget.config import Settings
from hiro_budget.db.models import Account, Category, Split, SplitSource, Transaction
from hiro_budget.services.categorization.classifier import Classifier, feature_text, features
from hiro_budget.services.categorization.mortgage import MortgageMatcher, extract_split
from hiro_budget.services.categorization.rules import compile_rules, first_match
from hiro_budget.services.categorization.transfers import (
    PatternMatcher,
    TransferTarget,
    find_transfer_target,
)
from hiro_budget.services.counterparts import sync_counterparts

log = logging.getLogger(__name__)


@dataclass
class CategorizationStats:
    counts: Counter[str] = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        parts = ", ".join(f"{k}: {v}" for k, v in sorted(self.counts.items()))
        return parts or "nothing to do"


def _split_key(s: Split) -> tuple:
    category = s.category.id if s.category is not None else s.category_id
    target = s.transfer_account.id if s.transfer_account is not None else s.transfer_account_id
    return (s.amount_cents, category, target, s.source, s.memo, s.rule_id, s.confidence)


def _set_splits(tx: Transaction, splits: list[Split]) -> None:
    """Replace the splits of ``tx`` only if they differ: recategorizing ten years of
    bookings rewrote all of them (5,400 deletes and inserts) though few change (#54)."""
    if [_split_key(s) for s in tx.splits] != [_split_key(s) for s in splits]:
        tx.splits[:] = splits


class Categorizer:
    """Holds compiled rules, matchers and the classifier for one session."""

    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        # Splits reference the ORM objects (not ids) so relationships are usable before flush.
        self.categories: dict[str, Category] = {
            c.key: c for c in session.scalars(select(Category).where(Category.hidden.is_(False)))
        }
        accounts = {a.name: a for a in session.scalars(select(Account))}
        self.rules = compile_rules(settings.rules.rules)
        self.transfer_targets = [
            TransferTarget(
                accounts[a.name],
                PatternMatcher.compile(a.match),
                self.categories.get(a.transfer_category or ""),
            )
            for a in settings.app.own_accounts
            if a.match and a.name in accounts
        ]
        self.mortgages = [
            MortgageMatcher(
                name=m.name,
                matcher=PatternMatcher.compile(m.match),
                loan_account=accounts[m.loan_account],
                interest_category=self.categories[m.interest_category],
                principal_category=self.categories.get(m.principal_category or ""),
            )
            for m in settings.app.mortgage
            if m.loan_account in accounts and m.interest_category in self.categories
        ]
        ml = settings.app.ml
        self.classifier = Classifier.load(settings.model_path) if ml.enabled else None

    # -- single booking steps ----------------------------------------------------

    def _deterministic(self, tx: Transaction, stats: CategorizationStats) -> list[Split] | None:
        """Transfer, mortgage and rule steps. Returns splits or None if nothing matched."""
        target = find_transfer_target(self.transfer_targets, tx)
        if target is not None:
            stats.counts["transfer"] += 1
            # Money leaving the budget into a tracking account is budgeted like an expense.
            leaves_budget = (
                tx.amount_cents < 0 and tx.account.on_budget and not target.account.on_budget
            )
            return [
                Split(
                    amount_cents=tx.amount_cents,
                    transfer_account=target.account,
                    category=target.category if leaves_budget else None,
                    source=SplitSource.TRANSFER,
                )
            ]

        for m in self.mortgages:
            if not m.applies(tx):
                continue
            parts = extract_split(tx.purpose, tx.amount_cents)
            if parts is None:
                stats.warnings.append(
                    f"{tx.booking_date} {tx.amount_cents / 100:.2f}: mortgage '{m.name}' matched "
                    "but interest/principal could not be extracted or do not add up"
                )
                return None
            stats.counts["mortgage"] += 1
            return [
                Split(
                    amount_cents=parts.interest_cents,
                    category=m.interest_category,
                    source=SplitSource.MORTGAGE,
                    memo="Zinsen",
                ),
                Split(
                    amount_cents=parts.principal_cents,
                    transfer_account=m.loan_account,
                    # The loan is a tracking account: the principal leaves the budget
                    # and is budgeted in its own category.
                    category=m.principal_category,
                    source=SplitSource.MORTGAGE,
                    memo="Tilgung",
                ),
            ]

        rule = first_match(self.rules, tx)
        if rule is not None and rule.category in self.categories:
            stats.counts["rule"] += 1
            return [
                Split(
                    amount_cents=tx.amount_cents,
                    category=self.categories[rule.category],
                    source=SplitSource.RULE,
                    rule_id=rule.id,
                )
            ]
        return None

    # -- public API --------------------------------------------------------------

    def categorize(self, transactions: list[Transaction]) -> CategorizationStats:
        """(Re)compute splits for all non-manual bookings in ``transactions``."""
        stats = CategorizationStats()
        pending: list[Transaction] = []
        for tx in transactions:
            if tx.is_counterpart:
                continue  # managed by services.counterparts
            if tx.is_manual:
                stats.counts["manual (kept)"] += 1
                continue
            splits = self._deterministic(tx, stats)
            if splits is None:
                pending.append(tx)
            else:
                _set_splits(tx, splits)

        predictions = (
            self.classifier.predict([features(t) for t in pending])
            if (self.classifier and pending)
            else []
        )
        threshold = self.settings.app.ml.min_confidence
        for i, tx in enumerate(pending):
            pred = predictions[i] if predictions else None
            if pred and pred.confidence >= threshold and pred.category_key in self.categories:
                stats.counts["ml"] += 1
                _set_splits(
                    tx,
                    [
                        Split(
                            amount_cents=tx.amount_cents,
                            category=self.categories[pred.category_key],
                            source=SplitSource.ML,
                            confidence=round(pred.confidence, 3),
                        )
                    ],
                )
            else:
                stats.counts["uncategorized"] += 1
                _set_splits(
                    tx, [Split(amount_cents=tx.amount_cents, source=SplitSource.UNCATEGORIZED)]
                )
        sync_counterparts(self.session, transactions, self.settings.app.matching.days)
        for w in stats.warnings:
            log.warning(w)
        return stats


def load_transactions(session: Session, ids: list[int] | None = None) -> list[Transaction]:
    q = select(Transaction).options(
        selectinload(Transaction.splits), selectinload(Transaction.account)
    )
    if ids is not None:
        q = q.where(Transaction.id.in_(ids))
    return list(session.scalars(q.order_by(Transaction.booking_date, Transaction.id)))


def training_samples(session: Session) -> list[tuple[str, str]]:
    """Bookings with exactly one manually or rule-assigned category split.

    Plain columns, no ORM entities: a whole history of bookings in a few ms (#55)."""
    single = (
        select(Split.transaction_id)
        .group_by(Split.transaction_id)
        .having(func.count(Split.id) == 1)
        .scalar_subquery()
    )
    rows = session.execute(
        select(
            Transaction.amount_cents,
            Transaction.merchant,
            Transaction.counterparty,
            Transaction.booking_type,
            Transaction.purpose,
            Transaction.details,
            Category.key,
        )
        .join(Split, Split.transaction_id == Transaction.id)
        .join(Category, Category.id == Split.category_id)
        .where(
            Transaction.id.in_(single),
            Split.source.in_((SplitSource.MANUAL, SplitSource.RULE)),
        )
        .order_by(Transaction.booking_date, Transaction.id)
    )
    return [(feature_text(*row[:6]), row[6]) for row in rows]


def fit_and_save(samples: list[tuple[str, str]], settings: Settings) -> Classifier | None:
    """The part of training without the database: may run in a worker thread."""
    clf = Classifier.train(samples, settings.app.ml.min_samples)
    if clf is not None:
        clf.save(settings.model_path)
    return clf


def train_classifier(session: Session, settings: Settings) -> Classifier | None:
    return fit_and_save(training_samples(session), settings)


def recategorize_all(session: Session, settings: Settings) -> CategorizationStats:
    return Categorizer(session, settings).categorize(load_transactions(session))
